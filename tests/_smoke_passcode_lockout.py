"""Smoke test: passcode hard lockout (group + platform sides).

Run: python tests/_smoke_passcode_lockout.py
Uses a throwaway SQLite DB so the real database is never touched.
"""
import asyncio
import os
import sys
import tempfile

# Point the app at a throwaway SQLite DB before any app import reads config.
# NOTE: DATABASE_URL must be set to "" (not popped) — config.py calls
# load_dotenv() at import time, and load_dotenv does NOT override variables
# that already exist in os.environ, so an empty string here wins over the
# .env file's Postgres URL and forces the SQLite path.
_tmpdir = tempfile.mkdtemp(prefix="lockout_smoke_")
os.environ["DATABASE_PATH"] = os.path.join(_tmpdir, "smoke.db")
os.environ["DATABASE_URL"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from renewise.db import queries, schema  # noqa: E402
from renewise.db.connection import _db  # noqa: E402
from renewise.services import platform as platform_svc  # noqa: E402


async def main() -> None:
    await schema.init_db()

    # ── Group side ────────────────────────────────────────────────────────────
    async with _db() as db:
        await db.execute(
            "INSERT INTO groups (telegram_chat_id, admin_telegram_id, price_usd_cents) "
            "VALUES ($1, $2, $3)",
            -100999001, 4242, 500,
        )
        gid = await db.fetchval("SELECT id FROM groups WHERE telegram_chat_id=$1", -100999001)

    # 1. Set initial passkey (first time — no current required)
    ok = await queries.set_group_passcode(gid, "1111", 4242)
    assert ok, "set_group_passcode first-time failed"

    # 2. Four wrong attempts → still just False
    for i in range(4):
        ok = await queries.check_group_wallet_passcode(gid, "0000", 4242)
        assert ok is False, f"wrong passcode #{i+1} unexpectedly passed"

    # 3. Fifth wrong attempt → lockout engaged
    ok = await queries.check_group_wallet_passcode(gid, "0000", 4242)
    assert ok is False, "fifth wrong passcode unexpectedly passed"

    # 4. Even the CORRECT passcode is now rejected with PasscodeLockedError
    locked = False
    try:
        await queries.check_group_wallet_passcode(gid, "1111", 4242)
    except queries.PasscodeLockedError as e:
        locked = True
        assert e.retry_after_seconds > 0, "retry_after_seconds not positive"
        print(f"  group lockout: retry_after_seconds={e.retry_after_seconds}")
    assert locked, "correct passcode accepted during lockout — lockout NOT enforced"

    # 5. set_group_passcode is also blocked while locked
    locked = False
    try:
        await queries.set_group_passcode(gid, "2222", 4242, current_passcode="1111")
    except queries.PasscodeLockedError:
        locked = True
    assert locked, "set_group_passcode allowed during lockout — lockout NOT enforced"

    # 6. Audit log captured the lockout event
    async with _db() as db:
        actions = [r["action"] for r in await db.fetch(
            "SELECT action FROM admin_audit_log WHERE group_id=$1 ORDER BY id", gid)]
    assert "group_passcode_lockout" in actions, f"lockout not audited: {actions}"
    assert actions.count("group_passcode_change_failed") >= 0
    print(f"  group audit trail: {actions}")

    # 7. Simulate lock expiry → correct passcode works and resets the counter
    from datetime import datetime, timedelta, timezone as _tz
    expired = (datetime.now(_tz.utc) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    async with _db() as db:
        await db.execute(
            "UPDATE groups SET passcode_locked_until = $1 WHERE id=$2", expired, gid)
    ok = await queries.check_group_wallet_passcode(gid, "1111", 4242)
    assert ok, "correct passcode rejected after lockout expired"
    async with _db() as db:
        attempts = await db.fetchval(
            "SELECT passcode_failed_attempts FROM groups WHERE id=$1", gid)
    assert int(attempts) == 0, f"failure counter not reset on success: {attempts}"
    print("  group side: lockout, audit, expiry-reset all OK")

    # ── Platform side ─────────────────────────────────────────────────────────
    async with _db() as db:
        await db.execute(
            "INSERT INTO platforms (owner_telegram_id, platform_name, publishable_key_test, secret_key_test, wallet_address) "
            "VALUES ($1, $2, $3, $4, $5)",
            4242, "smoke", "pk_test_smoke", "sk_test_smoke", "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        pid = await db.fetchval("SELECT id FROM platforms WHERE platform_name='smoke'")

    ok = await platform_svc.set_platform_passcode(pid, "3333", 4242)
    assert ok, "set_platform_passcode first-time failed"

    for i in range(4):
        ok = await platform_svc.update_platform_wallet(
            pid, "UQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", 4242, "0000")
        assert ok is False, f"platform wrong passcode #{i+1} unexpectedly passed"
    ok = await platform_svc.update_platform_wallet(
        pid, "UQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", 4242, "0000")
    assert ok is False, "platform fifth wrong passcode unexpectedly passed"

    locked = False
    try:
        await platform_svc.update_platform_wallet(
            pid, "UQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", 4242, "3333")
    except queries.PasscodeLockedError as e:
        locked = True
        print(f"  platform lockout: retry_after_seconds={e.retry_after_seconds}")
    assert locked, "platform correct passcode accepted during lockout — NOT enforced"

    locked = False
    try:
        await platform_svc.set_platform_passcode(pid, "4444", 4242, current_passcode="3333")
    except queries.PasscodeLockedError:
        locked = True
    assert locked, "platform set_platform_passcode allowed during lockout — NOT enforced"

    async with _db() as db:
        actions = [r["action"] for r in await db.fetch(
            "SELECT action FROM platform_audit_log WHERE platform_id=$1 ORDER BY id", pid)]
    assert "passcode_lockout" in actions, f"platform lockout not audited: {actions}"
    print(f"  platform audit trail: {actions}")

    # Expiry → correct passcode works and resets
    async with _db() as db:
        await db.execute(
            "UPDATE platforms SET passcode_locked_until = $1 WHERE id=$2", expired, pid)
    ok = await platform_svc.update_platform_wallet(
        pid, "UQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", 4242, "3333")
    assert ok, "platform correct passcode rejected after lockout expired"
    async with _db() as db:
        attempts = await db.fetchval(
            "SELECT passcode_failed_attempts FROM platforms WHERE id=$1", pid)
    assert int(attempts) == 0, f"platform failure counter not reset: {attempts}"
    print("  platform side: lockout, audit, expiry-reset all OK")

    print("\nALL LOCKOUT SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())

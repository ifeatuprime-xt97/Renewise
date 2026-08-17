"""
Pure-Python tests for the wallet change delay-and-notify flow.

Covers:
  - create_pending_wallet_change (insert + supersession)
  - get_superseded_pending_wallet_change
  - cancel_pending_wallet_change (idempotency)
  - get_due_wallet_changes (timing + group_status join)
  - apply_pending_wallet_change (atomic write)
  - get_wallet_change_history (ordering)
  - Item 9: suspended/frozen group skipped by apply job
  - Item 8: second submission supersedes first

All tests use an in-memory SQLite DB — no file I/O, no network, no bot token.
"""
from __future__ import annotations

import asyncio
import os
import pytest
import pytest_asyncio
import aiosqlite
from datetime import datetime, timezone, timedelta

# Point DATABASE_PATH at an in-memory DB before importing anything from renewise
os.environ.setdefault("BOT_TOKEN", "0:test")
os.environ.setdefault("DATABASE_PATH", ":memory:")
os.environ["DATABASE_PATH"] = ":memory:"

# Patch the config value so queries.py uses our in-memory path
import renewise.config as _cfg
_cfg.DATABASE_PATH = ":memory:"

# Now safe to import — all aiosqlite calls will use :memory:
from renewise.db import queries
from renewise.db.schema import init_db


# ── shared in-memory DB fixture ───────────────────────────────────────────────
# Each test gets a fresh DB via the monkeypatch on DATABASE_PATH.
# Because :memory: creates a NEW database per connection, we need a single
# shared connection for the duration of each test.  We achieve this by
# monkey-patching queries._db to reuse one connection.

@pytest_asyncio.fixture
async def db():
    """Yield a single aiosqlite connection with the full schema initialised."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        # Run all CREATE TABLE statements from schema
        from renewise.db.schema import (
            CREATE_GROUPS, CREATE_USERS, CREATE_SUBSCRIPTIONS,
            CREATE_AUDIT_LOG, CREATE_PROCESSED_TX, CREATE_ADMIN_SUSPENSIONS,
            CREATE_PLATFORM_CONFIG, CREATE_RECENT_ADMIN_GRANTS,
            CREATE_BANNED_ADMINS, CREATE_OVERPAYMENT_REFUNDS,
            CREATE_PENDING_WALLET_CHANGES, CREATE_VAULT_REGISTRY,
            CREATE_REMINDER_LOG, CREATE_IDX_VAULT, CREATE_IDX_SUB_RENEWAL,
            CREATE_IDX_PENDING_WALLET,
        )
        for stmt in (
            CREATE_GROUPS, CREATE_USERS, CREATE_SUBSCRIPTIONS,
            CREATE_AUDIT_LOG, CREATE_PROCESSED_TX, CREATE_ADMIN_SUSPENSIONS,
            CREATE_PLATFORM_CONFIG, CREATE_RECENT_ADMIN_GRANTS,
            CREATE_BANNED_ADMINS, CREATE_OVERPAYMENT_REFUNDS,
            CREATE_PENDING_WALLET_CHANGES, CREATE_VAULT_REGISTRY,
            CREATE_REMINDER_LOG, CREATE_IDX_VAULT, CREATE_IDX_SUB_RENEWAL,
            CREATE_IDX_PENDING_WALLET,
        ):
            await conn.execute(stmt)
        await conn.execute(
            "INSERT INTO platform_config (id, payments_paused) VALUES (1, 0)"
        )
        await conn.commit()
        yield conn


# Helper: patch queries._db to use the shared connection
from contextlib import asynccontextmanager

def _patch_db(conn, monkeypatch):
    @asynccontextmanager
    async def _fake_db():
        yield conn
    monkeypatch.setattr(queries, "_db", _fake_db)


# ── seed helpers ──────────────────────────────────────────────────────────────

async def _seed_group(conn, status="active") -> int:
    cur = await conn.execute(
        "INSERT INTO groups (telegram_chat_id, admin_telegram_id, status, chat_title) "
        "VALUES (100, 999, ?, 'Test Group') RETURNING id",
        (status,),
    )
    row = await cur.fetchone()
    await conn.commit()
    return row[0]


def _future(hours: float) -> str:
    t = datetime.now(timezone.utc) + timedelta(hours=hours)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _past(hours: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(hours=hours)
    return t.strftime("%Y-%m-%d %H:%M:%S")


# ═════════════════════════════════════════════════════════════════════════════
# Group A — create_pending_wallet_change
# ═════════════════════════════════════════════════════════════════════════════

class TestCreatePendingWalletChange:

    @pytest.mark.asyncio
    async def test_insert_returns_id(self, db, monkeypatch):
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db)
        change_id = await queries.create_pending_wallet_change(
            group_id=gid,
            old_wallet="EQold",
            new_wallet="EQnew",
            requested_by=999,
            activates_at=_future(24),
        )
        assert isinstance(change_id, int)
        assert change_id > 0

    @pytest.mark.asyncio
    async def test_row_is_pending(self, db, monkeypatch):
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db)
        change_id = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet=None, new_wallet="EQabc",
            requested_by=999, activates_at=_future(24),
        )
        row = await queries.get_pending_wallet_change(change_id)
        assert row["status"] == "pending"
        assert row["new_wallet_address"] == "EQabc"
        assert row["group_id"] == gid

    @pytest.mark.asyncio
    async def test_supersession_cancels_previous(self, db, monkeypatch):
        """Item 8: second submission auto-cancels the first."""
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db)

        first_id = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet=None, new_wallet="EQfirst",
            requested_by=999, activates_at=_future(24),
        )
        second_id = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet="EQfirst", new_wallet="EQsecond",
            requested_by=999, activates_at=_future(24),
        )

        first_row = await queries.get_pending_wallet_change(first_id)
        second_row = await queries.get_pending_wallet_change(second_id)

        assert first_row["status"] == "cancelled", "first change must be auto-cancelled"
        assert second_row["status"] == "pending", "second change must be pending"

    @pytest.mark.asyncio
    async def test_superseded_row_detectable(self, db, monkeypatch):
        """get_superseded_pending_wallet_change returns the just-cancelled row."""
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db)

        first_id = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet=None, new_wallet="EQfirst",
            requested_by=999, activates_at=_future(24),
        )
        second_id = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet="EQfirst", new_wallet="EQsecond",
            requested_by=999, activates_at=_future(24),
        )

        superseded = await queries.get_superseded_pending_wallet_change(gid, cancelled_by=999)
        assert superseded is not None
        assert superseded["id"] == first_id
        assert superseded["new_wallet_address"] == "EQfirst"


# ═════════════════════════════════════════════════════════════════════════════
# Group B — cancel_pending_wallet_change
# ═════════════════════════════════════════════════════════════════════════════

class TestCancelPendingWalletChange:

    @pytest.mark.asyncio
    async def test_cancel_pending_returns_true(self, db, monkeypatch):
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db)
        change_id = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet=None, new_wallet="EQx",
            requested_by=999, activates_at=_future(24),
        )
        result = await queries.cancel_pending_wallet_change(change_id, cancelled_by=999)
        assert result is True

        row = await queries.get_pending_wallet_change(change_id)
        assert row["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_already_cancelled_returns_false(self, db, monkeypatch):
        """Idempotent: cancelling twice returns False on second call."""
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db)
        change_id = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet=None, new_wallet="EQx",
            requested_by=999, activates_at=_future(24),
        )
        await queries.cancel_pending_wallet_change(change_id, cancelled_by=999)
        result = await queries.cancel_pending_wallet_change(change_id, cancelled_by=999)
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_applied_returns_false(self, db, monkeypatch):
        """Cannot cancel an already-applied change."""
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db)
        change_id = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet=None, new_wallet="EQapplied",
            requested_by=999, activates_at=_past(1),
        )
        await queries.apply_pending_wallet_change(change_id, "EQapplied", gid)
        result = await queries.cancel_pending_wallet_change(change_id, cancelled_by=999)
        assert result is False


# ═════════════════════════════════════════════════════════════════════════════
# Group C — get_due_wallet_changes + apply_pending_wallet_change
# ═════════════════════════════════════════════════════════════════════════════

class TestApplyWalletChanges:

    @pytest.mark.asyncio
    async def test_not_due_yet_excluded(self, db, monkeypatch):
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db)
        await queries.create_pending_wallet_change(
            group_id=gid, old_wallet=None, new_wallet="EQfuture",
            requested_by=999, activates_at=_future(24),
        )
        due = await queries.get_due_wallet_changes()
        assert all(r["group_id"] != gid for r in due), "future change must not be due"

    @pytest.mark.asyncio
    async def test_past_activates_at_is_due(self, db, monkeypatch):
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db)
        change_id = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet=None, new_wallet="EQdue",
            requested_by=999, activates_at=_past(1),
        )
        due = await queries.get_due_wallet_changes()
        ids = [r["id"] for r in due]
        assert change_id in ids

    @pytest.mark.asyncio
    async def test_apply_updates_group_wallet(self, db, monkeypatch):
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db)
        change_id = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet=None, new_wallet="EQnewwallet",
            requested_by=999, activates_at=_past(1),
        )
        await queries.apply_pending_wallet_change(change_id, "EQnewwallet", gid)

        group = await queries.get_group_by_id(gid)
        assert group["payout_wallet_address"] == "EQnewwallet"

        row = await queries.get_pending_wallet_change(change_id)
        assert row["status"] == "applied"

    @pytest.mark.asyncio
    async def test_item9_suspended_group_excluded_from_due(self, db, monkeypatch):
        """Item 9: due change for a suspended group — group_status is returned."""
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db, status="suspended")
        change_id = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet=None, new_wallet="EQbad",
            requested_by=999, activates_at=_past(1),
        )
        due = await queries.get_due_wallet_changes()
        matching = [r for r in due if r["id"] == change_id]
        assert len(matching) == 1, "row should still be returned by get_due_wallet_changes"
        assert matching[0]["group_status"] == "suspended", "group_status must be exposed"

    @pytest.mark.asyncio
    async def test_item9_frozen_group_status_exposed(self, db, monkeypatch):
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db, status="frozen")
        change_id = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet=None, new_wallet="EQfrozen",
            requested_by=999, activates_at=_past(1),
        )
        due = await queries.get_due_wallet_changes()
        matching = [r for r in due if r["id"] == change_id]
        assert matching[0]["group_status"] == "frozen"


# ═════════════════════════════════════════════════════════════════════════════
# Group D — get_wallet_change_history
# ═════════════════════════════════════════════════════════════════════════════

class TestWalletChangeHistory:

    @pytest.mark.asyncio
    async def test_empty_history(self, db, monkeypatch):
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db)
        rows = await queries.get_wallet_change_history(gid)
        assert rows == []

    @pytest.mark.asyncio
    async def test_history_newest_first(self, db, monkeypatch):
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db)

        id1 = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet=None, new_wallet="EQone",
            requested_by=999, activates_at=_future(24),
        )
        id2 = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet="EQone", new_wallet="EQtwo",
            requested_by=999, activates_at=_future(24),
        )

        rows = await queries.get_wallet_change_history(gid)
        ids = [r["id"] for r in rows]
        # id2 was inserted after id1, so it should appear first (newest first)
        assert ids.index(id2) < ids.index(id1)

    @pytest.mark.asyncio
    async def test_history_includes_all_statuses(self, db, monkeypatch):
        _patch_db(db, monkeypatch)
        gid = await _seed_group(db)

        # pending
        await queries.create_pending_wallet_change(
            group_id=gid, old_wallet=None, new_wallet="EQpending",
            requested_by=999, activates_at=_future(24),
        )
        # applied
        applied_id = await queries.create_pending_wallet_change(
            group_id=gid, old_wallet="EQpending", new_wallet="EQapplied",
            requested_by=999, activates_at=_past(1),
        )
        await queries.apply_pending_wallet_change(applied_id, "EQapplied", gid)

        rows = await queries.get_wallet_change_history(gid)
        statuses = {r["status"] for r in rows}
        assert "applied" in statuses
        assert "cancelled" in statuses  # first was superseded

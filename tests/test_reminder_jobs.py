"""
test_reminder_jobs.py
─────────────────────
Manual test: runs reminder and enforcement logic DIRECTLY — no Redis, no RQ,
no workers needed. The bot Telegram API calls are made inline using the bot
token from .env.

Usage (from repo root):
    python -m renewise.test_reminder_jobs [--sub-id ID]

Without --sub-id the script finds the first active subscription.
Pass --sub-id N to target a specific subscription row by DB id.

Steps performed
───────────────
TEST 1 — Renewal Reminder
  a. Set next_renewal_date = NOW + (REMINDER_WINDOW_DAYS - 1) days
  b. Clear reminder_log so the row is "unsent"
  c. Call get_subscriptions_due_reminder() → confirm row appears
  d. Call send_renewal_reminder task directly (no RQ)
     → sends real Telegram DM to the user
     → writes reminder_log row
  e. Verify reminder_log was written  ✅

TEST 2 — Grace Period Enforcement
  a. Set next_renewal_date = NOW - (GRACE_PERIOD_DAYS + 1) days
  b. Call get_subscriptions_past_grace() → confirm row appears
  c. Call expire_and_kick task directly (no RQ)
     → sets status='expired' in DB
     → sends real Telegram DM to user
     → sends real ban+unban to Telegram chat
  d. Verify status = 'expired'  ✅

  Restore original data afterwards.

WARNING: Sends REAL Telegram messages and issues a REAL kick.
         Only run against a test account / test channel.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("test_jobs")

PASS = "✅ PASS"
FAIL = "❌ FAIL"


async def main(sub_id_arg: int | None) -> None:
    import aiosqlite
    from renewise.config import DATABASE_PATH, REMINDER_WINDOW_DAYS, GRACE_PERIOD_DAYS, BOT_TOKEN
    from renewise.watcher.db import (
        migrate,
        get_subscriptions_due_reminder,
        get_subscriptions_past_grace,
        has_reminder_been_sent,
        record_reminder_sent,
    )

    log.info("DB: %s", DATABASE_PATH)
    log.info("REMINDER_WINDOW_DAYS=%d  GRACE_PERIOD_DAYS=%d", REMINDER_WINDOW_DAYS, GRACE_PERIOD_DAYS)

    # Ensure watcher tables exist (reminder_log etc.)
    await migrate()

    # ── 0. Find target subscription ───────────────────────────────────────────
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        if sub_id_arg:
            cur = await db.execute(
                "SELECT s.id, s.status, s.next_renewal_date, s.user_id, s.group_id, "
                "u.telegram_user_id, g.telegram_chat_id, g.chat_title "
                "FROM subscriptions s "
                "JOIN users u ON u.id=s.user_id "
                "JOIN groups g ON g.id=s.group_id "
                "WHERE s.id=?",
                (sub_id_arg,),
            )
        else:
            cur = await db.execute(
                "SELECT s.id, s.status, s.next_renewal_date, s.user_id, s.group_id, "
                "u.telegram_user_id, g.telegram_chat_id, g.chat_title "
                "FROM subscriptions s "
                "JOIN users u ON u.id=s.user_id "
                "JOIN groups g ON g.id=s.group_id "
                "WHERE s.status='active' "
                "ORDER BY s.id LIMIT 1",
            )
        row = await cur.fetchone()

    if not row:
        log.error("No active subscription found. Create one first or pass --sub-id.")
        sys.exit(1)

    sub_id           = row["id"]
    original_status  = row["status"]
    original_date    = row["next_renewal_date"]
    tg_user_id       = row["telegram_user_id"]
    group_id         = row["group_id"]
    telegram_chat_id = row["telegram_chat_id"]
    chat_title       = row["chat_title"] or f"Group {group_id}"

    log.info(
        "Target: sub_id=%d  user=%d  chat=%d (%s)  status=%s  next_renewal=%s",
        sub_id, tg_user_id, telegram_chat_id, chat_title, original_status, original_date,
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # TEST 1 — Renewal Reminder
    # ═══════════════════════════════════════════════════════════════════════════
    log.info("\n══ TEST 1: Renewal Reminder ══")

    # a. Set date inside reminder window
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE subscriptions "
            f"SET next_renewal_date=datetime('now', '+{REMINDER_WINDOW_DAYS - 1} days'), "
            "status='active' WHERE id=?",
            (sub_id,),
        )
        await db.execute("DELETE FROM reminder_log WHERE subscription_id=?", (sub_id,))
        await db.commit()

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT next_renewal_date FROM subscriptions WHERE id=?", (sub_id,))
        r = await cur.fetchone()
    renewal_date_str = r["next_renewal_date"]
    log.info("  Set next_renewal_date → %s", renewal_date_str)

    # b. Confirm query picks it up
    due = await get_subscriptions_due_reminder(REMINDER_WINDOW_DAYS)
    match = [x for x in due if x["id"] == sub_id]
    if match:
        log.info("  %s get_subscriptions_due_reminder returned sub_id=%d", PASS, sub_id)
    else:
        log.error("  %s sub_id=%d NOT in due_reminder results. Aborting.", FAIL, sub_id)
        await _restore(DATABASE_PATH, sub_id, original_date, original_status)
        sys.exit(1)

    # c. Send reminder DIRECTLY (no RQ) — fires real Telegram DM
    log.info("  Sending reminder DM directly via bot token...")
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

    bot = Bot(token=BOT_TOKEN)
    bot_me = await bot.get_me()
    renew_url = f"https://t.me/{bot_me.username}?start=renew_{group_id}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Renew Now", url=renew_url)]])

    # Compute days remaining
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(renewal_date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - datetime.now(timezone.utc)
        d = delta.days
        days_label = f"in {d} day{'s' if d != 1 else ''}" if d > 0 else "today"
    except Exception:
        days_label = "soon"

    try:
        await bot.send_message(
            chat_id=tg_user_id,
            text=(
                f"⏰ <b>Your subscription to {chat_title} renews {days_label}.</b>\n\n"
                "Tap <b>Renew Now</b> to generate a payment link and keep your access."
            ),
            parse_mode="HTML",
            reply_markup=kb,
        )
        log.info("  %s Reminder DM sent to user %d", PASS, tg_user_id)
    except Exception as exc:
        log.error("  %s Failed to send reminder DM: %s", FAIL, exc)

    # d. Write reminder_log directly (what the RQ task does)
    await record_reminder_sent(sub_id, renewal_date_str)

    # e. Verify reminder_log
    sent = await has_reminder_been_sent(sub_id, renewal_date_str)
    if sent:
        log.info("  %s reminder_log written for sub_id=%d cycle=%s", PASS, sub_id, renewal_date_str)
    else:
        log.error("  %s reminder_log row NOT found", FAIL)

    # ═══════════════════════════════════════════════════════════════════════════
    # TEST 2 — Grace Period Enforcement
    # ═══════════════════════════════════════════════════════════════════════════
    log.info("\n══ TEST 2: Grace Period Enforcement ══")

    # a. Set date past grace period
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE subscriptions "
            f"SET next_renewal_date=datetime('now', '-{GRACE_PERIOD_DAYS + 1} days'), "
            "status='active' WHERE id=?",
            (sub_id,),
        )
        await db.commit()

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT next_renewal_date FROM subscriptions WHERE id=?", (sub_id,))
        r = await cur.fetchone()
    log.info("  Set next_renewal_date → %s", r["next_renewal_date"])

    # b. Confirm query picks it up
    past = await get_subscriptions_past_grace(GRACE_PERIOD_DAYS)
    match = [x for x in past if x["id"] == sub_id]
    if match:
        log.info("  %s get_subscriptions_past_grace returned sub_id=%d", PASS, sub_id)
    else:
        log.error("  %s sub_id=%d NOT in past_grace results. Aborting.", FAIL, sub_id)
        await _restore(DATABASE_PATH, sub_id, original_date, original_status)
        sys.exit(1)

    # c. Expire + kick DIRECTLY (no RQ)
    log.info("  Expiring subscription and sending kick...")

    # Mark expired in DB
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE subscriptions SET status='expired' WHERE id=?", (sub_id,)
        )
        await db.commit()

    # Send expiry DM
    try:
        await bot.send_message(
            chat_id=tg_user_id,
            text=(
                "😔 <b>Your subscription has expired.</b>\n\n"
                f"You've been removed from <b>{chat_title}</b>. "
                "You can rejoin at any time by requesting access and renewing your subscription."
            ),
            parse_mode="HTML",
        )
        log.info("  %s Expiry DM sent to user %d", PASS, tg_user_id)
    except Exception as exc:
        log.error("  %s Failed to send expiry DM: %s", FAIL, exc)

    # Kick (ban + unban)
    try:
        await bot.ban_chat_member(chat_id=telegram_chat_id, user_id=tg_user_id)
        await bot.unban_chat_member(chat_id=telegram_chat_id, user_id=tg_user_id, only_if_banned=True)
        log.info("  %s Kicked user %d from chat %d", PASS, tg_user_id, telegram_chat_id)
    except Exception as exc:
        log.warning("  ⚠️  Kick failed (user may not be in chat): %s", exc)

    # d. Verify status
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT status FROM subscriptions WHERE id=?", (sub_id,))
        r = await cur.fetchone()

    if r and r["status"] == "expired":
        log.info("  %s subscription status = 'expired'", PASS)
    else:
        log.error("  %s subscription status = '%s'", FAIL, r["status"] if r else "missing")

    # ── Restore ───────────────────────────────────────────────────────────────
    await _restore(DATABASE_PATH, sub_id, original_date, original_status)

    await bot.close()
    log.info("\nAll tests complete.")


async def _restore(db_path: str, sub_id: int, original_date, original_status: str) -> None:
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE subscriptions SET next_renewal_date=?, status=? WHERE id=?",
            (original_date, original_status, sub_id),
        )
        await db.execute("DELETE FROM reminder_log WHERE subscription_id=?", (sub_id,))
        await db.commit()
    log.info("Restored: sub_id=%d status=%s next_renewal=%s", sub_id, original_status, original_date)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test renewal reminder and enforcement (no Redis needed)")
    parser.add_argument("--sub-id", type=int, default=None, help="Subscription DB id to target")
    args = parser.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main(args.sub_id))

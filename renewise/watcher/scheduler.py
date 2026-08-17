"""
renewise/watcher/scheduler.py

APScheduler cron jobs.

Jobs
────
renewal_reminder_job  — runs hourly; finds subscriptions expiring within
                        REMINDER_WINDOW_DAYS and enqueues reminder tasks.
grace_enforcement_job — runs hourly; finds subscriptions past
                        next_renewal_date + GRACE_PERIOD_DAYS and enqueues
                        expire_and_kick tasks.

Run the scheduler:
    python -m renewise.watcher.scheduler

The scheduler is a separate process from the workers.  It only enqueues jobs;
the workers do the actual work.  This separation means the scheduler can be
restarted without affecting in-flight jobs.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import redis as redis_sync
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from rq import Queue

from renewise.watcher.config import (
    REDIS_URL,
    RQ_QUEUE_NAME,
    GRACE_PERIOD_DAYS,
    REMINDER_WINDOW_DAYS,
)
from renewise.watcher.db import (
    get_subscriptions_due_reminder,
    get_subscriptions_past_grace,
    migrate,
)
from renewise.watcher.tasks import send_renewal_reminder, expire_and_kick, _retry
from renewise.db.queries import delete_stale_pending_subscriptions, get_due_wallet_changes, apply_pending_wallet_change, audit

log = logging.getLogger(__name__)


def _queue() -> Queue:
    return Queue(RQ_QUEUE_NAME, connection=redis_sync.from_url(REDIS_URL))


# ── Job: renewal reminders ────────────────────────────────────────────────────

async def renewal_reminder_job() -> None:
    """
    Find subscriptions expiring within REMINDER_WINDOW_DAYS and enqueue
    a reminder task for each one that hasn't been reminded this cycle.
    """
    log.info("renewal_reminder_job: scanning (window=%d days)", REMINDER_WINDOW_DAYS)
    rows = await get_subscriptions_due_reminder(REMINDER_WINDOW_DAYS)
    if not rows:
        log.info("renewal_reminder_job: nothing to remind")
        return

    q = _queue()
    enqueued = 0
    for row in rows:
        q.enqueue(
            send_renewal_reminder,
            kwargs={
                "subscription_id":   row["id"],
                "telegram_user_id":  row["telegram_user_id"],
                "group_id":          row["group_id"],
                "renewal_cycle":     row["next_renewal_date"],
            },
            retry=_retry(),
            job_timeout=60,
        )
        enqueued += 1

    log.info("renewal_reminder_job: enqueued %d reminders", enqueued)


# ── Job: grace period enforcement ─────────────────────────────────────────────

async def grace_enforcement_job() -> None:
    """
    Find subscriptions past next_renewal_date + GRACE_PERIOD_DAYS and enqueue
    expire_and_kick for each one.  Comped subscriptions and group admins are
    excluded by the DB query in watcher/db.py.
    """
    log.info("grace_enforcement_job: scanning (grace=%d days)", GRACE_PERIOD_DAYS)
    rows = await get_subscriptions_past_grace(GRACE_PERIOD_DAYS)
    if not rows:
        log.info("grace_enforcement_job: nothing to expire")
        return

    q = _queue()
    enqueued = 0
    for row in rows:
        q.enqueue(
            expire_and_kick,
            kwargs={
                "subscription_id":   row["id"],
                "telegram_user_id":  row["telegram_user_id"],
                "telegram_chat_id":  row["telegram_chat_id"],
                "group_id":          row["group_id"],
            },
            retry=_retry(),
            job_timeout=60,
        )
        enqueued += 1

    log.info("grace_enforcement_job: enqueued %d expirations", enqueued)


# ── Job: apply due wallet changes ────────────────────────────────────────────

async def apply_wallet_changes_job() -> None:
    """
    Apply any pending wallet changes whose activates_at has passed.

    For each due row:
    1. Apply atomically (groups.payout_wallet_address + row status='applied').
    2. Audit the application.
    3. DM the admin to confirm the change went live.

    The bot instance is not available in the scheduler process, so the DM is
    sent via a fresh Bot() call using BOT_TOKEN from config.
    """
    from renewise.config import BOT_TOKEN
    from telegram import Bot

    rows = await get_due_wallet_changes()
    if not rows:
        return

    log.info("apply_wallet_changes_job: %d change(s) due", len(rows))
    bot = Bot(BOT_TOKEN)

    from renewise.db.queries import cancel_pending_wallet_change

    for row in rows:
        change_id   = row["id"]
        group_id    = row["group_id"]
        new_wallet  = row["new_wallet_address"]
        admin_id    = row["admin_telegram_id"]
        group_title = row["chat_title"] or str(group_id)
        group_status = row["group_status"]

        # Item 9 — skip and auto-cancel if the group is no longer in good standing.
        # A suspended or frozen group should not receive a wallet update; the
        # pending change is cancelled so it doesn't silently apply if the group
        # is later reinstated (admin must resubmit explicitly).
        if group_status in ("suspended", "frozen"):
            await cancel_pending_wallet_change(change_id, cancelled_by=0)  # 0 = system
            await audit(
                group_id,
                "wallet_change_auto_cancelled_group_not_active",
                admin_id,
                {"change_id": change_id, "group_status": group_status},
            )
            log.warning(
                "apply_wallet_changes_job: skipped change_id=%d — group %d is %s",
                change_id, group_id, group_status,
            )
            continue

        await apply_pending_wallet_change(change_id, new_wallet, group_id)
        await audit(
            group_id,
            "wallet_change_applied",
            admin_id,
            {"change_id": change_id, "new_wallet": new_wallet},
        )

        short = f"{new_wallet[:6]}...{new_wallet[-4:]}" if len(new_wallet) > 12 else new_wallet
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=(
                    f"✅ <b>Wallet change applied.</b>\n\n"
                    f"Your payout wallet for <b>{group_title}</b> has been updated to:\n"
                    f"<code>{new_wallet}</code>\n\n"
                    f"New payments will be sent to this address."
                ),
                parse_mode="HTML",
            )
        except Exception as exc:
            log.warning("apply_wallet_changes_job: could not DM admin %d: %s", admin_id, exc)

        log.info(
            "apply_wallet_changes_job: applied change_id=%d group=%d wallet=%s",
            change_id, group_id, short,
        )


# ── Job: stale pending cleanup ───────────────────────────────────────────────

async def stale_pending_cleanup_job() -> None:
    """Delete pending subscriptions older than 12 hours with no payment."""
    deleted = await delete_stale_pending_subscriptions(older_than_hours=12)
    if deleted:
        log.info("stale_pending_cleanup_job: deleted %d stale pending subscription(s)", deleted)


# ── Scheduler setup ───────────────────────────────────────────────────────────

async def run_scheduler() -> None:
    await migrate()

    scheduler = AsyncIOScheduler()

    # Renewal reminders — every hour at :05 past the hour
    scheduler.add_job(
        renewal_reminder_job,
        trigger="cron",
        minute=5,
        id="renewal_reminder",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # Grace enforcement — every hour at :15 past the hour
    scheduler.add_job(
        grace_enforcement_job,
        trigger="cron",
        minute=15,
        id="grace_enforcement",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # Stale pending cleanup — every hour at :30 past the hour
    scheduler.add_job(
        stale_pending_cleanup_job,
        trigger="cron",
        minute=30,
        id="stale_pending_cleanup",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # Wallet change apply — every 15 minutes
    scheduler.add_job(
        apply_wallet_changes_job,
        trigger="cron",
        minute="0,15,30,45",
        id="apply_wallet_changes",
        replace_existing=True,
        misfire_grace_time=300,
    )

    scheduler.start()
    log.info("Scheduler started (renewal_reminder @:05, grace_enforcement @:15)")

    # Run both jobs immediately on startup so we don't wait up to an hour
    await renewal_reminder_job()
    await grace_enforcement_job()
    await stale_pending_cleanup_job()
    await apply_wallet_changes_job()

    # Keep running until interrupted.
    # Use asyncio.get_running_loop() (not the deprecated get_event_loop()) and
    # register signal handlers only on non-Windows platforms — signal.signal()
    # with SIGTERM doesn't work reliably on Windows and raises ValueError when
    # called outside the main thread.
    loop = asyncio.get_running_loop()
    stop: asyncio.Future = loop.create_future()

    if sys.platform != "win32":
        def _shutdown(sig, frame):
            log.info("Scheduler received signal %s, shutting down", sig)
            scheduler.shutdown(wait=False)
            if not stop.done():
                loop.call_soon_threadsafe(stop.set_result, None)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)
    else:
        # On Windows, rely on KeyboardInterrupt / CancelledError propagating
        # naturally through asyncio.run() — no SIGTERM support needed.
        try:
            await stop
        except asyncio.CancelledError:
            log.info("Scheduler cancelled, shutting down.")
            scheduler.shutdown(wait=False)
            return

    await stop


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )
    asyncio.run(run_scheduler())


if __name__ == "__main__":
    main()

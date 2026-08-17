"""
tests/load_test.py

Load test: simulates 100+ concurrent transaction events hitting the RQ queue
and verifies the pipeline drains correctly without dropped or duplicated events.

What it tests
─────────────
1. Enqueue 120 unique payment events simultaneously (simulating a burst).
2. Wait for all jobs to drain from the queue.
3. Assert every subscription was activated exactly once (no duplicates).
4. Assert no jobs remain in the failed queue.
5. Print timing and throughput stats.

Prerequisites
─────────────
- Redis running (REDIS_URL in .env or environment)
- At least one RQ worker running:
      python -m renewise.watcher.worker --concurrency 4
- The test database must exist (run the bot once or call init_db() + migrate())

Run:
    python tests/load_test.py

Or with pytest (slower due to setup overhead):
    pytest tests/load_test.py -v -s
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
import logging
from pathlib import Path

# Ensure project root is on sys.path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

import redis as redis_sync
from rq import Queue
from dotenv import load_dotenv

load_dotenv()

from renewise.watcher.config import REDIS_URL, RQ_QUEUE_NAME
from renewise.watcher.db import migrate, register_vault
from renewise.watcher.tasks import process_payment, _retry
from renewise.db.schema import init_db
from renewise.db.queries import (
    upsert_user,
    upsert_group,
    create_subscription,
    get_subscription,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("load_test")

# ── Config ────────────────────────────────────────────────────────────────────

NUM_PAYMENTS    = 120   # number of concurrent payment events
DRAIN_TIMEOUT   = 120   # seconds to wait for queue to drain
PRICE_NANO      = 1_000_000_000  # 1 TON per subscription (test value)
BUYER_FEE_BPS   = 200
AMOUNT_NANO     = PRICE_NANO + (PRICE_NANO * BUYER_FEE_BPS // 10000)  # price + buyer fee


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_vault_address(i: int) -> str:
    """Generate a unique fake vault address for each test subscription."""
    return f"EQtest{'0' * 60}{i:04d}"[:66]


def _fake_tx_hash(i: int) -> str:
    """Generate a unique fake tx hash for each payment event."""
    return f"txhash_{uuid.uuid4().hex}_{i:04d}"


async def _setup_test_data(n: int) -> list[dict]:
    """
    Create n test users, groups, subscriptions, and vault registrations.
    Returns a list of dicts with the context needed to enqueue each job.
    """
    await init_db()
    await migrate()

    events = []
    for i in range(n):
        # Use unique telegram IDs in the load-test range (9_000_000+)
        tg_user_id  = 9_000_000 + i
        tg_chat_id  = 8_000_000 + i

        user_db_id  = await upsert_user(tg_user_id)
        group_db_id = await upsert_group(tg_chat_id, admin_telegram_id=1)

        # Set price on the group row
        import aiosqlite
        from renewise.config import DATABASE_PATH
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "UPDATE groups SET price=?, payout_wallet_address=? WHERE id=?",
                (PRICE_NANO / 1e9, "EQDummyAdminWallet" + "0" * 48, group_db_id),
            )
            await db.commit()

        sub_id = await create_subscription(
            user_id=user_db_id,
            group_id=group_db_id,
            price_locked_in=PRICE_NANO / 1e9,
        )

        vault_address = _fake_vault_address(i)
        await register_vault(
            vault_address=vault_address,
            subscription_id=sub_id,
            user_id=user_db_id,
            group_id=group_db_id,
        )

        events.append({
            "vault_address": vault_address,
            "tx_hash":       _fake_tx_hash(i),
            "amount_nano":   AMOUNT_NANO,
            "sub_id":        sub_id,
            "user_db_id":    user_db_id,
            "group_db_id":   group_db_id,
        })

    return events


def _enqueue_all(events: list[dict], q: Queue) -> list:
    """Enqueue all payment jobs simultaneously and return the job list."""
    jobs = []
    for ev in events:
        job = q.enqueue(
            process_payment,
            kwargs={
                "vault_address": ev["vault_address"],
                "tx_hash":       ev["tx_hash"],
                "amount_nano":   ev["amount_nano"],
            },
            retry=_retry(),
            job_timeout=60,
        )
        jobs.append(job)
    return jobs


def _wait_for_drain(q: Queue, failed_q, job_ids: list[str], timeout: int) -> tuple[int, int, int]:
    """
    Poll until all jobs are finished or timeout.
    Returns (finished, failed, still_pending).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = sum(
            1 for jid in job_ids
            if q.fetch_job(jid) is not None and q.fetch_job(jid).get_status() in ("queued", "started", "deferred")
        )
        if pending == 0:
            break
        time.sleep(1)

    finished = sum(
        1 for jid in job_ids
        if (j := q.fetch_job(jid)) is not None and j.get_status() == "finished"
    )
    failed = len(failed_q.jobs)
    pending = sum(
        1 for jid in job_ids
        if (j := q.fetch_job(jid)) is not None and j.get_status() in ("queued", "started")
    )
    return finished, failed, pending


async def _verify_results(events: list[dict]) -> tuple[int, int, int]:
    """
    Check DB state: count activated, pending, and duplicated subscriptions.
    Returns (activated, still_pending, duplicates).
    """
    import aiosqlite
    from renewise.config import DATABASE_PATH

    activated = 0
    still_pending = 0
    duplicates = 0

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        for ev in events:
            cur = await db.execute(
                "SELECT status FROM subscriptions WHERE id=?", (ev["sub_id"],)
            )
            row = await cur.fetchone()
            if row and row["status"] == "active":
                activated += 1
            else:
                still_pending += 1

        # Check for duplicate processed_tx_hashes entries (should be impossible)
        cur = await db.execute(
            "SELECT tx_hash, COUNT(*) as cnt FROM processed_tx_hashes GROUP BY tx_hash HAVING cnt > 1"
        )
        dup_rows = await cur.fetchall()
        duplicates = len(dup_rows)

    return activated, still_pending, duplicates


# ── Main ──────────────────────────────────────────────────────────────────────

async def run_load_test() -> None:
    log.info("=" * 60)
    log.info("renewise load test: %d concurrent payment events", NUM_PAYMENTS)
    log.info("=" * 60)

    # Check Redis connectivity
    try:
        r = redis_sync.from_url(REDIS_URL)
        r.ping()
        log.info("Redis: connected (%s)", REDIS_URL)
    except Exception as exc:
        log.error("Redis not available: %s", exc)
        sys.exit(1)

    q        = Queue(RQ_QUEUE_NAME, connection=r)
    failed_q = Queue("failed",      connection=r)

    # Check workers are running
    from rq import Worker
    workers = Worker.all(connection=r)
    if not workers:
        log.warning(
            "No RQ workers detected! Start workers first:\n"
            "  python -m renewise.watcher.worker --concurrency 4"
        )

    # Setup
    log.info("Setting up %d test subscriptions…", NUM_PAYMENTS)
    t0 = time.time()
    events = await _setup_test_data(NUM_PAYMENTS)
    log.info("Setup done in %.2fs", time.time() - t0)

    # Enqueue all jobs simultaneously
    log.info("Enqueueing %d jobs simultaneously…", NUM_PAYMENTS)
    t_enqueue = time.time()
    jobs = _enqueue_all(events, q)
    enqueue_time = time.time() - t_enqueue
    log.info("Enqueued %d jobs in %.3fs (%.0f jobs/s)", NUM_PAYMENTS, enqueue_time, NUM_PAYMENTS / enqueue_time)

    # Duplicate-fire test: enqueue the same events again (should be no-ops)
    log.info("Enqueueing same events again (idempotency test)…")
    _enqueue_all(events, q)
    log.info("Duplicate events enqueued — workers should ignore them")

    # Wait for drain
    log.info("Waiting for queue to drain (timeout=%ds)…", DRAIN_TIMEOUT)
    t_drain = time.time()
    job_ids = [j.id for j in jobs]
    finished, failed_count, still_pending = _wait_for_drain(q, failed_q, job_ids, DRAIN_TIMEOUT)
    drain_time = time.time() - t_drain

    # Verify DB state
    activated, still_pending_db, duplicates = await _verify_results(events)

    # ── Results ───────────────────────────────────────────────────────────────
    sep = "─" * 60
    log.info(sep)
    log.info("LOAD TEST RESULTS")
    log.info(sep)
    log.info("  Events enqueued (first batch):  %d", NUM_PAYMENTS)
    log.info("  Events enqueued (duplicate):    %d  (idempotency test)", NUM_PAYMENTS)
    log.info("  Enqueue time:                   %.3fs", enqueue_time)
    log.info("  Drain time:                     %.2fs", drain_time)
    log.info("  Throughput:                     %.1f payments/s", NUM_PAYMENTS / drain_time if drain_time > 0 else 0)
    log.info(sep)
    log.info("  Subscriptions activated:        %d / %d", activated, NUM_PAYMENTS)
    log.info("  Subscriptions still pending:    %d", still_pending_db)
    log.info("  Duplicate tx_hash entries:      %d  (must be 0)", duplicates)
    log.info("  Failed jobs:                    %d  (must be 0)", failed_count)
    log.info("  Jobs still in queue:            %d  (must be 0)", still_pending)
    log.info(sep)

    # ── Pass/fail ─────────────────────────────────────────────────────────────
    passed = (
        activated == NUM_PAYMENTS
        and duplicates == 0
        and failed_count == 0
        and still_pending == 0
    )
    if passed:
        log.info("✅ LOAD TEST PASSED")
    else:
        log.error("❌ LOAD TEST FAILED")
        if activated < NUM_PAYMENTS:
            log.error("  %d subscriptions were NOT activated", NUM_PAYMENTS - activated)
        if duplicates > 0:
            log.error("  %d duplicate tx_hash entries found — idempotency broken!", duplicates)
        if failed_count > 0:
            log.error("  %d jobs in failed queue — check worker logs", failed_count)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_load_test())

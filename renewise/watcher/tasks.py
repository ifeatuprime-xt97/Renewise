"""
renewise/watcher/tasks.py

RQ task definitions.  Every function here is a unit of work that runs inside
an RQ worker process.  All tasks are:

  • Idempotent  — safe to run more than once for the same input.
  • Retry-safe  — the processed_tx_hashes check is the outermost guard, so a
                  partial failure followed by a retry never double-credits.
  • Async-aware — RQ workers are synchronous; we use asyncio.run() to call
                  async DB helpers.  Each task creates its own event loop so
                  there are no shared-state issues across concurrent workers.

Retry strategy
──────────────
RQ's built-in retry uses rq.Retry(max=N, interval=[...]).  We configure
exponential backoff via JOB_RETRY_BASE_DELAY: delays are [10, 20, 40, 80, 160]
seconds for up to JOB_MAX_RETRIES attempts.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import redis as redis_sync
from rq import Retry

from renewise.watcher.config import (
    REDIS_URL,
    JOB_MAX_RETRIES,
    JOB_RETRY_BASE_DELAY,
    BOT_ACTIONS_CHANNEL,
)

log = logging.getLogger(__name__)


# ── Retry config ──────────────────────────────────────────────────────────────

def _retry() -> Retry:
    delays = [JOB_RETRY_BASE_DELAY * (2 ** i) for i in range(JOB_MAX_RETRIES)]
    return Retry(max=JOB_MAX_RETRIES, interval=delays)


# ── Bot action publisher ──────────────────────────────────────────────────────

def _publish_bot_action(action: str, payload: dict[str, Any]) -> None:
    """
    Publish a bot action to the Redis pub/sub channel.
    The bot process (renewise/watcher/bot_listener.py) subscribes and executes
    the corresponding Telegram API call.
    """
    r = redis_sync.from_url(REDIS_URL)
    r.publish(BOT_ACTIONS_CHANNEL, json.dumps({"action": action, **payload}))


# ── Task: process a confirmed vault payment ───────────────────────────────────

def process_payment(vault_address: str, tx_hash: str, amount_nano: int) -> None:
    """
    Core payment processing task.  Enqueued by the webhook receiver or poller
    for every new confirmed transaction on a vault address.

    Steps (all-or-nothing from the idempotency perspective):
      1. Check processed_tx_hashes — exit immediately if already done.
      2. Look up vault_registry to find (subscription_id, user_id, group_id).
      3. Verify amount >= required payment for this subscription.
      4. Atomically insert into processed_tx_hashes (INSERT OR IGNORE).
         If rowcount == 0 a concurrent worker beat us — exit.
      5. Activate the subscription in the DB.
      6. Publish bot action: approve join request + send welcome DM.
    """
    log.info(
        "process_payment start | vault=%s tx=%s amount=%d",
        vault_address, tx_hash, amount_nano,
    )

    async def _run() -> bool:
        """Returns True only if actual work was done (new payment processed)."""
        from renewise.watcher.db import (
            get_vault_registration,
            mark_tx_processed,
            is_tx_processed,
        )
        from renewise.db.queries import (
            get_group_by_id,
            activate_subscription,
            audit,
        )

        # Step 1 — fast pre-check (avoids DB write on obvious duplicates)
        if await is_tx_processed(tx_hash):
            log.info("process_payment skip (already processed) | tx=%s", tx_hash)
            return False

        # Step 2 — resolve vault address to subscription context
        reg = await get_vault_registration(vault_address)
        if not reg:
            log.warning(
                "process_payment: unknown vault %s tx=%s — no registry entry",
                vault_address, tx_hash,
            )
            return False

        sub_id   = reg["subscription_id"]
        user_id  = reg["user_id"]
        group_id = reg["group_id"]

        from renewise.db.queries import get_subscription
        sub_before = await get_subscription(user_id, group_id)
        is_renewal = sub_before is not None and sub_before["status"] == "active"

        if not sub_before:
            log.warning("process_payment: no subscription found for user_id=%s group_id=%s", user_id, group_id)
            return False

        # Step 3 — verify amount
        group = await get_group_by_id(group_id)
        if group:
            if sub_before["required_nano_amount"]:
                required = sub_before["required_nano_amount"]
                # Sanity-check: if the incoming tx already meets or exceeds the stored
                # required_nano_amount, the stored value is stale (generated at a different
                # exchange rate). Discard it and fall back to re-deriving from price so we
                # never fire a false "insufficient" for a payment that clearly covers the price.
                if amount_nano >= required:
                    log.debug(
                        "process_payment: amount_nano=%d >= stored required=%d — "
                        "stale required_nano_amount, re-deriving | vault=%s tx=%s",
                        amount_nano, required, vault_address, tx_hash,
                    )
                    required = None
            else:
                required = None

            if required is None:
                from renewise.utils.coingecko import get_ton_usd_price
                price_usd = sub_before["price_locked_in"]
                ton_usd_rate = await get_ton_usd_price()
                price_gram = price_usd / ton_usd_rate
                price_nano = round(price_gram * 1_000_000_000)
                # Per-group override takes precedence; NULL means use global .env default
                buyer_fee_bps = (
                    group["buyer_fee_bps"]
                    if group["buyer_fee_bps"] is not None
                    else int(
                        __import__("renewise.config", fromlist=["BUYER_FEE_BPS"]).BUYER_FEE_BPS
                    )
                )
                buyer_fee = price_nano * buyer_fee_bps // 10000
                required  = price_nano + buyer_fee

            if amount_nano < required:
                log.warning(
                    "process_payment: insufficient amount %d < %d | vault=%s tx=%s",
                    amount_nano, required, vault_address, tx_hash,
                )

                # If the subscription is already active, this is a residual
                # internal contract message after the real payment. Mark it
                # processed silently — no partial-payment DM for active members.
                from renewise.db.queries import get_subscription as _get_sub
                current_sub = await _get_sub(user_id, group_id)
                if current_sub and current_sub["status"] == "active":
                    log.info(
                        "process_payment: residual tx after confirmed payment, marking silently "
                        "| vault=%s tx=%s amount=%d",
                        vault_address, tx_hash, amount_nano,
                    )
                    await mark_tx_processed(tx_hash, sub_id)
                    return False

                # Do NOT mark as processed — leave the tx retryable.
                # Notify the user so they know what happened.
                _publish_bot_action("insufficient_payment", {
                    "user_id":       user_id,
                    "group_id":      group_id,
                    "amount_nano":   amount_nano,
                    "required_nano": required,
                })
                await audit(
                    group_id=group_id,
                    action="payment_insufficient",
                    actor_id=user_id,
                    details={
                        "tx_hash": tx_hash,
                        "amount_nano": amount_nano,
                        "required_nano": required,
                        "shortfall_nano": required - amount_nano,
                    },
                )
                return False

        # Step 3.5 — Kill switch check
        from renewise.db.queries import is_payments_paused
        if await is_payments_paused():
            log.warning("process_payment: global kill switch is active. Pausing processing for tx=%s", tx_hash)
            return False

        # Step 4 — atomic idempotency claim
        claimed = await mark_tx_processed(tx_hash, sub_id)
        if not claimed:
            log.info("process_payment: lost race on idempotency claim | tx=%s", tx_hash)
            return False

        # Step 4.5 — removed duplicate is_renewal logic because we now do it at Step 2

        # Step 5 — activate subscription
        new_renewal_date = await activate_subscription(user_id, group_id, tx_hash=tx_hash)
        log.info(
            "process_payment: subscription activated | sub_id=%d user_id=%d group_id=%d tx=%s is_renewal=%s",
            sub_id, user_id, group_id, tx_hash, is_renewal,
        )

        # Step 6 — audit
        await audit(
            group_id=group_id,
            action="payment_confirmed",
            actor_id=user_id,
            details={"tx_hash": tx_hash, "amount_nano": amount_nano, "is_renewal": is_renewal},
        )

        return (is_renewal, new_renewal_date)  # signal to caller

    did_work = asyncio.run(_run())

    if did_work is not False:
        # _run() returns (is_renewal, new_renewal_date) on success, False on skip
        try:
            is_renewal, new_renewal_date = did_work
        except TypeError:
            # Should not happen, but guard against unexpected return shapes
            log.error("process_payment: unexpected _run() return: %r", did_work)
            return

        if is_renewal:
            _publish_bot_action("renewal_confirmed", {
                "vault_address":     vault_address,
                "tx_hash":           tx_hash,
                "new_renewal_date":  new_renewal_date,
                "subscription_id":   sub_id,
                "old_renewal_cycle": sub_before["next_renewal_date"] or "",
            })
        else:
            _publish_bot_action("approve_and_welcome", {
                "vault_address": vault_address,
                "tx_hash":       tx_hash,
            })

    log.info("process_payment done | vault=%s tx=%s", vault_address, tx_hash)


# ── Task: send renewal reminder DM ───────────────────────────────────────────

def send_renewal_reminder(
    subscription_id: int,
    telegram_user_id: int,
    group_id: int,
    renewal_cycle: str,
) -> None:
    """
    Send a renewal reminder DM to the user.
    Idempotent: reminder_log prevents re-sending for the same renewal cycle.
    """
    log.info(
        "send_renewal_reminder | sub_id=%d user=%d group=%d cycle=%s",
        subscription_id, telegram_user_id, group_id, renewal_cycle,
    )

    async def _run() -> bool:
        from renewise.watcher.db import has_reminder_been_sent, record_reminder_sent

        if await has_reminder_been_sent(subscription_id, renewal_cycle):
            log.info("send_renewal_reminder: already sent | sub_id=%d", subscription_id)
            return False

        await record_reminder_sent(subscription_id, renewal_cycle)
        return True

    did_work = asyncio.run(_run())

    if did_work:
        _publish_bot_action("send_renewal_reminder", {
            "subscription_id": subscription_id,
            "telegram_user_id": telegram_user_id,
            "group_id": group_id,
            "renewal_cycle": renewal_cycle,
        })
    log.info("send_renewal_reminder done | sub_id=%d", subscription_id)


# ── Task: expire subscription and kick member ─────────────────────────────────

def expire_and_kick(
    subscription_id: int,
    telegram_user_id: int,
    telegram_chat_id: int,
    group_id: int,
) -> None:
    """
    Mark subscription as expired and publish a kick action to the bot.
    Skipped for comped subscriptions and group admins (enforced in scheduler.py
    before this task is enqueued, but we double-check here for safety).
    """
    log.info(
        "expire_and_kick | sub_id=%d user=%d chat=%d",
        subscription_id, telegram_user_id, telegram_chat_id,
    )

    async def _run() -> None:
        from renewise.db.queries import audit
        from renewise.db.connection import _db as _conn

        async with _conn() as db:
            row = await db.fetchrow(
                "SELECT status FROM subscriptions WHERE id=$1", subscription_id
            )
            if not row or row["status"] in ("comped", "expired", "cancelled"):
                log.info("expire_and_kick: skip (status=%s) | sub_id=%d",
                         row["status"] if row else "missing", subscription_id)
                return
            await db.execute(
                "UPDATE subscriptions SET status='expired' WHERE id=$1",
                subscription_id,
            )

        await audit(
            group_id=group_id,
            action="subscription_expired",
            actor_id=telegram_user_id,
            details={"subscription_id": subscription_id},
        )

    asyncio.run(_run())

    async def _was_expired() -> bool:
        from renewise.db.connection import _db as _conn
        async with _conn() as db:
            val = await db.fetchval(
                "SELECT status FROM subscriptions WHERE id=$1", subscription_id
            )
            return val == "expired"

    if asyncio.run(_was_expired()):
        _publish_bot_action("kick_member", {
            "telegram_user_id": telegram_user_id,
            "telegram_chat_id": telegram_chat_id,
            "subscription_id":  subscription_id,
        })
        log.info("expire_and_kick: kick published | sub_id=%d", subscription_id)
    else:
        log.info("expire_and_kick: skipped kick (status not expired) | sub_id=%d", subscription_id)

    log.info("expire_and_kick done | sub_id=%d", subscription_id)

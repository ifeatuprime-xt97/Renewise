"""
renewise/watcher/inprocess_watcher.py

In-process payment watcher — runs inside the same asyncio event loop as the
bot, with no Redis or RQ dependency.

Use this for development / testnet where you want everything in one process:
    python run.py

Production deployments can continue to use the separate
watcher.py + worker.py + Redis stack for horizontal scaling.

Architecture
────────────
poll_vaults_inprocess(app)
  └─ loops every POLL_INTERVAL_SECONDS
       └─ for each vault in vault_registry
            └─ fetch_transactions() from toncenter.py  (reuses existing client)
                 └─ for each new tx: _process_payment_inprocess(app, ...)
                      ├─ idempotency check (processed_tx_hashes)
                      ├─ amount verification
                      ├─ activate_subscription in DB
                      └─ call bot_listener handlers directly (no Redis pub/sub)
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from renewise.watcher.config import POLL_INTERVAL_SECONDS, TONCENTER_TESTNET, TONCENTER_BASE_URL
# Reuse the existing TonCenter client — correct URL + key rotation already there
from renewise.watcher.toncenter import fetch_transactions, extract_tx_hash, extract_in_msg_value
from renewise.watcher.db import (
    migrate,
    get_vault_registration,
    is_tx_processed,
    mark_tx_processed,
)
from renewise.db.queries import (
    get_vaults_to_watch,
    get_group_by_id,
    get_subscription,
    activate_subscription,
    audit,
    is_payments_paused,
)

if TYPE_CHECKING:
    from telegram.ext import Application

log = logging.getLogger(__name__)


# ── Insufficient payment notifier ─────────────────────────────────────────────

async def _notify_insufficient_payment(
    app: "Application",
    user_id: int,         # DB user id
    group_id: int,
    total_paid_nano: int,
    required_nano: int,
) -> None:
    """DM the user when they sent less than the required amount."""
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        row = await db.fetchrow(
            "SELECT u.telegram_user_id, g.chat_title "
            "FROM users u JOIN groups g ON g.id=$1 WHERE u.id=$2",
            group_id, user_id,
        )

    if not row:
        return

    tg_user_id = row["telegram_user_id"]
    group_name = row["chat_title"] or "the group"

    shortfall_ton  = (required_nano - total_paid_nano) / 1_000_000_000
    total_paid_ton = total_paid_nano / 1_000_000_000
    required_ton   = required_nano / 1_000_000_000

    try:
        sent = await app.bot.send_message(
            chat_id=tg_user_id,
            text=(
                f"⚠️ <b>Partial payment received.</b>\n\n"
                f"You have paid a total of <b>{total_paid_ton:.4f} GRAM</b> but need "
                f"<b>{required_ton:.4f} GRAM</b> for <b>{group_name}</b>.\n\n"
                f"You're short by <b>{shortfall_ton:.4f} GRAM</b>.\n\n"
                f"Please send the remaining balance to the same "
                f"payment link to activate your subscription."
            ),
            parse_mode="HTML",
        )
        # Store so the successful-payment path can delete this warning message
        app.bot_data.setdefault("insufficient_msgs", {})[(user_id, group_id)] = sent.message_id
        log.info("Insufficient payment DM sent | user_id=%d group=%d msg=%d",
                 user_id, group_id, sent.message_id)
    except Exception as exc:
        log.warning("Could not DM user %d about insufficient payment: %s", tg_user_id, exc)


# ── Overpayment notifier ──────────────────────────────────────────────────────

async def _notify_overpayment(
    app: "Application",
    user_id: int,        # DB user id
    overpaid_nano: int,
    refund_nano: int,
    refund_usd: float,
    refund_id: int,
) -> None:
    """
    DM the user when they overpaid by more than $1 USD.
    Ask them for their Gram wallet address so we can process the refund.
    The refund_id is stored in bot_data so the next message they send
    (or /refundwallet command) is captured as their wallet address.
    """
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        row = await db.fetchrow(
            "SELECT telegram_user_id FROM users WHERE id=$1", user_id
        )

    if not row:
        return

    tg_user_id   = row["telegram_user_id"]
    overpaid_ton = overpaid_nano / 1_000_000_000
    refund_ton   = refund_nano   / 1_000_000_000

    # Store the pending refund id keyed by telegram_user_id so the wallet
    # collection handler can find it when the user replies.
    app.bot_data.setdefault("pending_refunds", {})[tg_user_id] = refund_id

    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "💳 Send My Wallet Address",
                callback_data=f"refund:prompt_{refund_id}",
            )
        ]])
        await app.bot.send_message(
            chat_id=tg_user_id,
            text=(
                f"💸 <b>You overpaid — we owe you a refund!</b>\n\n"
                f"You sent <b>{overpaid_ton:.4f} GRAM</b> more than required.\n"
                f"Refund amount: <b>{refund_ton:.4f} GRAM</b> "
                f"(≈ <b>${refund_usd:.2f} USD</b>) after network fees.\n\n"
                f"To receive your refund automatically, just <b>reply to this message</b> "
                f"with your Gram wallet address (starts with <code>UQ</code> or <code>EQ</code>).\n\n"
                f"You can find your address in Tonkeeper → Settings → Wallet Address."
            ),
            parse_mode="HTML",
            reply_markup=kb,
        )
        log.info(
            "Overpayment DM sent | user_id=%d overpaid=%.4f refund=%.4f usd=%.2f refund_id=%d",
            user_id, overpaid_ton, refund_ton, refund_usd, refund_id,
        )
    except Exception as exc:
        log.warning("Could not DM user %d about overpayment: %s", tg_user_id, exc)

async def _process_platform_charge_inprocess(
    app: "Application",
    charge: dict,
    tx_hash: str,
    amount_nano: int,
) -> None:
    """Process a completed platform API charge."""
    log.info("inprocess_platform: tx=%s charge_id=%d amount=%d status=%s",
             tx_hash, charge["id"], amount_nano, charge.get("status"))

    # Idempotency pre-check — skip txs already fully processed
    if await is_tx_processed(tx_hash):
        log.debug("inprocess_platform: already processed tx=%s", tx_hash)
        return

    required = charge["required_nano_amount"]

    if required is not None and amount_nano < required:
        log.warning(
            "inprocess_platform: insufficient amount %d < %d for charge %d",
            amount_nano, required, charge["id"],
        )
        # Record as partial so the seeding loop doesn't skip it on restart,
        # but don't dispatch a webhook for a partial payment.
        await mark_tx_processed(tx_hash, charge["id"], is_partial=True)
        return

    # Claim idempotency atomically — point of no return
    claimed = await mark_tx_processed(tx_hash, charge["id"])
    if not claimed:
        log.info("inprocess_platform: lost race on idempotency claim tx=%s", tx_hash)
        return
    
    log.info("inprocess_platform: marking charge %d as completed (tx=%s)", charge["id"], tx_hash)
        
    # Mark as completed
    from renewise.db.connection import _db
    async with _db() as db:
        await db.execute(
            "UPDATE platform_charges SET status = 'completed', completed_at = NOW(), tx_hash = $1 WHERE id = $2",
            tx_hash, charge["id"]
        )
    
    log.info("inprocess_platform: charge %d status updated to completed", charge["id"])
        
    from renewise.services.webhooks import dispatch_webhook
    asyncio.create_task(dispatch_webhook(charge["id"]))
    
    log.info("inprocess_platform: charge %d completed successfully", charge["id"])

async def _process_payment_inprocess(
    app: "Application",
    vault_address: str,
    tx_hash: str,
    amount_nano: int,
    insufficient_seen: set[str] | None = None,
    network: str | None = None,
) -> None:
    """
    Process a single confirmed vault payment entirely in-process.

    Mirrors tasks.process_payment() but:
      - runs as a native coroutine (no asyncio.run() wrapper needed)
      - calls bot_listener handlers directly instead of publishing to Redis
    """
    log.info("inprocess: tx=%s vault=%s amount=%d", tx_hash, vault_address, amount_nano)

    # Step 1 — idempotency pre-check
    if await is_tx_processed(tx_hash):
        log.debug("inprocess: already processed tx=%s", tx_hash)
        return

    log.info("inprocess: NEW transaction detected - tx=%s vault=%s amount=%d", tx_hash, vault_address, amount_nano)

    # Step 2 — resolve vault → subscription context
    reg = await get_vault_registration(vault_address)
    if not reg:
        from renewise.db.queries import get_platform_charge_by_vault
        log.info("inprocess: no vault registry for %s, checking platform_charges", vault_address)
        charge = await get_platform_charge_by_vault(vault_address)
        if charge:
            log.info("inprocess: found platform charge id=%d for vault=%s", charge["id"], vault_address)
            return await _process_platform_charge_inprocess(app, charge, tx_hash, amount_nano)
        
        # Enhanced debugging: show what's in the database
        from renewise.db.connection import _db
        async with _db() as db:
            all_pending = await db.fetch(
                "SELECT id, vault_address, status FROM platform_charges WHERE status = 'pending' ORDER BY id DESC LIMIT 10"
            )
            log.warning("inprocess: unknown vault %s — no registry entry and no platform charge. Recent pending charges:", vault_address)
            for pc in all_pending:
                log.warning("  charge_id=%d vault=%s status=%s match=%s", 
                           pc["id"], pc["vault_address"], pc["status"],
                           "YES" if pc["vault_address"] == vault_address else "NO")
        return

    user_id  = reg["user_id"]
    group_id = reg["group_id"]
    sub_id   = reg["subscription_id"]

    sub = await get_subscription(user_id, group_id)
    if not sub:
        log.warning("inprocess: no subscription for user_id=%d group_id=%d", user_id, group_id)
        return

    is_renewal = sub["status"] == "active"

    # Step 3 — resolve the required payment amount
    #
    # SOURCE OF TRUTH: sub["required_nano_amount"] is stored at payment-link
    # generation time from the vault's own price + buyer_fee + gas_reserve
    # calculation. It is locked in at that moment and must NOT be re-derived
    # from the live exchange rate — if Gram price moves between the first partial
    # payment and a top-up, re-deriving would shift the threshold and
    # incorrectly reject a payment that the vault itself accepted.
    #
    # Fallback (required_nano_amount is NULL — old row before this was added):
    # Read from the vault's required_payment() getter via a TonCenter get-method
    # call. This returns exactly what the contract computed, independent of the
    # current exchange rate. If that also fails, use 0 to skip the amount check
    # entirely and let the on-chain PaymentLog be the authoritative signal.
    group = await get_group_by_id(group_id)
    required: int | None = sub["required_nano_amount"] if sub["required_nano_amount"] else None

    # Sanity-check: if the single incoming tx already meets or exceeds the stored
    # required_nano_amount, then the stored value is stale (e.g. from a previous
    # payment link generated at a different exchange rate). Treat it as None so we
    # fall through to the on-chain getter rather than falsely firing a partial-payment
    # notification for a transaction that clearly covers the full price.
    if required is not None and amount_nano >= required:
        log.debug(
            "inprocess: amount_nano=%d >= stored required=%d — ignoring stale required_nano_amount, "
            "will recompute from vault getter | vault=%s tx=%s",
            amount_nano, required, vault_address, tx_hash,
        )
        required = None

    if required is None:
        # Attempt to read the vault's own required_payment() getter on-chain.
        # This is the same value the contract used when it split, so it is
        # immune to exchange rate drift.
        try:
            from renewise.watcher.toncenter import call_get_method
            result = await call_get_method(vault_address, "required_payment", [], network=network)
            if result is not None:
                required = int(result)
                log.info(
                    "inprocess: fetched required_payment from vault on-chain: %d | vault=%s",
                    required, vault_address,
                )
        except Exception as exc:
            log.warning(
                "inprocess: could not fetch required_payment from vault %s: %s — "
                "skipping amount check, relying on on-chain PaymentLog",
                vault_address, exc,
            )

    # Step 3.5 — global kill switch
    if await is_payments_paused():
        log.warning("inprocess: global payments kill switch active, skipping tx=%s", tx_hash)
        return

    # Step 4 — partial payment accumulation check
    #
    # Only compare against required if we have a reliable locked-in value.
    # If required is None (vault getter also failed), we trust the on-chain
    # PaymentLog: any tx that reached the vault is processed optimistically.
    # A false activation is less harmful than a paying user stuck with no access.
    #
    # We do NOT mark_tx_processed here yet — a partial payment must remain
    # retryable so that when the user sends a top-up we process that too.
    current_paid   = sub["amount_paid_so_far"] or 0
    new_total_paid = current_paid + amount_nano

    if required is not None and new_total_paid < required:
        log.warning(
            "inprocess: partial payment accumulated %d < %d | vault=%s tx=%s",
            new_total_paid, required, vault_address, tx_hash,
        )
        # NOTE: do NOT call mark_tx_processed here. Partial payments must remain
        # re-processable after a restart so that a top-up tx causes the watcher
        # to re-examine the running total. The in-memory insufficient_seen set
        # is sufficient to deduplicate notifications within a single run.
        from renewise.db.queries import update_amount_paid_so_far
        await update_amount_paid_so_far(sub["id"], new_total_paid)
        if insufficient_seen is not None and tx_hash not in insufficient_seen:
            insufficient_seen.add(tx_hash)
            await _notify_insufficient_payment(app, user_id, group_id, new_total_paid, required)
            await audit(
                group_id=group_id,
                action="payment_insufficient",
                actor_id=user_id,
                details={
                    "tx_hash":         tx_hash,
                    "amount_nano":     amount_nano,
                    "total_paid_nano": new_total_paid,
                    "required_nano":   required,
                    "shortfall_nano":  required - new_total_paid,
                },
            )
        elif insufficient_seen is None:
            await _notify_insufficient_payment(app, user_id, group_id, new_total_paid, required)
        return

    # Step 4.5 — atomic idempotency claim (only reached when amount is sufficient)
    # This is the point of no return: once claimed, we WILL activate the subscription.
    claimed = await mark_tx_processed(tx_hash, sub_id)
    if not claimed:
        log.info("inprocess: lost race on idempotency claim tx=%s", tx_hash)
        return

    # Reset accumulated amount — next renewal cycle starts from zero
    from renewise.db.queries import update_amount_paid_so_far
    await update_amount_paid_so_far(sub["id"], 0)

    # Step 5 — activate subscription in DB
    new_renewal_date = await activate_subscription(user_id, group_id, tx_hash=tx_hash)
    log.info(
        "inprocess: activated | sub_id=%d user=%d group=%d tx=%s is_renewal=%s",
        sub_id, user_id, group_id, tx_hash, is_renewal,
    )

    # Step 6 — audit
    await audit(
        group_id=group_id,
        action="payment_confirmed",
        actor_id=user_id,
        details={"tx_hash": tx_hash, "amount_nano": amount_nano, "total_paid_nano": new_total_paid, "is_renewal": is_renewal},
    )

    # Step 6.5 — overpayment check
    # Only fire if we have a reliable required value to compare against.
    if required is not None and new_total_paid > required:
        overpaid_nano = new_total_paid - required
        try:
            from renewise.utils.coingecko import get_ton_usd_price
            ton_rate     = await get_ton_usd_price()
            overpaid_usd = (overpaid_nano / 1_000_000_000) * ton_rate
        except Exception:
            overpaid_usd = 0.0

        from renewise.config import OVERPAYMENT_REFUND_THRESHOLD_USD
        if overpaid_usd >= OVERPAYMENT_REFUND_THRESHOLD_USD:
            log.info(
                "inprocess: overpayment detected | user=%d amount=%d total=%d required=%d "
                "overpaid_nano=%d overpaid_usd=%.2f tx=%s",
                user_id, amount_nano, new_total_paid, required, overpaid_nano, overpaid_usd, tx_hash,
            )
            # The vault deducts its own Refund{} gas fee (0.005 GRAM) on execution.
            # We record the full overpaid_nano so the DB matches what the vault holds.
            # The actual GRAM the user receives = overpaid_nano - 0.005 GRAM (on-chain).
            VAULT_REFUND_GAS_NANO = 5_000_000  # matches contract: ton("0.005")
            refund_nano = max(0, overpaid_nano - VAULT_REFUND_GAS_NANO)
            refund_usd  = (refund_nano / 1_000_000_000) * ton_rate if refund_nano else 0.0

            from renewise.db.queries import create_overpayment_refund
            refund_id = await create_overpayment_refund(
                subscription_id=sub_id,
                user_id=user_id,
                group_id=group_id,
                tx_hash=tx_hash,
                overpaid_nano=overpaid_nano,
                refund_nano=refund_nano,
                refund_usd=refund_usd,
            )
            await audit(
                group_id=group_id,
                action="overpayment_detected",
                actor_id=user_id,
                details={
                    "tx_hash":       tx_hash,
                    "overpaid_nano": overpaid_nano,
                    "overpaid_usd":  round(overpaid_usd, 2),
                    "refund_id":     refund_id,
                },
            )
            await _notify_overpayment(app, user_id, overpaid_nano, refund_nano, refund_usd, refund_id)

    # Step 7 — trigger bot actions directly (no Redis pub/sub needed)
    #
    # Also delete the "partial payment" warning if one was sent earlier for
    # this (user_id, group_id) pair — it's no longer relevant now that the
    # full payment has been confirmed.
    insuf_msg_id = app.bot_data.get("insufficient_msgs", {}).pop((user_id, group_id), None)
    if insuf_msg_id:
        try:
            from renewise.db.connection import _db as _conn
            async with _conn() as db:
                _row = await db.fetchrow(
                    "SELECT telegram_user_id FROM users WHERE id=$1", user_id
                )
            if _row:
                await app.bot.delete_message(chat_id=_row["telegram_user_id"], message_id=insuf_msg_id)
                log.info("Deleted insufficient-payment msg | user_id=%d group=%d msg=%d",
                         user_id, group_id, insuf_msg_id)
        except Exception as exc:
            log.debug("Could not delete insufficient-payment msg user_id=%d: %s", user_id, exc)

    from renewise.watcher.bot_listener import (
        _handle_approve_and_welcome,
        _handle_renewal_confirmed,
    )

    if is_renewal:
        await _handle_renewal_confirmed(app, {
            "vault_address":    vault_address,
            "tx_hash":          tx_hash,
            "new_renewal_date": new_renewal_date,
            "subscription_id":  sub_id,
            # Pass the OLD next_renewal_date so the handler can look up the
            # reminder message_id from reminder_log (keyed by renewal_cycle).
            "old_renewal_cycle": sub["next_renewal_date"] or "",
        })
    else:
        await _handle_approve_and_welcome(app, {
            "vault_address": vault_address,
            "tx_hash":       tx_hash,
        })

    log.info("inprocess: done tx=%s", tx_hash)


# ── Expire stale platform charges ────────────────────────────────────────────

async def _expire_stale_charges_inprocess() -> None:
    """
    Mark pending platform_charges as expired if they've passed their expires_at timestamp.
    Runs periodically in the watcher loop (every 5 minutes).
    """
    from renewise.db.connection import _db
    
    async with _db() as db:
        result = await db.execute(
            """
            UPDATE platform_charges
            SET status = 'expired'
            WHERE status = 'pending'
            AND expires_at IS NOT NULL
            AND expires_at < NOW() - INTERVAL '5 minutes'
            AND tx_hash IS NULL
            """
        )
        
        # PostgreSQL returns "UPDATE N" where N is row count
        if hasattr(result, 'split'):
            count = int(result.split()[-1]) if result.split()[-1].isdigit() else 0
        else:
            count = 0
            
        if count > 0:
            log.info("_expire_stale_charges: expired %d charge(s)", count)


# ── Wallet change apply (in-process, dev mode) ───────────────────────────────

async def _apply_wallet_changes_inprocess(app: "Application") -> None:
    """
    Dev-mode equivalent of apply_wallet_changes_job.

    Applies any pending wallet changes whose activates_at has passed,
    with the same Item 9 guard: skips and auto-cancels if the group is
    suspended or frozen.  DMs the admin via the live bot instance instead
    of a fresh Bot() call (we already have the Application here).
    """
    from renewise.db.queries import (
        get_due_wallet_changes,
        apply_pending_wallet_change,
        cancel_pending_wallet_change,
        audit,
    )

    rows = await get_due_wallet_changes()
    if not rows:
        return

    log.info("inprocess: %d wallet change(s) due", len(rows))

    for row in rows:
        change_id    = row["id"]
        group_id     = row["group_id"]
        new_wallet   = row["new_wallet_address"]
        admin_id     = row["admin_telegram_id"]
        group_title  = row["chat_title"] or str(group_id)
        group_status = row["group_status"]

        # Item 9 — skip if group is suspended or frozen
        if group_status in ("suspended", "frozen"):
            await cancel_pending_wallet_change(change_id, cancelled_by=0)
            await audit(
                group_id,
                "wallet_change_auto_cancelled_group_not_active",
                admin_id,
                {"change_id": change_id, "group_status": group_status},
            )
            log.warning(
                "inprocess: skipped wallet change_id=%d — group %d is %s",
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

        try:
            await app.bot.send_message(
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
            log.warning("inprocess: could not DM admin %d about wallet change: %s", admin_id, exc)

        log.info(
            "inprocess: applied wallet change_id=%d group=%d",
            change_id, group_id,
        )


# ── Main polling loop ─────────────────────────────────────────────────────────

async def poll_vaults_inprocess(app: "Application") -> None:
    """
    Continuously poll all registered vault addresses for new transactions
    and process any confirmed payments.

    Runs as a background asyncio task alongside the bot — cancel to stop.
    """
    # Ensure watcher DB tables exist (idempotent)
    await migrate()
    log.info(
        "In-process watcher started (poll interval=%ds, mainnet=%s, testnet=%s)",
        POLL_INTERVAL_SECONDS,
        TONCENTER_BASE_URL if not TONCENTER_TESTNET else "https://toncenter.com/api/v2",
        "https://testnet.toncenter.com/api/v2",
    )

    # Per-address seen-set: vault_address → set of tx hashes already handled.
    # On the very first poll for each vault we seed this set from existing txs
    # WITHOUT processing them — avoids replaying history on startup.
    seen: dict[str, set[str]] = {}

    # Track hashes that failed the amount check so we don't re-notify on every poll.
    _insufficient: set[str] = set()

    # Stale pending cleanup: run once per hour in dev mode.
    _last_cleanup: float = 0.0
    # Wallet change apply: run every 15 minutes in dev mode.
    _last_wallet_apply: float = 0.0
    # Charge expiration: run every 5 minutes
    _last_charge_expire: float = 0.0

    # Track in-flight payment tasks so they can be awaited/cancelled on shutdown.
    # WeakSet would lose references before tasks complete; use a plain set and
    # add a done-callback to remove finished tasks automatically.
    _inflight: set[asyncio.Task] = set()

    def _remove_done(t: asyncio.Task) -> None:
        _inflight.discard(t)

    try:
        while True:
            poll_backoff = 0  # extra seconds to wait after a 429 this cycle
            try:
                # Stale pending cleanup — once per hour
                import time as _time
                now = _time.monotonic()
                if now - _last_cleanup >= 3600:
                    from renewise.db.queries import delete_stale_pending_subscriptions
                    deleted = await delete_stale_pending_subscriptions(older_than_hours=12)
                    if deleted:
                        log.info("inprocess: deleted %d stale pending subscription(s)", deleted)
                    _last_cleanup = now

                if now - _last_wallet_apply >= 900:  # every 15 minutes
                    await _apply_wallet_changes_inprocess(app)
                    _last_wallet_apply = now
                    
                if now - _last_charge_expire >= 300:  # every 5 minutes
                    await _expire_stale_charges_inprocess()
                    _last_charge_expire = now

                vaults = await get_vaults_to_watch()
                # ALWAYS log polling activity so we can see the watcher is alive
                log.info("inprocess watcher: polling %d vault(s)", len(vaults))

                for vault_address, network in vaults:
                    # Normalize address to raw 0:<hex> form so all friendly-address
                    # variants (EQ/UQ/kQ/0Q) of the same vault map to the same
                    # seen-set key and TonCenter query uses a stable canonical form.
                    try:
                        from pytoniq_core import Address as _Addr
                        _a = _Addr(vault_address)
                        canonical = f"0:{_a.hash_part.hex()}"
                    except Exception:
                        canonical = vault_address

                    try:
                        txs = await fetch_transactions(canonical, limit=20, network=network)
                    except Exception as exc:
                        err_str = str(exc)
                        if "429" in err_str:
                            poll_backoff = max(poll_backoff, 30)
                            log.warning(
                                "inprocess: 429 rate-limit for vault %s — "
                                "backing off %ds before next cycle",
                                canonical, poll_backoff,
                            )
                        else:
                            log.warning("inprocess: fetch error for %s: %s", canonical, exc)
                        continue

                    if canonical not in seen:
                        seen[canonical] = set()
                        # Determine if this is a brand-new vault (just added to
                        # platform_charges or subscriptions) vs one that has been
                        # watched before and had history we should skip.
                        #
                        # For KNOWN old vaults: mark all existing txs as seen
                        # to avoid replaying history on every restart.
                        #
                        # For NEW vaults: we must still process unprocessed txs
                        # because the payment may have landed BEFORE the watcher
                        # first saw the vault (deploy-on-first-message means the
                        # vault can receive, split and empty in one block — by
                        # the time we first check it has 0 balance but the tx
                        # is in the history).
                        for tx in txs:
                            h = extract_tx_hash(tx)
                            if not h:
                                continue
                            if await is_tx_processed(h):
                                # Fully processed in a previous run — skip forever.
                                # Partial-payment txs are NOT returned by is_tx_processed
                                # (they have is_partial=1), so they are left out of seen
                                # and will be re-examined this cycle.
                                seen[canonical].add(h)
                            # Leave unprocessed and partial txs OUT of seen so they get
                            # picked up by the loop below on this same cycle.
                        log.info(
                            "inprocess: seeded %d already-processed tx(s) for vault %s "
                            "(%d total on-chain) — will process %d unprocessed tx(s)",
                            len(seen[canonical]), canonical, len(txs),
                            len(txs) - len(seen[canonical]),
                        )

                    for tx in txs:
                        tx_hash     = extract_tx_hash(tx)
                        amount_nano = extract_in_msg_value(tx)

                        if not tx_hash or tx_hash in seen[canonical]:
                            continue

                        seen[canonical].add(tx_hash)
                        log.info(
                            "inprocess: NEW transaction detected - tx=%s vault=%s amount=%d",
                            tx_hash, canonical, amount_nano,
                        )
                        t = asyncio.create_task(
                            _process_payment_inprocess(
                                app, canonical, tx_hash, amount_nano,
                                insufficient_seen=_insufficient,
                                network=network,
                            ),
                            name=f"pay_{tx_hash[:12]}",
                        )
                        _inflight.add(t)
                        t.add_done_callback(_remove_done)

            except asyncio.CancelledError:
                raise  # handled in outer try/finally
            except Exception as exc:
                log.error("inprocess watcher poll error: %s", exc)

            await asyncio.sleep(POLL_INTERVAL_SECONDS + poll_backoff)

    except asyncio.CancelledError:
        log.info("In-process watcher cancelled — waiting for %d in-flight payment(s).", len(_inflight))
        if _inflight:
            # Give in-flight payments a short grace period to finish naturally.
            done, pending = await asyncio.wait(_inflight, timeout=10)
            if pending:
                log.warning("Cancelling %d payment task(s) that didn't finish in time.", len(pending))
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        log.info("In-process watcher shut down.")

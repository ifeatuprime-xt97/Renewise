"""
renewise/handlers/refund.py

Handles wallet collection and trustless on-chain refund for overpayments.

Flow
────
  Overpayment detected (inprocess_watcher)
    → vault holds overage in overage_held (on-chain state)
    → refund record created in DB (status=pending_wallet)
    → bot_data["pending_refunds"][tg_user_id] = refund_id
    → user DMed asking for their GRAM wallet address (with inline prompt button)

  User replies with their wallet address  ← handled here
    → validated with validate_ton_address() (format + checksum)
    → trigger wallet sends Refund{recipient} to vault contract
    → vault verifies sender == trigger_wallet, releases overage_held to recipient
    → user DMed: trigger tx hash + TonScan explorer link
    → DB updated to status=sent automatically no superadmin needed

  If TRIGGER_MNEMONIC is not configured:
    → wallet saved to DB (status=pending_send)
    → superadmins alerted for manual processing
    → user told refund will be handled within 24 hours
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from renewise.db.queries import (
    get_pending_refund_for_user,
    set_refund_wallet,
    mark_refund_sent,
)
from renewise.services.wallet import validate_ton_address
from renewise.config import TRIGGER_MNEMONIC, TONCENTER_TESTNET

log = logging.getLogger(__name__)

# TonScan explorer base URL (testnet vs mainnet)
_EXPLORER_BASE = (
    "https://testnet.tonscan.org/tx/"
    if TONCENTER_TESTNET
    else "https://tonscan.org/tx/"
)


async def _get_vault_address_for_refund(refund_id: int) -> str:
    """Look up the vault_address for a refund record via its subscription."""
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        row = await db.fetchrow(
            "SELECT s.vault_address "
            "FROM overpayment_refunds r "
            "JOIN subscriptions s ON s.id = r.subscription_id "
            "WHERE r.id=$1",
            refund_id,
        )
    return row["vault_address"] if row and row["vault_address"] else ""


async def cb_refund_prompt(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    User tapped 'Send My Wallet Address' button on the overpayment DM.
    Just sends a clear prompt so they know exactly what to type next.
    The actual wallet is collected by handle_refund_wallet_message below.
    """
    query = update.callback_query
    await query.answer()
    await ctx.bot.send_message(
        chat_id=update.effective_user.id,
        text=(
            "💳 <b>Reply with your wallet address</b>\n\n"
            "Just send your address in the next message it starts with "
            "<code>UQ</code> or <code>EQ</code>.\n\n"
            "Example:\n"
            "<code>UQBvI0aFLnw2QbZgjMPCLRdtRHxhUyinQudg6sdiohIwg5jL</code>"
        ),
        parse_mode="HTML",
    )


async def handle_refund_wallet_message(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Catches plain-text messages in private chats when the user has a pending
    refund awaiting their wallet address.

    Registered as a low-priority MessageHandler only fires in private chats
    and only when bot_data["pending_refunds"] has an entry for this user OR
    the DB has a pending_wallet refund for them.  All other handlers run first.
    """
    if not update.message or not update.effective_user:
        return
    if update.effective_chat.type != "private":
        return

    tg_user_id = update.effective_user.id

    # Check if this user has a pending refund — bot_data first (fast path),
    # then DB (handles bot restarts).
    has_pending_in_memory = bool(
        ctx.bot_data.get("pending_refunds", {}).get(tg_user_id)
    )
    pending_refund = None
    if not has_pending_in_memory:
        pending_refund = await get_pending_refund_for_user(tg_user_id)
        if not pending_refund:
            return  # not our message — let other handlers deal with it

    text = (update.message.text or "").strip()
    await _process_wallet_submission(tg_user_id, text, ctx, update.message.reply_text)


async def _process_wallet_submission(
    tg_user_id: int,
    raw_wallet: str,
    ctx: ContextTypes.DEFAULT_TYPE,
    reply_fn,
) -> None:
    """
    Validate the submitted wallet address, execute the on-chain refund, and
    update the user and DB with the result.
    """
    wallet = raw_wallet.strip()

    # ── Step 1: validate address ──────────────────────────────────────────────
    # Use the existing wallet.py validator — it checks both format and checksum,
    # and respects TONCENTER_TESTNET to catch testnet/mainnet address mismatches.
    is_valid = await validate_ton_address(wallet)
    if not is_valid:
        await reply_fn(
            "❌ <b>That doesn't look like a valid wallet address.</b>\n\n"
            "Please send an address starting with <code>UQ</code> or <code>EQ</code>.\n\n"
            "You can copy it from Tonkeeper → Settings → Wallet Address.",
            parse_mode="HTML",
        )
        return

    # ── Step 2: resolve pending refund ────────────────────────────────────────
    pending = await get_pending_refund_for_user(tg_user_id)
    if not pending:
        # Stale bot_data entry — already processed
        ctx.bot_data.get("pending_refunds", {}).pop(tg_user_id, None)
        await reply_fn(
            "ℹ️ Your refund has already been processed or there is nothing pending.",
            parse_mode="HTML",
        )
        return

    refund_id   = pending["id"]
    refund_nano = pending["refund_nano"]
    refund_usd  = pending["refund_usd"]
    refund_ton  = refund_nano / 1_000_000_000

    # ── Step 3: atomically claim the refund row ───────────────────────────────
    # set_refund_wallet uses WHERE status='pending_wallet' as a compare-and-swap.
    # If two concurrent submissions race here, only the first UPDATE matches a row
    # and returns True — the second finds nothing and returns False, stopping here.
    claimed = await set_refund_wallet(refund_id, wallet)
    if not claimed:
        # Lost the race — another submission already claimed this refund.
        # The contract's require(amount > 0) would stop the duplicate on-chain
        # anyway, but we stop here in Python to avoid firing two trigger messages.
        ctx.bot_data.get("pending_refunds", {}).pop(tg_user_id, None)
        await reply_fn(
            "ℹ️ Your refund is already being processed. "
            "You will receive a confirmation once it completes.",
            parse_mode="HTML",
        )
        return

    # ── Step 4: clear bot_data ────────────────────────────────────────────────
    ctx.bot_data.get("pending_refunds", {}).pop(tg_user_id, None)

    # ── Step 5: send the Refund{} trigger to the vault ───────────────────────
    # The trigger wallet sends Refund{recipient} to the vault contract.
    # The vault verifies sender == trigger_wallet and releases overage_held
    # to recipient. The trigger wallet only needs enough GRAM for gas (~0.01 GRAM).
    if not TRIGGER_MNEMONIC:
        # Trigger wallet not configured — wallet is saved, notify superadmins.
        log.warning(
            "refund: TRIGGER_MNEMONIC not set refund_id=%d queued for manual send",
            refund_id,
        )
        from renewise.config import ALLOWED_SUPERADMIN_IDS
        for admin_id in ALLOWED_SUPERADMIN_IDS:
            try:
                await ctx.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"💸 <b>Manual refund needed (trigger wallet not configured)</b>\n\n"
                        f"Refund ID: <code>{refund_id}</code>\n"
                        f"User: <code>{tg_user_id}</code>\n"
                        f"Amount: <b>{refund_ton:.4f} GRAM</b> (≈ ${refund_usd:.2f} USD)\n"
                        f"Recipient: <code>{wallet}</code>\n\n"
                        f"Add <code>TRIGGER_MNEMONIC</code> to .env to automate refunds.\n"
                        f"View all refunds with /pendingrefunds."
                    ),
                    parse_mode="HTML",
                )
            except Exception as exc:
                log.warning("Could not alert superadmin %s about manual refund: %s", admin_id, exc)
        await reply_fn(
            f"✅ <b>Wallet address saved!</b>\n\n"
            f"We'll send <b>{refund_ton:.4f} GRAM</b> (≈ <b>${refund_usd:.2f} USD</b>) to:\n"
            f"<code>{wallet}</code>\n\n"
            f"<i>Refunds are processed within 24 hours.</i>",
            parse_mode="HTML",
        )
        return

    # Tell user we're processing
    await reply_fn(
        f"⏳ <b>Processing your refund…</b>\n\n"
        f"Sending <b>{refund_ton:.4f} GRAM</b> to <code>{wallet}</code>.\n"
        f"This usually takes under 30 seconds.",
        parse_mode="HTML",
    )

    vault_address = await _get_vault_address_for_refund(pending["id"])
    if not vault_address:
        log.error("refund: no vault_address for refund_id=%d sub_id=%s", refund_id, pending["subscription_id"])
        from renewise.config import ALLOWED_SUPERADMIN_IDS
        for admin_id in ALLOWED_SUPERADMIN_IDS:
            try:
                await ctx.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"🚨 <b>Refund failed vault address missing</b>\n\n"
                        f"Refund ID: <code>{refund_id}</code>\n"
                        f"User: <code>{tg_user_id}</code>\n"
        f"Amount: <b>{refund_ton:.4f} GRAM</b>\n"
                        f"Recipient: <code>{wallet}</code>\n\n"
                        f"The vault_address is NULL in the subscriptions table. "
                        f"Manually look up the vault for subscription_id="
                        f"<code>{pending['subscription_id']}</code> and send "
                        f"<code>Refund{{recipient}}</code> from the trigger wallet."
                    ),
                    parse_mode="HTML",
                )
            except Exception as exc:
                log.warning("Could not alert superadmin %s: %s", admin_id, exc)
        await reply_fn(
            "⚠️ <b>Could not locate your vault automatically.</b>\n\n"
            "Your wallet address has been saved. Our team will process your refund "
            f"of <b>{refund_ton:.4f} GRAM</b> manually within 24 hours.",
            parse_mode="HTML",
        )
        return

    from renewise.ton.refund_trigger import send_refund_trigger
    result = await send_refund_trigger(
        vault_address=vault_address,
        recipient_address=wallet,
    )

    if result.success:
        await mark_refund_sent(refund_id)

        explorer_url = f"{_EXPLORER_BASE}{result.tx_hash}" if result.tx_hash else None

        msg = (
            f"✅ <b>Refund triggered!</b>\n\n"
            f"<b>Amount:</b> {refund_ton:.4f} GRAM (≈ ${refund_usd:.2f} USD)\n"
            f"<b>To:</b> <code>{wallet}</code>\n\n"
            f"<b>Trigger TX:</b>\n"
            f"<code>{result.tx_hash}</code>\n"
        )
        if explorer_url:
            msg += f"\n🔍 <a href=\"{explorer_url}\">View trigger tx on TonScan</a>\n"
        msg += (
            f"\n<i>The vault contract will send the GRAM directly to your wallet. "
            f"It may take a few seconds to appear on-chain.</i>"
        )
        await reply_fn(msg, parse_mode="HTML")

        log.info(
            "refund: trigger sent | refund_id=%d user=%d vault=%s tx=%s",
            refund_id, tg_user_id, vault_address, result.tx_hash,
        )
        from renewise.db.queries import audit
        await audit(
            group_id=pending["group_id"],
            action="refund_triggered",
            actor_id=tg_user_id,
            details={
                "refund_id":   refund_id,
                "tx_hash":     result.tx_hash,
                "vault":       vault_address,
                "recipient":   wallet,
                "refund_nano": refund_nano,
            },
        )
    else:
        log.error(
            "refund: trigger failed | refund_id=%d user=%d error=%s",
            refund_id, tg_user_id, result.error,
        )
        await reply_fn(
            f"⚠️ <b>Automatic refund failed.</b>\n\n"
            f"Don't worry your wallet address has been saved and a member of "
            f"our team will process your refund of <b>{refund_ton:.4f} GRAM</b> manually.\n\n"
            f"<i>Expected within 24 hours.</i>",
            parse_mode="HTML",
        )
        from renewise.config import ALLOWED_SUPERADMIN_IDS
        for admin_id in ALLOWED_SUPERADMIN_IDS:
            try:
                await ctx.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"🚨 <b>Auto-refund trigger failed manual action required</b>\n\n"
                        f"Refund ID: <code>{refund_id}</code>\n"
                        f"User: <code>{tg_user_id}</code>\n"
                        f"Vault: <code>{vault_address}</code>\n"
                        f"Amount: <b>{refund_ton:.4f} GRAM</b> (≈ ${refund_usd:.2f} USD)\n"
                        f"Recipient: <code>{wallet}</code>\n"
                        f"Error: <code>{result.error}</code>\n\n"
                        f"<b>To refund manually:</b>\n"
                        f"Send a <code>Refund{{recipient: {wallet}}}</code> message "
                        f"from the trigger wallet to vault <code>{vault_address}</code>.\n\n"
                        f"Use /pendingrefunds to view all pending refunds."
                    ),
                    parse_mode="HTML",
                )
            except Exception as exc:
                log.warning("Could not alert superadmin %s: %s", admin_id, exc)

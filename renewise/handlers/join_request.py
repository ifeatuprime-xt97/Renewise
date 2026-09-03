"""
Handles ChatJoinRequest events for paywalled groups.
Also handles the pay-now and I've-Paid callbacks from the join flow.
"""
from __future__ import annotations
import html
import io
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from telegram import Update
from telegram.ext import ContextTypes
from renewise.db import queries
from renewise.services.payment import generate_payment_request, check_payment_status, PaymentStatus
from renewise.utils.keyboards import pay_now_kb, payment_details_kb, _support_url

log = logging.getLogger(__name__)


async def _fetch_qr_bytes(data: str, size: int = 400) -> bytes:
    """Fetch a QR code PNG from api.qrserver.com for the given data string.

    Explicitly sets white background + black foreground so the QR renders
    correctly on Telegram's dark theme without appearing inverted.
    """
    url = (
        f"https://api.qrserver.com/v1/create-qr-code/"
        f"?size={size}x{size}&margin=10"
        f"&color=000000&bgcolor=ffffff"
        f"&data={aiohttp.helpers.quote(data, safe='')}"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            resp.raise_for_status()
            return await resp.read()


async def handle_join_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    req = update.chat_join_request
    if not req:
        return

    chat = req.chat
    user = req.from_user

    group = await queries.get_group_by_chat_id(chat.id)
    if not group:
        return

    if group["status"] == "suspended":
        # Group suspended by admin — explicitly decline join request
        try:
            await ctx.bot.decline_chat_join_request(chat_id=chat.id, user_id=user.id)
        except Exception as e:
            log.error("Failed to decline join request for suspended group %s: %s", chat.id, e)
        return

    if group["status"] != "active":
        # Not a paywalled group or paused/frozen — ignore
        return

    # Check global payments kill-switch BEFORE doing anything else — avoid
    # sending a welcome DM then immediately a "payments paused" DM.
    if await queries.is_payments_paused():
        try:
            await ctx.bot.send_message(
                user.id,
                "⚠️ New signups and payments are temporarily paused platform-wide. "
                "Please try again later.",
                parse_mode="HTML",
            )
        except Exception as e:
            log.warning("Cannot DM user %s (payments paused notice): %s", user.id, e)
        return

    # price_locked_in stores the USD price in cents (matching groups.price_usd_cents).
    # The watcher uses required_nano_amount (set by generate_payment_request) as the
    # authoritative verification amount — price_locked_in is only used as a human-readable
    # record and fallback. Store USD cents as a float here for consistency.
    price_locked_in = group["price_usd_cents"] / 100.0 if group["price_usd_cents"] else group["price"]
    user_db_id = await queries.upsert_user(
        telegram_user_id=user.id,
        first_name=user.first_name,
        username=user.username
    )
    await queries.create_subscription(
        user_id=user_db_id,
        group_id=group["id"],
        price_locked_in=price_locked_in,
    )

    # Step 1: warm welcome with pricing info
    group_name = html.escape(chat.title or "our community")
    description = html.escape(getattr(chat, "description", None) or "")

    price_usd = group["price_usd_cents"] / 100.0 if group["price_usd_cents"] else None
    interval  = group["billing_interval_days"]

    # Compute the total GRAM the member will actually pay (price + buyer fee).
    # group["price"] is stale — derive live from CoinGecko.
    if price_usd:
        try:
            from renewise.utils.coingecko import get_ton_usd_price
            from renewise.db.queries import get_global_fees
            from renewise.ton.vault import MIN_GAS_RESERVE_NANO
            ton_rate = await get_ton_usd_price()
            global_buyer_bps, _ = await get_global_fees()
            buyer_bps  = group["buyer_fee_bps"] if group["buyer_fee_bps"] is not None else global_buyer_bps
            buyer_pct  = buyer_bps / 100.0          # 200 bps → 2.00 (displayed as 2.00%)
            fee_usd    = price_usd * buyer_bps / 10000
            total_usd  = price_usd + fee_usd
            total_gram = total_usd / ton_rate
            price_line = (
                f"💰 <b>${price_usd:.2f}</b> + {buyer_pct:.2f}% fee "
                f"= <b>${total_usd:.2f} (≈ {total_gram:.4f} GRAM)</b> "
                f"per {interval} days"
            )
        except Exception:
            price_line = f"💰 ${price_usd:.2f} USD per {interval} days"
    else:
        # Legacy row — fall back to stored GRAM value, no fee info
        price_line = f"💰 {group['price']:.4f} GRAM per {interval} days"

    safe_first_name = html.escape(user.first_name or "there")
    welcome = (
        f"👋 Hey {safe_first_name}! Thanks for your interest in joining "
        f"<b>{group_name}</b>.\n"
    )
    if description:
        welcome += f"\n{description}\n"
    welcome += (
        f"\nWe're excited to have you just one quick step to get you in! 👇\n\n"
        f"<b>Subscription fee:</b>\n"
        f"{price_line}\n\n"
        f"You pay in <b>GRAM (TON)</b>. Tap <b>Pay Now</b> to proceed. 👇"
    )

    # Build the Pay Now deep-link button.
    # This routes through /start pay_{group_id} which guarantees the bot can DM
    # the user (they just tapped a link that opens the bot) — no more "can't DM"
    # silent failures from users who haven't started the bot yet.
    bot_me = await ctx.bot.get_me()
    pay_link = f"https://t.me/{bot_me.username}?start=pay_{group['id']}"

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    pay_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💳 Pay Now", url=pay_link)
    ]])

    try:
        await ctx.bot.send_message(user.id, welcome, parse_mode="HTML", reply_markup=pay_kb)
    except Exception:
        # User hasn't started the bot yet — they can't receive DMs.
        # The Pay Now button is a deep-link so tapping it opens the bot DM
        # and fires /start pay_{group_id}, at which point we can DM them.
        # Nothing more to do here; the /start handler takes over.
        log.info(
            "handle_join_request: cannot DM user %s yet pay link will be shown on /start",
            user.id,
        )

    # Store the pending context so /start pay_{group_id} and cb_ive_paid
    # (legacy fallback) can both find it.
    # Payment link is generated lazily in the /start handler to get the
    # freshest exchange rate at the moment the user actually taps Pay Now.
    ctx.bot_data.setdefault("pending_joins", {})[user.id] = {
        "group_id":   group["id"],
        "chat_id":    chat.id,
        "group_name": group_name,
        "interval":   interval,
    }


async def cb_pay_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """User tapped 'Pay Now' send QR code + wallet address + amount."""
    query = update.callback_query
    await query.answer("Opening payment details…")
    user = update.effective_user

    pending = ctx.bot_data.get("pending_joins", {}).get(user.id)
    if not pending:
        # Try to fetch the channel link from the most recent pending subscription in DB
        channel_link_text = ""
        try:
            from renewise.db.connection import _db as _conn
            user_row = await queries.get_user_by_telegram_id(user.id)
            if user_row:
                async with _conn() as db:
                    row = await db.fetchrow(
                        "SELECT g.telegram_chat_id, g.chat_title, g.invite_link "
                        "FROM subscriptions s "
                        "JOIN groups g ON g.id = s.group_id "
                        "WHERE s.user_id = $1 AND s.status = 'pending' "
                        "ORDER BY s.created_at DESC LIMIT 1",
                        user_row["id"],
                    )
                if row:
                    invite_link = row["invite_link"]
                    chat_title  = html.escape(row["chat_title"] or "the channel")
                    if not invite_link:
                        try:
                            chat_obj = await ctx.bot.get_chat(row["telegram_chat_id"])
                            invite_link = chat_obj.invite_link
                        except Exception:
                            pass
                    if invite_link:
                        channel_link_text = f'\n\n👉 <a href="{invite_link}">{chat_title}</a>'
                    else:
                        channel_link_text = f"\n\n👉 <b>{chat_title}</b>"
        except Exception as _e:
            log.debug("cb_pay_now: could not resolve channel link for expired session: %s", _e)

        await query.edit_message_text(
            "⚠️ Session expired. Please request to join again.\n\n"
            "Your previous request has been cancelled so you can re-request immediately."
            + channel_link_text,
            parse_mode="HTML",
        )
        return

    payment_url = pending["payment_url"]   # https://app.tonkeeper.com/... (Telegram button)
    vault_addr  = pending["vault_addr"]
    amount_ton  = pending["amount_ton"]

    # payment_url is already ton:// with &init= — use directly for the QR
    ton_qr_url = payment_url

    # Show USD equivalent so the user can sanity-check the amount
    try:
        from renewise.utils.coingecko import get_ton_usd_price
        ton_rate   = await get_ton_usd_price()
        amount_usd = amount_ton * ton_rate
        usd_line   = f"≈ <b>${amount_usd:.2f} USD</b>"
    except Exception:
        usd_line = ""

    caption = (
        f"� <b>Payment</b>\n\n"
        f"<b>Amount to send:</b>\n"
        f"<code>{amount_ton:.9f} TON</code>  {usd_line}\n\n"
        f"<b>How to pay:</b>\n"
        f"• Tap the button below to open your wallet\n"
        f"• Or scan the QR code with your mobile wallet\n\n"
        f"💡 <b>Your wallet may show a warning.</b> This is normal for first payments. "
        f"Your TON is safe and will be split automatically on the blockchain.\n\n"
        f"Send the <b>exact amount shown</b> overpayments are refunded automatically."
    )

    try:
        qr_bytes = await _fetch_qr_bytes(ton_qr_url)
        qr_file  = io.BytesIO(qr_bytes)
        qr_file.name = "payment_qr.png"
        await ctx.bot.send_photo(
            chat_id=user.id,
            photo=qr_file,
            caption=caption,
            parse_mode="HTML",
            reply_markup=payment_details_kb(payment_url, _support_url()),
        )
    except Exception as e:
        log.error("cb_pay_now send_photo failed for user %s: %r", user.id, e)
        try:
            await ctx.bot.send_message(
                user.id,
                caption,
                parse_mode="HTML",
                reply_markup=payment_details_kb(payment_url, _support_url()),
            )
        except Exception as e2:
            # Bot can't DM the user (they haven't started a chat with the bot yet).
            # Edit the existing inline message with the full payment details so the
            # user can still complete the payment without needing a DM.
            log.warning(
                "cb_pay_now: cannot DM user %s (%r) falling back to inline edit",
                user.id, e2,
            )
            fallback_text = (
                f"📲 <b>Payment Details</b>\n\n"
                f"<b>Amount to send:</b>\n"
                f"<code>{amount_ton:.9f} TON</code>\n\n"
                f"<b>How to pay:</b>\n"
                f"• Tap the button below to open your wallet\n"
                f"• Or start a chat with this bot to get the QR code\n\n"
                f"💡 <b>Your wallet may show a warning.</b> This is normal — your TON is safe!\n\n"
                f"Send the <b>exact amount shown</b> — overpayments are refunded automatically."
            )
            try:
                await query.edit_message_text(
                    fallback_text,
                    parse_mode="HTML",
                    reply_markup=payment_details_kb(payment_url, _support_url()),
                )
            except Exception as e3:
                log.error(
                    "cb_pay_now: inline edit also failed for user %s: %r",
                    user.id, e3,
                )


async def cb_ive_paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Checking your payment…")
    user = update.effective_user

    async def _edit(text: str, reply_markup=None) -> None:
        """Edit the message whether it's a photo (caption) or plain text.

        Silently ignores Telegram's 'Message is not modified' error so that
        repeated taps of the same button never crash the bot.
        """
        from telegram.error import BadRequest as TgBadRequest
        kwargs = {"parse_mode": "HTML"}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        try:
            if query.message and query.message.photo:
                await query.edit_message_caption(caption=text, **kwargs)
            else:
                await query.edit_message_text(text, **kwargs)
        except TgBadRequest as e:
            if "message is not modified" in str(e).lower():
                pass  # user tapped the button twice — harmless
            else:
                raise

    pending = ctx.bot_data.get("pending_joins", {}).get(user.id)

    # ── No in-memory session (bot restarted, or old payment message) ──────────
    # Fall back to a direct on-chain recheck using the vault registered in the DB.
    if not pending:
        user_row = await queries.get_user_by_telegram_id(user.id)
        if not user_row:
            await _edit(
                "⚠️ I couldn't find your account. "
                "Please request to join the group again."
            )
            return

        # Find the most recent pending subscription for this user
        from renewise.db.connection import _db as _conn
        async with _conn() as db:
            sub_row = await db.fetchrow(
                "SELECT s.id, s.group_id, s.status, "
                "g.telegram_chat_id, g.billing_interval_days, g.chat_title, "
                "vr.vault_address "
                "FROM subscriptions s "
                "JOIN groups g ON g.id = s.group_id "
                "LEFT JOIN vault_registry vr "
                "ON vr.user_id = s.user_id AND vr.group_id = s.group_id "
                "WHERE s.user_id = $1 AND s.status = 'pending' "
                "ORDER BY s.created_at DESC LIMIT 1",
                user_row["id"],
            )

        if not sub_row:
            # Check if they're already active (watcher already processed it)
            async with _conn() as db:
                active_row = await db.fetchrow(
                    "SELECT s.group_id, g.chat_title, g.billing_interval_days, "
                    "g.telegram_chat_id "
                    "FROM subscriptions s "
                    "JOIN groups g ON g.id = s.group_id "
                    "WHERE s.user_id = $1 AND s.status = 'active' "
                    "ORDER BY s.start_date DESC LIMIT 1",
                    user_row["id"],
                )

            if active_row:
                await _edit(
                    f"✅ <b>You're already a member of {html.escape(active_row['chat_title'] or 'the group')}!</b>\n\n"
                    "Your payment was confirmed and your subscription is active."
                )
            else:
                await _edit(
                    "⚠️ No pending payment found. "
                    "Please request to join the group again."
                )
            return

        group_id  = sub_row["group_id"]
        chat_id   = sub_row["telegram_chat_id"]
        interval  = sub_row["billing_interval_days"] or 30
        group_name = html.escape(sub_row["chat_title"] or "the group")
        vault_address = sub_row["vault_address"]

        if not vault_address:
            await _edit(
                "⏳ <b>Payment not confirmed yet.</b>\n\n"
                "The watcher is checking on-chain automatically. "
                "You'll receive a confirmation message once your payment is detected.\n\n"
                "If this takes more than a few minutes, contact support.",
                reply_markup=query.message.reply_markup,
            )
            return

        # Trigger an immediate on-chain recheck for this vault
        await _edit(
            "🔍 <b>Checking on-chain now…</b>\n\n"
            "Please wait a moment.",
        )

        try:
            from renewise.watcher.toncenter import fetch_transactions, extract_tx_hash, extract_in_msg_value
            from renewise.watcher.db import is_tx_processed
            from renewise.watcher.inprocess_watcher import _process_payment_inprocess
            from renewise.db.connection import _db as _conn

            # Fetch required amount from DB — avoids re-computing the exchange rate
            async with _conn() as db:
                req_row = await db.fetchrow(
                    "SELECT required_nano_amount FROM subscriptions "
                    "WHERE user_id=$1 AND group_id=$2",
                    user_row["id"], group_id,
                )
            required_nano = req_row["required_nano_amount"] if req_row else None

            txs = await fetch_transactions(vault_address, limit=10)
            found_unprocessed = False
            # Suppress partial-payment DMs during manual I've-Paid checks
            _seen_insufficient: set[str] = set()
            for tx in txs:
                tx_hash     = extract_tx_hash(tx)
                amount_nano = extract_in_msg_value(tx)
                if not tx_hash or await is_tx_processed(tx_hash):
                    continue
                # Only skip genuinely low-value txs — if required_nano itself is
                # >= amount_nano it may be stale (generated at a different rate).
                # Let _process_payment_inprocess make the authoritative call;
                # its stale guard will re-derive required from the vault getter.
                if required_nano and amount_nano < required_nano and required_nano > amount_nano * 2:
                    log.info(
                        "cb_ive_paid fallback: skipping dust tx=%s amount=%d required=%d",
                        tx_hash, amount_nano, required_nano,
                    )
                    continue
                # Found a plausible unprocessed tx — let the watcher decide
                found_unprocessed = True
                await _process_payment_inprocess(
                    ctx.application, vault_address, tx_hash, amount_nano,
                    insufficient_seen=_seen_insufficient,
                )
                break

            # Re-check DB status after processing
            status = await check_payment_status(user.id, group_id)
            if status == PaymentStatus.CONFIRMED:
                next_renewal = datetime.now(timezone.utc) + timedelta(days=interval)
                renewal_str  = next_renewal.strftime("%d/%m/%Y")
                # Delete the QR message, send a clean confirmation
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await ctx.bot.send_message(
                    chat_id=user.id,
                    text=(
                        f"🎉 <b>Welcome to {group_name}!</b>\n\n"
                        "✅ Your payment is confirmed and you're now a member. Enjoy!\n\n"
                        f"📅 <b>Your next subscription is due on {renewal_str}.</b>\n"
                        "🔔 We'll send you a reminder 3 days before it's due."
                    ),
                    parse_mode="HTML",
                )
            elif not found_unprocessed:
                await _edit(
                    "⏳ <b>No payment found on-chain yet.</b>\n\n"
                    "If you've already sent the payment, it may still be confirming. "
                    "Try again in a few seconds, or contact support.",
                    reply_markup=query.message.reply_markup,
                )
            else:
                await _edit(
                    "⏳ <b>Payment found but still processing.</b>\n\n"
                    "You'll receive a confirmation message shortly.",
                    reply_markup=query.message.reply_markup,
                )
        except Exception as e:
            log.error("On-chain recheck failed for user %s: %s", user.id, e)
            await _edit(
                "⏳ <b>Could not reach the blockchain right now.</b>\n\n"
                "The watcher will pick up your payment automatically. "
                "You'll receive a confirmation message once it's detected.",
                reply_markup=query.message.reply_markup,
            )
        return

    # ── Normal path: active in-memory session ─────────────────────────────────
    group_id   = pending["group_id"]
    chat_id    = pending.get("chat_id")
    interval   = pending.get("interval", 30)
    group_name = html.escape(pending.get("group_name") or "the group")
    vault_address = pending.get("vault_addr")

    async def _confirm_and_welcome(grp_id: int, tg_chat_id, ivl: int, gname: str) -> None:
        """Approve the join request, delete the QR message, send clean confirmation."""
        if tg_chat_id:
            try:
                await ctx.bot.approve_chat_join_request(chat_id=tg_chat_id, user_id=user.id)
            except Exception as e:
                log.warning(
                    "Failed to approve join request for %s in %s (may already be approved): %s",
                    user.id, tg_chat_id, e,
                )
        # Delete the QR/payment-details message to keep the chat clean
        pay_msg_id = ctx.bot_data.get("pending_joins", {}).get(user.id, {}).get("payment_msg_id")
        if pay_msg_id:
            try:
                await ctx.bot.delete_message(chat_id=user.id, message_id=pay_msg_id)
            except Exception:
                pass  # already deleted or too old — harmless
        ctx.bot_data["pending_joins"].pop(user.id, None)
        next_renewal = datetime.now(timezone.utc) + timedelta(days=ivl)
        renewal_str  = next_renewal.strftime("%d/%m/%Y")
        await ctx.bot.send_message(
            chat_id=user.id,
            text=(
                f"🎉 <b>Welcome to {gname}!</b>\n\n"
                "✅ Your payment is confirmed and you're now a member. Enjoy!\n\n"
                f"📅 <b>Your next subscription is due on {renewal_str}.</b>\n"
                "🔔 We'll send you a reminder 3 days before it's due."
            ),
            parse_mode="HTML",
        )
        await queries.audit(
            group_id=grp_id,
            action="member_joined",
            actor_id=user.id,
            details={"method": "payment"},
        )

    # First: fast DB check (watcher may have already activated before the user tapped)
    status = await check_payment_status(user.id, group_id)
    if status == PaymentStatus.CONFIRMED:
        await _confirm_and_welcome(group_id, chat_id, interval, group_name)
        return

    # DB not confirmed yet — trigger an immediate on-chain recheck (same logic as
    # the no-session fallback path) so the user gets instant feedback after paying.
    if not vault_address:
        checked_at = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        await _edit(
            "⏳ <b>Payment not confirmed yet.</b>\n\n"
            "It can take a moment to process on-chain. "
            "Tap <b>I've Paid</b> again in a few seconds, "
            "or contact support if this persists.\n\n"
            f"<i>Last checked: {checked_at}</i>",
            reply_markup=query.message.reply_markup,
        )
        return

    await _edit("🔍 <b>Checking on-chain now…</b>\n\nPlease wait a moment.")

    try:
        from renewise.watcher.toncenter import fetch_transactions, extract_tx_hash, extract_in_msg_value
        from renewise.watcher.db import is_tx_processed
        from renewise.watcher.inprocess_watcher import _process_payment_inprocess

        # Fetch the locked-in required amount from DB to avoid exchange-rate drift
        user_row_inner = await queries.get_user_by_telegram_id(user.id)
        required_nano: int | None = None
        if user_row_inner:
            from renewise.db.connection import _db as _conn
            async with _conn() as db:
                req_row = await db.fetchrow(
                    "SELECT required_nano_amount FROM subscriptions "
                    "WHERE user_id=$1 AND group_id=$2",
                    user_row_inner["id"], group_id,
                )
            required_nano = req_row["required_nano_amount"] if req_row else None

        txs = await fetch_transactions(vault_address, limit=10)
        found_unprocessed = False
        # Pass a local insufficient_seen set so _process_payment_inprocess does NOT
        # send a "partial payment" DM during a manual I've-Paid check — the user
        # just told us they paid, so showing "you're short" immediately is confusing
        # and races with the confirmation message.
        _seen_insufficient: set[str] = set()
        for tx in txs:
            tx_hash     = extract_tx_hash(tx)
            amount_nano = extract_in_msg_value(tx)
            if not tx_hash or await is_tx_processed(tx_hash):
                continue
            # Only skip genuinely tiny txs (amount is less than half of required).
            # If amount_nano is close to required_nano, required_nano may be stale —
            # let _process_payment_inprocess's stale guard make the final call.
            if required_nano and amount_nano < required_nano and required_nano > amount_nano * 2:
                log.info(
                    "cb_ive_paid normal: skipping dust tx=%s amount=%d required=%d",
                    tx_hash, amount_nano, required_nano,
                )
                continue
            found_unprocessed = True
            await _process_payment_inprocess(
                ctx.application, vault_address, tx_hash, amount_nano,
                insufficient_seen=_seen_insufficient,
            )
            break

        # Re-check DB after the inprocess watcher may have activated the subscription
        status = await check_payment_status(user.id, group_id)
        if status == PaymentStatus.CONFIRMED:
            await _confirm_and_welcome(group_id, chat_id, interval, group_name)
        elif not found_unprocessed:
            checked_at = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            await _edit(
                "⏳ <b>No payment found on-chain yet.</b>\n\n"
                "If you've already sent the payment, it may still be confirming. "
                "Try again in a few seconds, or contact support.\n\n"
                f"<i>Last checked: {checked_at}</i>",
                reply_markup=query.message.reply_markup,
            )
        else:
            await _edit(
                "⏳ <b>Payment found but still processing.</b>\n\n"
                "You'll receive a confirmation message shortly.",
                reply_markup=query.message.reply_markup,
            )
    except Exception as e:
        log.error("On-chain recheck (normal path) failed for user %s: %s", user.id, e)
        checked_at = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        await _edit(
            "⏳ <b>Could not reach the blockchain right now.</b>\n\n"
            "The watcher will pick up your payment automatically. "
            "You'll receive a confirmation message once it's detected.\n\n"
            f"<i>Last checked: {checked_at}</i>",
            reply_markup=query.message.reply_markup,
        )


async def cb_cancel_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """User tapped 'Cancel' on the payment details screen."""
    query = update.callback_query
    await query.answer("Payment cancelled.")
    user = update.effective_user

    pending = ctx.bot_data.get("pending_joins", {}).pop(user.id, None)

    # Decline the open Telegram join request so the user can re-request immediately.
    # If we don't do this, Telegram keeps the request "open" and the user is blocked
    # from sending a new one until it times out or is manually declined.
    if pending:
        try:
            await ctx.bot.decline_chat_join_request(
                chat_id=pending["chat_id"],
                user_id=user.id,
            )
        except Exception as e:
            log.warning("Could not decline join request for user %s in chat %s: %s",
                        user.id, pending["chat_id"], e)

    # Edit the QR photo caption to show a friendly cancellation message
    try:
        await query.edit_message_caption(
            caption=(
                "❌ <b>Payment cancelled.</b>\n\n"
                "No worries! If you change your mind, just request to join the group again."
            ),
            parse_mode="HTML",
        )
    except Exception:
        # Message may not have a caption (text fallback path) — try plain edit
        try:
            await query.edit_message_text(
                "❌ <b>Payment cancelled.</b>\n\n"
                "No worries! If you change your mind, just request to join the group again.",
                parse_mode="HTML",
            )
        except Exception as e:
            log.warning("Could not edit cancel message for user %s: %s", user.id, e)

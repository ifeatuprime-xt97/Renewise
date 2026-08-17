"""
renewise bot entry point.
Run with: python -m renewise.bot
"""
import asyncio
import sys
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ChatMemberHandler, ChatJoinRequestHandler,
    CallbackQueryHandler, CommandHandler, ContextTypes,
)

from renewise.config import BOT_TOKEN
from renewise.db.schema import init_db
from renewise.handlers.chat_member import handle_my_chat_member
from renewise.handlers.join_request import handle_join_request, cb_pay_now, cb_ive_paid, cb_cancel_payment
from renewise.handlers.create_paywall import build_create_paywall_handler
from renewise.handlers.admin_menu import register_menu_callbacks
from renewise.handlers.refund import cb_refund_prompt, handle_refund_wallet_message

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── Terms of Service text (condensed for Telegram) ────────────────────────────
_TERMS_TEXT = (
    "📋 <b>Renewise — Terms of Service &amp; Privacy Policy</b>\n\n"

    "<b>What Renewise does</b>\n"
    "Renewise lets Telegram group and channel admins charge for membership. "
    "Payments are made in GRAM (TON) directly to a smart contract that splits "
    "funds between the admin and Renewise's platform fee in one atomic transaction.\n\n"

    "<b>Non-custodial</b>\n"
    "Renewise never holds admin or member funds. Completed on-chain splits are final and "
    "cannot be reversed. Overpayments above $1.00 USD are automatically "
    "refunded to the member's TON wallet on request.\n\n"

    "<b>Fees</b>\n"
    "A 2.00% buyer fee is added to the subscription price the member pays, and a 3.30% "
    "admin fee is deducted from the admin's payout. Both are shown before any payment.\n\n"

    "<b>Data we store</b>\n"
    "Your Telegram user ID, first name, username, subscription records, and "
    "payment transaction hashes. We do not store private keys or wallet seeds. "
    "On-chain data is publicly visible on the TON blockchain by its nature.\n\n"

    "<b>Data we share</b>\n"
    "Only with TonCenter (on-chain verification) and CoinGecko (exchange rates). "
    "We never sell your data.\n\n"

    "<b>Your rights</b>\n"
    "You may request access to or deletion of your off-chain data at any time "
    "by contacting support. On-chain data is immutable.\n\n"

    "<b>Age</b>\n"
    "You must be 18 or older to use this service.\n\n"

    "By tapping <b>✅ I Agree</b> you confirm you have read and accept these "
    "terms. Tap <b>❌ Decline</b> to exit without proceeding."
)

def _terms_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I Agree",  callback_data="terms:accept")],
        [InlineKeyboardButton("❌ Decline",  callback_data="terms:decline")],
    ])


async def _show_terms(update: Update) -> None:
    """Send the ToS message regardless of whether this is a fresh message or callback."""
    if update.callback_query:
        await update.callback_query.edit_message_text(
            _TERMS_TEXT, parse_mode="HTML", reply_markup=_terms_kb(),
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text(
            _TERMS_TEXT, parse_mode="HTML", reply_markup=_terms_kb(),
            disable_web_page_preview=True,
        )


async def cb_terms_accept(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """User tapped ✅ I Agree — record acceptance then continue to normal /start flow."""
    query = update.callback_query
    await query.answer("Terms accepted ✅")

    from renewise.db.queries import accept_terms, upsert_user
    user = update.effective_user
    # Ensure the user row exists before marking acceptance
    await upsert_user(
        telegram_user_id=user.id,
        first_name=user.first_name,
        username=user.username,
    )
    await accept_terms(user.id)
    log.info("ToS accepted by user %d (@%s)", user.id, user.username or "")

    # If the user was sent to ToS from the create_paywall flow, resume it directly.
    # Otherwise fall back to the standard /start screen.
    resume = ctx.user_data.pop("tos_resume", None)
    if resume == "create_paywall":
        from renewise.handlers.create_paywall import cmd_create_paywall
        await cmd_create_paywall(update, ctx)
    else:
        await _start_inner(update, ctx)


async def cb_terms_decline(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """User tapped ❌ Decline — acknowledge and do nothing else."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "No problem. You can come back any time by sending /start.\n\n"
        "Renewise will not process any of your data until you agree to the Terms.",
    )


async def post_init(app: Application) -> None:
    await init_db()
    # Migrate watcher tables (idempotent safe to run every startup)
    try:
        from renewise.watcher.db import migrate as watcher_migrate
        await watcher_migrate()
        log.info("Watcher DB tables ready.")
    except Exception as exc:
        log.warning("Watcher DB migration skipped: %s", exc)

    # ── TRIGGER_MNEMONIC startup check ────────────────────────────────────────
    # Warn loudly so this is never a silent gap in production.
    from renewise.config import TRIGGER_MNEMONIC as _TM
    if not _TM:
        log.warning(
            "⚠️  TRIGGER_MNEMONIC is not set — refund auto-trigger DISABLED. "
            "Overpayments will be queued in overpayment_refunds (status=pending_send) "
            "and require manual superadmin processing. "
            "Set TRIGGER_MNEMONIC in .env to enable trustless on-chain refunds."
        )
    else:
        log.info("Refund trigger wallet configured — on-chain auto-refunds enabled.")

    # Start the Redis pub/sub listener for watcher → bot actions.
    # Only attempt if Redis is reachable — skip silently when running without Redis
    # (e.g. dev/testnet mode using the in-process watcher instead).
    try:
        import redis.asyncio as _aioredis
        from renewise.watcher.config import REDIS_URL
        _r = _aioredis.from_url(REDIS_URL)
        await _r.ping()
        await _r.aclose()
        # Redis is up — start the listener
        from renewise.watcher.bot_listener import start_bot_listener
        app.bot_data["listener_task"] = start_bot_listener(app)
        log.info("Bot listener started (Redis connected).")
    except Exception:
        log.info("Redis not available — bot listener skipped (in-process watcher handles payments).")

    log.info("Database initialised.")


async def post_shutdown(app: Application) -> None:
    """Cancel background tasks that were started in post_init."""
    listener_task: asyncio.Task | None = app.bot_data.get("listener_task")
    if listener_task and not listener_task.done():
        listener_task.cancel()
        try:
            await listener_task
        except (asyncio.CancelledError, Exception):
            pass
        log.info("Bot listener task cancelled.")


# ── /start ────────────────────────────────────────────────────────────────────

async def _send_renewal_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE, group_id: int, user_telegram_id: int):
    import io as _io
    from renewise.db import queries
    from renewise.handlers.join_request import _fetch_qr_bytes
    from renewise.utils.keyboards import payment_details_kb, _support_url

    try:
        user_db = await queries.get_user_by_telegram_id(user_telegram_id)
        if not user_db:
            msg = "No active subscription found for that group."
            if update.callback_query:
                await update.callback_query.edit_message_text(msg)
            else:
                await update.message.reply_text(msg)
            return

        sub = await queries.get_subscription(user_db["id"], group_id)
        if not sub:
            msg = "No active subscription found for that group."
            if update.callback_query:
                await update.callback_query.edit_message_text(msg)
            else:
                await update.message.reply_text(msg)
            return

        from renewise.services.payment import generate_payment_request
        payment = await generate_payment_request(user_telegram_id, group_id)

        # Store in pending_joins so cb_ive_paid can confirm the renewal
        pending = ctx.bot_data.get("pending_joins", {}).get(user_telegram_id, {})
        try:
            from renewise.db.queries import get_group_by_id as _get_group_by_id
            _grp = await _get_group_by_id(group_id)
            pending.setdefault("chat_id",    _grp["telegram_chat_id"] if _grp else None)
            pending.setdefault("group_name", _grp["chat_title"] or "the group" if _grp else "the group")
            pending.setdefault("interval",   _grp["billing_interval_days"] or 30 if _grp else 30)
        except Exception:
            pass
        ctx.bot_data.setdefault("pending_joins", {})[user_telegram_id] = {
            **pending,
            "group_id":   group_id,
            "payment_url": payment.payment_url,
            "vault_addr":  payment.vault_address,
            "amount_ton":  payment.amount,
        }

        # Build fee breakdown caption (same style as new-member payment)
        try:
            from renewise.utils.coingecko import get_ton_usd_price
            from renewise.db.queries import get_global_fees, get_group_by_id as _get_group
            group_row    = await _get_group(group_id)
            ton_rate     = await get_ton_usd_price()
            global_buyer_bps, _ = await get_global_fees()
            buyer_bps    = (group_row["buyer_fee_bps"] if group_row and group_row["buyer_fee_bps"] is not None
                            else global_buyer_bps)
            buyer_pct    = buyer_bps / 100.0
            price_usd    = (group_row["price_usd_cents"] / 100.0) if group_row else 0.0
            fee_usd      = price_usd * buyer_bps / 10000
            total_usd    = price_usd + fee_usd
            exact_ton    = payment.required_nano / 1_000_000_000
            fee_breakdown = (
                f"<b>Price breakdown:</b>\n"
                f"  Subscription: <b>${price_usd:.2f}</b>\n"
                f"  Service fee ({buyer_pct:.2f}%): <b>${fee_usd:.2f}</b>\n"
                f"  ────────────────\n"
                f"  Total: <b>${total_usd:.2f} ≈ {exact_ton:.6f} TON</b>\n\n"
            )
        except Exception:
            exact_ton     = payment.required_nano / 1_000_000_000
            fee_breakdown = ""

        caption = (
            f"🔄 <b>Renew Subscription</b>\n\n"
            f"{fee_breakdown}"
            f"<b>Amount to send:</b>\n"
            f"<code>{exact_ton:.9f} TON</code>\n\n"
            f"Choose how to pay:\n"
            f"• <b>Telegram Wallet</b> — tap the button below, instant.\n"
            f"• <b>Other TON Wallet</b> — tap the button below, opens your installed TON wallet app.\n"
            f"• <b>Scan QR code</b> — use a TON wallet on a second device.\n\n"
            f"⚠️ <b>A TON wallet is required to pay.</b>\n\n"
            f"<i>Your subscription will automatically extend once the transaction confirms.</i>"
        )

        kb = payment_details_kb(payment.payment_url, _support_url())

        # Delete any prior renewal message to avoid duplicates
        prev_msg_id = ctx.bot_data.get("pending_joins", {}).get(user_telegram_id, {}).get("payment_msg_id")
        if prev_msg_id:
            try:
                await ctx.bot.delete_message(chat_id=user_telegram_id, message_id=prev_msg_id)
            except Exception:
                pass

        try:
            qr_bytes = await _fetch_qr_bytes(payment.payment_url)
            qr_file  = _io.BytesIO(qr_bytes)
            qr_file.name = "renewal_qr.png"
            if update.callback_query:
                await update.callback_query.answer()
                sent = await ctx.bot.send_photo(
                    chat_id=user_telegram_id,
                    photo=qr_file,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            else:
                sent = await update.message.reply_photo(
                    photo=qr_file,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            ctx.bot_data.setdefault("pending_joins", {}).setdefault(user_telegram_id, {})["payment_msg_id"] = sent.message_id
        except Exception as e:
            log.warning("_send_renewal_payment: QR failed, sending text: %s", e)
            if update.callback_query:
                sent = await update.callback_query.edit_message_text(caption, parse_mode="HTML", reply_markup=kb)
            else:
                sent = await update.message.reply_text(caption, parse_mode="HTML", reply_markup=kb)
            ctx.bot_data.setdefault("pending_joins", {}).setdefault(user_telegram_id, {})["payment_msg_id"] = sent.message_id

    except Exception as e:
        log.error("Error generating renewal link for user_id=%s group_id=%s: %s", user_telegram_id, group_id, e)
        msg = "Something went wrong generating your renewal link — please try again or use /menu."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)

async def _send_payment_details(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    group_id: int,
    user_telegram_id: int,
) -> None:
    """
    Generate a fresh payment link and send the QR + details to the user.
    Called from the /start pay_{group_id} deep-link handler.
    By the time this runs the user has opened the bot DM, so send_message
    and send_photo are guaranteed to work.
    """
    import io as _io
    from renewise.services.payment import generate_payment_request
    from renewise.utils.keyboards import payment_details_kb, _support_url
    from renewise.utils.coingecko import get_ton_usd_price
    from renewise.handlers.join_request import _fetch_qr_bytes

    # Generate fresh payment request (gets live exchange rate)
    payment = await generate_payment_request(user_telegram_id, group_id)

    # Store in pending_joins so cb_ive_paid can find it.
    # Preserve chat_id / group_name written earlier by handle_join_request;
    # only overwrite the payment-specific keys.
    pending = ctx.bot_data.get("pending_joins", {}).get(user_telegram_id, {})
    # Resolve chat_id / group_name if they're not already in the session
    # (e.g. user arrived via a direct pay_ deep-link without a prior join-request).
    if "chat_id" not in pending or "group_name" not in pending:
        try:
            from renewise.db.queries import get_group_by_id as _get_group_by_id
            _grp = await _get_group_by_id(group_id)
            pending.setdefault("chat_id",    _grp["telegram_chat_id"] if _grp else None)
            pending.setdefault("group_name", _grp["chat_title"] or "the group" if _grp else "the group")
            pending.setdefault("interval",   _grp["billing_interval_days"] or 30 if _grp else 30)
        except Exception:
            pass
    ctx.bot_data.setdefault("pending_joins", {})[user_telegram_id] = {
        **pending,
        "group_id":    group_id,
        "payment_url": payment.payment_url,
        "vault_addr":  payment.vault_address,
        "amount_ton":  payment.amount,
    }

    # payment.payment_url is already ton:// with &init= — use it for both the
    # button and the QR code. No conversion needed.
    ton_qr_url = payment.payment_url

    # Build fee breakdown for display.
    # payment.required_nano is the exact amount the vault enforces on-chain
    # (price + buyer_fee + gas_reserve). We show price vs fee vs total clearly.
    try:
        from renewise.utils.coingecko import get_ton_usd_price
        from renewise.db.queries import get_global_fees, get_group_by_id as _get_group
        from renewise.ton.vault import MIN_GAS_RESERVE_NANO
        group_row    = await _get_group(group_id)
        ton_rate     = await get_ton_usd_price()
        global_buyer_bps, _ = await get_global_fees()
        buyer_bps    = (group_row["buyer_fee_bps"] if group_row and group_row["buyer_fee_bps"] is not None
                        else global_buyer_bps)
        buyer_pct    = buyer_bps / 100.0          # 200 bps → 2.00
        price_usd    = (group_row["price_usd_cents"] / 100.0) if group_row else 0.0
        fee_usd      = price_usd * buyer_bps / 10000
        total_usd    = price_usd + fee_usd
        # Exact TON from required_nano (what the wallet will actually deduct)
        exact_ton    = payment.required_nano / 1_000_000_000
        exact_usd    = exact_ton * ton_rate
        fee_breakdown = (
            f"<b>Price breakdown:</b>\n"
            f"  Subscription: <b>${price_usd:.2f}</b>\n"
            f"  Service fee ({buyer_pct:.2f}%): <b>${fee_usd:.2f}</b>\n"
            f"  ────────────────\n"
            f"  Total: <b>${total_usd:.2f} ≈ {exact_ton:.6f} TON</b>\n\n"
        )
    except Exception:
        exact_ton     = payment.required_nano / 1_000_000_000
        fee_breakdown = ""

    caption = (
        f"📲 <b>Payment Details</b>\n\n"
        f"{fee_breakdown}"
        f"<b>Amount to send:</b>\n"
        f"<code>{exact_ton:.9f} TON</code>\n\n"
        f"Choose how to pay:\n"
        f"• <b>Telegram Wallet</b> — tap the button below, instant.\n"
        f"• <b>Other TON Wallet</b> — tap the button below, opens your installed TON wallet app with everything pre-filled.\n"
        f"• <b>Scan QR code</b> — use a TON wallet on a second device.\n\n"
        f"⚠️ <b>A TON wallet is required to pay.</b> Plain bank transfers or crypto "
        f"exchanges will not activate your subscription.\n\n"
        f"Send the <b>exact amount shown</b> — sending less won't activate your subscription, "
        f"sending more triggers an automatic partial refund."
    )

    kb = payment_details_kb(payment.payment_url, _support_url())

    # Delete any previously sent payment message to avoid duplicates.
    # (User may tap the "Pay Now" deep-link URL button more than once.)
    prev_msg_id = ctx.bot_data.get("pending_joins", {}).get(user_telegram_id, {}).get("payment_msg_id")
    if prev_msg_id:
        try:
            await ctx.bot.delete_message(chat_id=user_telegram_id, message_id=prev_msg_id)
        except Exception:
            pass  # already deleted or too old — harmless

    try:
        qr_bytes = await _fetch_qr_bytes(ton_qr_url)
        qr_file  = _io.BytesIO(qr_bytes)
        qr_file.name = "payment_qr.png"
        if update.callback_query:
            await update.callback_query.answer()
            sent = await ctx.bot.send_photo(
                chat_id=user_telegram_id,
                photo=qr_file,
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb,
            )
        else:
            sent = await update.message.reply_photo(
                photo=qr_file,
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb,
            )
        # Store message_id so a second tap of the deep-link replaces this message
        ctx.bot_data.setdefault("pending_joins", {}).setdefault(user_telegram_id, {})["payment_msg_id"] = sent.message_id
    except Exception as e:
        log.warning("_send_payment_details: QR failed, sending text: %s", e)
        if update.callback_query:
            sent = await update.callback_query.edit_message_text(
                caption, parse_mode="HTML", reply_markup=kb
            )
        else:
            sent = await update.message.reply_text(caption, parse_mode="HTML", reply_markup=kb)
        ctx.bot_data.setdefault("pending_joins", {}).setdefault(user_telegram_id, {})["payment_msg_id"] = sent.message_id


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please use /start in a DM with me.")
        return

    from renewise.db.queries import has_accepted_terms, upsert_user
    user = update.effective_user
    await upsert_user(
        telegram_user_id=user.id,
        first_name=user.first_name,
        username=user.username,
    )

    # ── ToS gate ──────────────────────────────────────────────────────────────
    # Only shown when the user is about to create a paywall (either fresh /start
    # with no args, or explicitly via start:create_paywall callback).
    # Members arriving via pay_<id> or renew_<id> deep links are NOT gated —
    # they just want to pay, not sign up as a platform operator.
    arg = ctx.args[0] if ctx.args else ""
    is_payment_deep_link = arg.startswith("pay_") or arg.startswith("renew_")

    if not is_payment_deep_link and not await has_accepted_terms(user.id):
        await _show_terms(update)
        return

    await _start_inner(update, ctx)


async def _start_inner(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Core /start logic — only reached after ToS is accepted."""
    from renewise.db import queries

    # Helper: send or edit depending on whether we came via a callback (ToS accept)
    async def _reply(text: str, **kwargs):
        if update.callback_query:
            await update.callback_query.edit_message_text(text, **kwargs)
        else:
            await update.message.reply_text(text, **kwargs)

    # Handle deep links like /start renew_123 or /start reminders
    if ctx.args:
        arg = ctx.args[0]
        if arg.startswith("renew_"):
            try:
                group_id = int(arg.split("_")[1])
                await _send_renewal_payment(update, ctx, group_id, update.effective_user.id)
            except Exception:
                await _reply("Invalid renewal link.")
            return
        elif arg.startswith("pay_"):
            try:
                group_id = int(arg.split("_")[1])
                await _send_payment_details(update, ctx, group_id, update.effective_user.id)
            except Exception as e:
                log.error("pay_ deep-link failed for user %s group %s: %s",
                          update.effective_user.id, arg, e)
                await _reply("Something went wrong generating your payment link. Please try again.")
            return
        elif arg == "reminders":
            await _reply(
                "🔔 <b>Renewal Reminders are Active!</b>\n\n"
                "You will automatically receive a direct message from me 24 hours before your subscription expires. "
                "You can renew directly through that message using your TON wallet.\n\n"
                "If your subscription expires, you'll be safely removed from the group, but you can always rejoin later by purchasing a new subscription.",
                parse_mode="HTML"
            )
            return
        elif arg == "support":
            await _reply(
                "💬 <b>Contact Support</b>\n\n"
                "To contact our support team, please send a message starting with <b>support:</b> followed by your question or issue.\n\n"
                "<i>Example:</i>\nsupport: I need help setting up my paywall",
                parse_mode="HTML"
            )
            return

    user_id = update.effective_user.id
    groups  = await queries.get_groups_for_admin(user_id)
    subs    = await queries.get_user_subscriptions(user_id)
    
    is_admin = len(groups) > 0
    is_member = len(subs) > 0

    if not is_admin and not is_member:
        # First-time user
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛠️ Set Up My First Paywall", callback_data="start:create_paywall")],
            [InlineKeyboardButton("❓ How This Works",           callback_data="start:how_it_works")],
        ])
        await _reply(
            "👋 <b>Welcome to Renewise!</b>\n\n"
            "I turn your Telegram group or channel into a paid community "
            "members pay in GRAM, you get paid directly, no middleman holding your funds.\n\n"
            "Ready to set one up? It takes about 2 minutes.",
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        buttons = []
        if is_admin:
            buttons.append([InlineKeyboardButton("📋 My Networks", callback_data="start:my_groups")])
        if is_member:
            buttons.append([InlineKeyboardButton("📋 My Subscriptions", callback_data="start:my_subs")])
            
        if is_admin:
            buttons.append([InlineKeyboardButton("🛠️ Set Up Another", callback_data="start:create_paywall")])
        else:
            buttons.append([InlineKeyboardButton("🛠️ Set Up a Paywall", callback_data="start:create_paywall")])
            
        buttons.append([InlineKeyboardButton("❓ How This Works", callback_data="start:how_it_works")])
        kb = InlineKeyboardMarkup(buttons)
        
        if is_admin and not is_member:
            n = len(groups)
            label = "community" if n == 1 else "communities"
            lines = [f"👋 <b>Welcome back!</b>\n", f"You're managing <b>{n}</b> paywalled {label}:\n"]
            for g in groups:
                try:
                    chat = await ctx.bot.get_chat(g["telegram_chat_id"])
                    title = chat.title or "Unknown Chat"
                except Exception:
                    title = f"Chat {g['telegram_chat_id']}"
                    
                g_dict = dict(g)
                usd = g_dict.get("price_usd_cents", 0) / 100.0
                wallet = g_dict.get("payout_wallet_address") or "Not set"
                if len(wallet) > 20:
                    wallet = f"{wallet[:6]}...{wallet[-4:]}"
                    
                lines.append(f"• <b>{title}</b>")
                lines.append(f"  💰 ${usd:.2f} USD")
                lines.append(f"  💎 <code>{wallet}</code>\n")
            dashboard_text = "\n".join(lines)
        elif is_member and not is_admin:
            n_subs = len(subs)
            dashboard_text = f"👋 <b>Welcome back!</b>\n\nYou have <b>{n_subs}</b> active or pending subscription(s)."
        else:
            n_admin = len(groups)
            n_subs = len(subs)
            label = "community" if n_admin == 1 else "communities"
            dashboard_text = f"👋 <b>Welcome back!</b>\n\nYou're managing <b>{n_admin}</b> paywalled {label} and have <b>{n_subs}</b> subscription(s)."
        
        await _reply(
            dashboard_text,
            parse_mode="HTML",
            reply_markup=kb,
        )


# ── start callbacks ───────────────────────────────────────────────────────────

async def cb_start_my_groups(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    from renewise.db import queries
    from telegram.error import BadRequest as TgBadRequest
    groups = await queries.get_groups_for_admin(update.effective_user.id)

    if not groups:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛠️ Set Up a Paywall", callback_data="start:create_paywall")],
            [InlineKeyboardButton("◀️ Back",              callback_data="start:back")],
        ])
        try:
            await query.edit_message_text(
                "You haven't set up any paywalls yet.\n"
                "Ready to monetize your group or channel?",
                reply_markup=kb,
            )
        except TgBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    else:
        from renewise.handlers.admin_menu import _show_network_picker
        await _show_network_picker(update, ctx)


async def cb_start_how_it_works(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠️ Set Up a Paywall", callback_data="start:create_paywall")],
        [InlineKeyboardButton("🔙 Back",             callback_data="start:back")],
    ])
    await query.edit_message_text(
        "❓ <b>How Renewise Works</b>\n\n"
        "<b>1️⃣ Add me as admin</b>\n"
        "Add me to your group or channel as an Admin with the required permissions "
        "(Invite Users, Manage Chat / Post Messages).\n\n"
        "<b>2️⃣ Set your price</b>\n"
        "Choose a USD price and billing interval (weekly or monthly). "
        "Members always pay the equivalent amount in GRAM at the live exchange rate.\n\n"
        "<b>3️⃣ Members pay and I handle the rest</b>\n"
        "Share your unique invite link. When someone pays, I auto-approve their join request "
        "and send them a welcome message. No manual work needed.\n\n"
        "<b>4️⃣ Automatic renewal & removal</b>\n"
        "Before their subscription expires, members get a renewal reminder. "
        "If they don't renew, I remove them automatically. "
        "You receive payouts directly to your TON wallet no middleman.",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def cb_start_my_subs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    from renewise.db import queries
    from renewise.config import REMINDER_WINDOW_DAYS
    from datetime import datetime, timezone, timedelta

    subs = await queries.get_user_subscriptions(update.effective_user.id)

    if not subs:
        await query.edit_message_text(
            "You have no active subscriptions.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="start:back")]]),
        )
        return

    lines = ["📋 <b>My Subscriptions</b>\n"]
    buttons = []
    now = datetime.now(timezone.utc)

    for s in subs:
        title    = s["chat_title"] or f"Group {s['group_id']}"
        status   = s["subscription_status"]
        date_str = s["next_renewal_date"] or "N/A"
        usd      = s["price_usd_cents"] / 100.0 if s["price_usd_cents"] else 0

        status_icon = "✅" if status == "active" else "⏳" if status == "pending" else "❄️"
        lines.append(f"{status_icon} <b>{title}</b>")
        lines.append(f"   Status: {status.title()}")
        lines.append(f"   Renews: {date_str} (${usd:.2f})")
        lines.append("")

        # Only show Renew button if within the reminder window OR already expired.
        # This avoids confusion when someone pays days before it's needed.
        show_renew = False
        if status in ("active", "pending") and s["next_renewal_date"]:
            try:
                renewal_dt = datetime.fromisoformat(s["next_renewal_date"].replace("Z", "+00:00"))
                if renewal_dt.tzinfo is None:
                    renewal_dt = renewal_dt.replace(tzinfo=timezone.utc)
                days_left = (renewal_dt - now).total_seconds() / 86400
                show_renew = days_left <= REMINDER_WINDOW_DAYS
            except Exception:
                show_renew = False
        elif status in ("expired", "cancelled"):
            show_renew = True

        if show_renew:
            buttons.append([InlineKeyboardButton(f"🔄 Renew {title}", callback_data=f"renew_{s['group_id']}")])

    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="start:back")])

    await query.edit_message_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )

async def cb_renew(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.split("_")[1])
    await _send_renewal_payment(update, ctx, group_id, update.effective_user.id)

async def cb_start_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Return to the dynamic /start screen."""
    query = update.callback_query
    await query.answer()

    from renewise.db import queries
    user_id = update.effective_user.id
    groups  = await queries.get_groups_for_admin(user_id)
    subs    = await queries.get_user_subscriptions(user_id)
    
    is_admin = len(groups) > 0
    is_member = len(subs) > 0

    if not is_admin and not is_member:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛠️ Set Up My First Paywall", callback_data="start:create_paywall")],
            [InlineKeyboardButton("❓ How This Works",           callback_data="start:how_it_works")],
        ])
        text = (
            "👋 <b>Welcome to Renewise!</b>\n\n"
            "I turn your Telegram group or channel into a paid community "
            "members pay in GRAM, you get paid directly, no middleman holding your funds.\n\n"
            "Ready to set one up? It takes about 2 minutes."
        )
    else:
        buttons = []
        if is_admin:
            buttons.append([InlineKeyboardButton("📋 Manage Dashboard", callback_data="start:my_groups")])
        if is_member:
            buttons.append([InlineKeyboardButton("📋 My Subscriptions", callback_data="start:my_subs")])
            
        if is_admin:
            buttons.append([InlineKeyboardButton("🛠️ Set Up Another", callback_data="start:create_paywall")])
        else:
            buttons.append([InlineKeyboardButton("🛠️ Set Up a Paywall", callback_data="start:create_paywall")])
            
        buttons.append([InlineKeyboardButton("❓ How This Works", callback_data="start:how_it_works")])
        kb = InlineKeyboardMarkup(buttons)
        
        if is_admin and not is_member:
            n = len(groups)
            label = "community" if n == 1 else "communities"
            lines = [f"👋 <b>Welcome back!</b>\n", f"You're managing <b>{n}</b> paywalled {label}:\n"]
            for g in groups:
                try:
                    chat = await ctx.bot.get_chat(g["telegram_chat_id"])
                    title = chat.title or "Unknown Chat"
                except Exception:
                    title = f"Chat {g['telegram_chat_id']}"
                    
                g_dict = dict(g)
                usd = g_dict.get("price_usd_cents", 0) / 100.0
                wallet = g_dict.get("payout_wallet_address") or "Not set"
                if len(wallet) > 20:
                    wallet = f"{wallet[:6]}...{wallet[-4:]}"
                    
                lines.append(f"• <b>{title}</b>")
                lines.append(f"  💰 ${usd:.2f} USD")
                lines.append(f"  💎 <code>{wallet}</code>\n")
            text = "\n".join(lines)
        elif is_member and not is_admin:
            n_subs = len(subs)
            text = f"👋 <b>Welcome back!</b>\n\nYou have <b>{n_subs}</b> active or pending subscription(s)."
        else:
            n_admin = len(groups)
            n_subs = len(subs)
            label = "community" if n_admin == 1 else "communities"
            text = f"👋 <b>Welcome back!</b>\n\nYou're managing <b>{n_admin}</b> paywalled {label} and have <b>{n_subs}</b> subscription(s)."

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


# ── app assembly ──────────────────────────────────────────────────────────────

def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_terms_accept,       pattern=r"^terms:accept$"))
    app.add_handler(CallbackQueryHandler(cb_terms_decline,      pattern=r"^terms:decline$"))
    app.add_handler(CallbackQueryHandler(cb_start_my_groups,    pattern=r"^start:my_groups$"))
    app.add_handler(CallbackQueryHandler(cb_start_my_subs,      pattern=r"^start:my_subs$"))
    app.add_handler(CallbackQueryHandler(cb_renew,              pattern=r"^renew_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_start_how_it_works, pattern=r"^start:how_it_works$"))
    app.add_handler(CallbackQueryHandler(cb_start_back,         pattern=r"^start:back$"))

    # ── my_chat_member: bot added/removed as admin ────────────────────────────
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # ── join requests from members ────────────────────────────────────────────
    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    app.add_handler(CallbackQueryHandler(cb_pay_now,         pattern=r"^join:pay_now$"))
    app.add_handler(CallbackQueryHandler(cb_ive_paid,        pattern=r"^join:paid$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_payment,  pattern=r"^join:cancel$"))

    # ── /createpaywall wizard (Steps 1-7) ─────────────────────────────────────
    app.add_handler(build_create_paywall_handler())

    # ── /menu + all admin sub-flows ───────────────────────────────────────────
    register_menu_callbacks(app)

    # ── refund wallet collection ──────────────────────────────────────────────
    # Inline button prompt callback first, then low-priority message handler.
    from telegram.ext import MessageHandler, filters
    app.add_handler(CallbackQueryHandler(cb_refund_prompt, pattern=r"^refund:prompt_\d+$"))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_refund_wallet_message,
    ))

    log.info("renewise bot starting…")
    app.run_polling(allowed_updates=[
        "message",
        "callback_query",
        "chat_member",
        "my_chat_member",
        "chat_join_request",
    ])


if __name__ == "__main__":
    # On Windows, the default ProactorEventLoop raises 'RuntimeError: Event loop
    # is closed' on shutdown due to pending asyncio cleanup tasks.  Switching to
    # SelectorEventLoop (which PTB also recommends for Windows) prevents this.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    main()


"""
Admin menu /menu command + all inline-button sub-flows.

Sub-flows that need text input (Update Price, Update Wallet, Comp a Member)
use their own ConversationHandlers so they don't interfere with the wizard.
"""
from __future__ import annotations
import html
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters,
)
from renewise.db import queries
from renewise.services.wallet import validate_ton_address
from renewise.utils.keyboards import (
    main_menu_kb,
    confirm_price_kb,
    network_select_kb,
    network_detail_kb,
    member_action_kb,
    members_nav_kb,
    payment_history_nav_kb,
    cancel_input_kb,
    wallet_change_alert_kb,
)
from renewise.config import MEMBERS_PAGE_SIZE

log = logging.getLogger(__name__)

# ── conversation states ───────────────────────────────────────────────────────
AWAIT_NEW_PRICE, AWAIT_PRICE_CONFIRM = range(2)
AWAIT_NEW_WALLET = 10
AWAIT_WALLET_PIN = 11
AWAIT_COMP_USERNAME = 20
AWAIT_SUPPORT_MSG = 30
AWAIT_DELETE_CONFIRM_NAME = 40
AWAIT_GROUP_PASSKEY_NEW = 50
AWAIT_GROUP_PASSKEY_CURRENT = 51


# ── helpers ───────────────────────────────────────────────────────────────────

async def _require_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str) -> dict | None:
    """Fetch admin's network. If multiple, show a picker and return None."""
    user_id = update.effective_user.id
    groups = await queries.get_groups_for_admin(user_id)

    if not groups:
        text = "⚠️ You don't have any active networks set up. Use /createpaywall first."
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return None

    if len(groups) == 1:
        return dict(groups[0])

    # Multiple networks — check if one was already selected via callback data
    query = update.callback_query
    if query and query.data and query.data.startswith("grpsel:"):
        _, gid_str, _ = query.data.split(":", 2)
        selected_id = int(gid_str)
        for g in groups:
            if g["id"] == selected_id:
                return dict(g)

    # Not yet selected — show network picker
    await _show_network_picker(update, ctx)
    return None


async def _show_network_picker(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send/edit the network selection screen."""
    user_id = update.effective_user.id
    groups = await queries.get_groups_for_admin(user_id)

    if not groups:
        text = "⚠️ You don't have any active networks set up. Use /createpaywall first."
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return

    formatted = []
    for g in groups:
        try:
            chat = await ctx.bot.get_chat(g["telegram_chat_id"])
            title = html.escape(chat.title or str(g["telegram_chat_id"]))
        except Exception:
            title = str(g["telegram_chat_id"])
        formatted.append({"id": g["id"], "title": title})

    kb = network_select_kb(formatted, action="menu:home")
    text = "🌐 <b>My Networks</b>\n\nSelect a network to manage:"

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def _get_network_detail_text(ctx: ContextTypes.DEFAULT_TYPE, g_dict: dict) -> str:
    """Render the detail block for a single network."""
    try:
        chat = await ctx.bot.get_chat(g_dict["telegram_chat_id"])
        title = html.escape(chat.title or str(g_dict["telegram_chat_id"]))
        chat_type = "📢 Channel" if chat.type == "channel" else "👥 Group"
    except Exception:
        title = str(g_dict["telegram_chat_id"])
        chat_type = "🌐"

    usd = g_dict.get("price_usd_cents", 0) / 100.0
    wallet = g_dict.get("payout_wallet_address") or "Not set"
    if len(wallet) > 20:
        wallet = f"{wallet[:6]}...{wallet[-4:]}"

    invite_link = g_dict.get("invite_link")
    if not invite_link:
        try:
            invite_link_obj = await ctx.bot.create_chat_invite_link(
                chat_id=g_dict["telegram_chat_id"],
                creates_join_request=True,
            )
            invite_link = invite_link_obj.invite_link
            await queries.update_group_invite_link(g_dict["id"], invite_link)
        except Exception as e:
            log.warning("Failed to generate invite link for %s: %s", g_dict["telegram_chat_id"], e)
            invite_link = "Not generated"

    member_count = await queries.count_members(g_dict["id"])
    status = g_dict.get("status", "unknown")
    status_icon = "✅" if status == "active" else "⏸" if status == "paused" else "❄️"
    interval = g_dict.get("billing_interval_days", 30)

    lines = [
        "⚙️ <b>Network Dashboard</b>\n",
        f"{status_icon} {chat_type} <b>{title}</b>",
        f"  💰 ${usd:.2f} USD / {interval} days",
        f"  👥 {member_count} active member{'s' if member_count != 1 else ''}",
        f"  👛 <code>{wallet}</code>",
        f"  🔗 Link: <code>{invite_link}</code>\n",
        "What would you like to do?",
    ]
    return "\n".join(lines)


# ── /menu entry ───────────────────────────────────────────────────────────────

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please use /menu in a DM with me.")
        return

    groups = await queries.get_groups_for_admin(update.effective_user.id)
    if len(groups) == 1:
        # Skip picker for single-network admins
        g_dict = dict(groups[0])
        text = await _get_network_detail_text(ctx, g_dict)
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=network_detail_kb(g_dict["id"]),
        )
    else:
        await _show_network_picker(update, ctx)


async def cb_menu_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """◀️ Back always returns to the network picker."""
    query = update.callback_query
    await query.answer()
    await _show_network_picker(update, ctx)


async def cb_network_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped a network from the picker → show its detail screen."""
    query = update.callback_query
    await query.answer()
    # callback_data: grpsel:<group_id>:menu:home
    parts = query.data.split(":")
    group_id = int(parts[1])
    group = await queries.get_group_by_id(group_id)
    if not group:
        await query.edit_message_text("⚠️ Network not found.")
        return
    # Verify ownership
    if group["admin_telegram_id"] != update.effective_user.id:
        await query.edit_message_text("❌ Unauthorized.")
        return
    g_dict = dict(group)
    text = await _get_network_detail_text(ctx, g_dict)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=network_detail_kb(group_id))


# ── Stats ─────────────────────────────────────────────────────────────────────

async def cb_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group = await _require_group(update, ctx, action="menu:stats")
    if not group:
        return

    # Resolve human-readable chat title
    try:
        chat = await ctx.bot.get_chat(group["telegram_chat_id"])
        chat_title = html.escape(chat.title or str(group["telegram_chat_id"]))
        chat_type = "📢 Channel" if chat.type == "channel" else "👥 Group"
    except Exception:
        chat_title = str(group["telegram_chat_id"])
        chat_type = "💬"

    active_count = await queries.count_members(group["id"])
    usd = (group.get("price_usd_cents") or 0) / 100.0
    renewals = await queries.get_upcoming_renewals(group["id"], limit=3)

    renewal_lines = "\n".join(
        f"  • User {r['telegram_user_id']} — {r['next_renewal_date'] or 'N/A'}"
        for r in renewals
    ) or "  (none)"

    await query.edit_message_text(
        f"📊 <b>Stats {chat_type} {chat_title}</b>\n\n"
        f"👥 Active members: <b>{active_count}</b>\n"
        f"💰 Price: <b>${usd:.2f} USD/cycle</b>\n\n"
        f"📅 Next renewals:\n{renewal_lines}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Back", callback_data=f"grpsel:{group['id']}:menu:home")
        ]]),
    )


# ── Update Price ──────────────────────────────────────────────────────────────

async def cb_update_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    group = await _require_group(update, ctx, action="menu:update_price")
    if not group:
        return ConversationHandler.END
    ctx.user_data["price_group"] = group  # type: ignore[index]
    
    current_usd = group.get('price_usd_cents', 0) / 100.0
    await query.edit_message_text(
        f"Current price: <b>${current_usd:.2f} USD</b> / billing cycle\n\n"
        "Send the new price in USD (positive number).\n"
        "<i>Existing subscribers keep their locked-in price until next renewal.</i>",
        parse_mode="HTML",
        reply_markup=cancel_input_kb(),
    )
    return AWAIT_NEW_PRICE


async def msg_new_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lstrip("$")
    try:
        usd_price = float(text)
        if usd_price < 1.0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid price. Send a positive number (minimum $1.00), e.g. <code>9.99</code>.",
            parse_mode="HTML",
            reply_markup=cancel_input_kb(),
        )
        return AWAIT_NEW_PRICE

    ctx.user_data["new_price_usd"] = usd_price  # type: ignore[index]
    
    from renewise.utils.coingecko import get_ton_usd_price
    ton_price_usd = await get_ton_usd_price()
    equivalent_gram = usd_price / ton_price_usd
    
    await update.message.reply_text(
        f"Set new price to <b>${usd_price:.2f} USD</b> (~{equivalent_gram:.2f} GRAM)?",
        parse_mode="HTML",
        reply_markup=confirm_price_kb(),
    )
    return AWAIT_PRICE_CONFIRM


async def cb_price_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "cancel":
        ctx.user_data.pop("new_price_usd", None)  # type: ignore[union-attr]
        group = ctx.user_data.pop("price_group", None)  # type: ignore[union-attr]
        if group:
            g_dict = dict(group)
            text = await _get_network_detail_text(ctx, g_dict)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=network_detail_kb(g_dict["id"]))
        else:
            await _show_network_picker(update, ctx)
        return ConversationHandler.END

    group = ctx.user_data.get("price_group")  # type: ignore[union-attr]
    usd_price = ctx.user_data.get("new_price_usd")  # type: ignore[union-attr]
    cents = int(round(usd_price * 100))

    await queries.update_group_price_usd_cents(group["id"], cents)
    await queries.audit(group["id"], "price_updated", update.effective_user.id, {"new_price_usd": usd_price})
    ctx.user_data.pop("new_price_usd", None)  # type: ignore[union-attr]
    ctx.user_data.pop("price_group", None)  # type: ignore[union-attr]
    await query.edit_message_text(
        f"✅ Price updated to <b>${usd_price:.2f} USD</b>.\n"
        "Existing subscribers keep their locked-in rate until renewal.",
        parse_mode="HTML",
        reply_markup=network_detail_kb(group["id"]),
    )
    return ConversationHandler.END


async def cb_cancel_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    # clean up any partial state, capture group if available
    ctx.user_data.pop("new_price_usd", None)  # type: ignore[union-attr]
    group = (
        ctx.user_data.pop("price_group", None)  # type: ignore[union-attr]
        or ctx.user_data.pop("wallet_group", None)  # type: ignore[union-attr]
        or ctx.user_data.pop("comp_group", None)  # type: ignore[union-attr]
    )

    if group:
        g_dict = dict(group)
        text = await _get_network_detail_text(ctx, g_dict)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=network_detail_kb(g_dict["id"]))
    else:
        await _show_network_picker(update, ctx)
    return ConversationHandler.END


def build_update_price_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_update_price, pattern=r"^(menu:update_price|grpsel:\d+:menu:update_price)$")],
        states={
            AWAIT_NEW_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_new_price)],
            AWAIT_PRICE_CONFIRM: [CallbackQueryHandler(cb_price_confirm, pattern=r"^price:")],
        },
        fallbacks=[
            CommandHandler("cancel", lambda u, c: ConversationHandler.END),
            CallbackQueryHandler(cb_cancel_input, pattern=r"^input:cancel$"),
        ],
        per_chat=True,
        per_user=True,
    )


# ── Update Wallet ─────────────────────────────────────────────────────────────

async def cb_update_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    group = await _require_group(update, ctx, action="menu:update_wallet")
    if not group:
        return ConversationHandler.END
    ctx.user_data["wallet_group"] = group  # type: ignore[index]

    has_passkey = bool(group.get("wallet_passcode_hash"))
    passcode_line = "🔒 A passkey is required to confirm." if has_passkey else ""

    await query.edit_message_text(
        f"Current wallet: <code>{group['payout_wallet_address'] or 'not set'}</code>\n\n"
        f"Send your new GRAM wallet address:{f'  {passcode_line}' if passcode_line else ''}",
        parse_mode="HTML",
        reply_markup=cancel_input_kb(),
    )
    return AWAIT_NEW_WALLET


async def msg_new_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    address = update.message.text.strip()
    if not await validate_ton_address(address):
        await update.message.reply_text(
            "❌ Invalid wallet address format. Please try again:",
            parse_mode="HTML",
            reply_markup=cancel_input_kb(),
        )
        return AWAIT_NEW_WALLET

    # Network check: on mainnet, testnet addresses won't receive payments.
    # On testnet, both address types are accepted.
    from renewise.services.wallet import detect_address_network
    from renewise.config import TONCENTER_TESTNET
    addr_network = detect_address_network(address)
    if not TONCENTER_TESTNET and addr_network == "testnet":
        await update.message.reply_text(
            "⚠️ <b>Testnet address detected</b>\n\n"
            "This looks like a <b>testnet</b> wallet address (starts with <code>kQ</code> or <code>0Q</code>).\n\n"
            "The system is running on <b>mainnet</b> — payouts will not reach a testnet wallet.\n\n"
            "Please send your <b>mainnet</b> wallet address (starts with <code>EQ</code> or <code>UQ</code>):",
            parse_mode="HTML",
            reply_markup=cancel_input_kb(),
        )
        return AWAIT_NEW_WALLET

    group = ctx.user_data.get("wallet_group")  # type: ignore[union-attr]

    # If a passkey is set on this group, we need to verify it before scheduling.
    # Store the validated address and ask for the PIN.
    if group.get("wallet_passcode_hash"):
        ctx.user_data["wallet_pending_address"] = address  # type: ignore[index]
        await update.message.reply_text(
            "🔒 <b>Passkey required</b>\n\n"
            "Send your 4-digit wallet passkey to confirm this change:",
            parse_mode="HTML",
            reply_markup=cancel_input_kb(),
        )
        return AWAIT_WALLET_PIN

    # No passkey — schedule immediately
    return await _schedule_wallet_change(update, ctx, group, address)


async def msg_wallet_pin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the 4-digit PIN, verify it, then schedule the wallet change."""
    pin = update.message.text.strip()
    group   = ctx.user_data.get("wallet_group")  # type: ignore[union-attr]
    address = ctx.user_data.pop("wallet_pending_address", None)  # type: ignore[union-attr]

    if not group or not address:
        await update.message.reply_text("❌ Session expired. Please start over.")
        return ConversationHandler.END

    # Verify PIN against the stored hash
    ok = await queries.check_group_wallet_passcode(group["id"], pin)
    if not ok:
        await update.message.reply_text(
            "❌ Incorrect passkey. Please try again:",
            parse_mode="HTML",
            reply_markup=cancel_input_kb(),
        )
        # Restore the pending address so the admin can try the PIN again
        ctx.user_data["wallet_pending_address"] = address  # type: ignore[index]
        return AWAIT_WALLET_PIN

    return await _schedule_wallet_change(update, ctx, group, address)


async def _schedule_wallet_change(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    group: dict,
    address: str,
) -> int:
    """Shared logic: create the pending wallet change row and notify the admin."""
    from datetime import datetime, timezone, timedelta
    from renewise.config import WALLET_CHANGE_DELAY_HOURS

    admin_id  = update.effective_user.id
    old_wallet = group.get("payout_wallet_address")

    now_utc      = datetime.now(timezone.utc)
    activates_at = now_utc + timedelta(hours=WALLET_CHANGE_DELAY_HOURS)
    activates_at_str = activates_at.strftime("%Y-%m-%d %H:%M:%S")

    superseded = await queries.get_superseded_pending_wallet_change(group["id"], admin_id)

    change_id = await queries.create_pending_wallet_change(
        group_id=group["id"],
        old_wallet=old_wallet,
        new_wallet=address,
        requested_by=admin_id,
        activates_at=activates_at_str,
    )

    superseded_row = await queries.get_superseded_pending_wallet_change(group["id"], admin_id)
    if superseded_row and superseded_row["id"] != change_id:
        await queries.audit(
            group["id"], "wallet_change_auto_cancelled", admin_id,
            {
                "superseded_change_id": superseded_row["id"],
                "superseded_wallet":    superseded_row["new_wallet_address"],
                "reason":               "new_request_submitted",
            },
        )

    await queries.audit(
        group["id"], "wallet_change_requested", admin_id,
        {"change_id": change_id, "new_wallet": address},
    )

    ctx.user_data.pop("wallet_group", None)  # type: ignore[union-attr]

    try:
        chat = await ctx.bot.get_chat(group["telegram_chat_id"])
        group_title = chat.title or str(group["telegram_chat_id"])
    except Exception:
        group_title = str(group["telegram_chat_id"])

    await update.message.reply_text(
        f"⚠️ <b>Wallet change scheduled</b>\n\n"
        f"Your payout wallet for <b>{group_title}</b> is scheduled to change to:\n"
        f"<code>{address}</code>\n\n"
        f"This will take effect in <b>{WALLET_CHANGE_DELAY_HOURS} hours</b>.\n\n"
        "If this wasn't you, tap below to cancel immediately.",
        parse_mode="HTML",
        reply_markup=wallet_change_alert_kb(change_id),
    )
    return ConversationHandler.END


# ── Cancel wallet change (Item 4) ─────────────────────────────────────────────

async def cb_cancel_wallet_change(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin taps [🛑 Cancel This Change] in the alert DM.

    No passkey or extra verification — cancellation is always safe.
    The worst case is the legitimate change needs to be resubmitted.
    """
    query = update.callback_query
    await query.answer()

    # callback_data: wallet_change:cancel:<change_id>
    parts = query.data.split(":")
    if len(parts) != 3:
        await query.edit_message_text("❌ Invalid cancel request.")
        return

    change_id = int(parts[2])
    admin_id = update.effective_user.id

    # Fetch the change to verify ownership before cancelling
    change = await queries.get_pending_wallet_change(change_id)
    if not change:
        await query.edit_message_text("⚠️ This wallet change no longer exists.")
        return

    # Ownership check: the requesting admin must own the group
    if not await _verify_admin_access(admin_id, change["group_id"]):
        await query.edit_message_text("❌ Unauthorized.")
        return

    actually_cancelled = await queries.cancel_pending_wallet_change(change_id, admin_id)

    if not actually_cancelled:
        # Already applied or already cancelled
        status_map = {"applied": "already been applied", "cancelled": "already been cancelled"}
        reason = status_map.get(change["status"], "no longer pending")
        await query.edit_message_text(
            f"⚠️ This wallet change has {reason} and cannot be cancelled.",
        )
        return

    await queries.audit(
        change["group_id"],
        "wallet_change_cancelled",
        admin_id,
        {"change_id": change_id, "cancelled_wallet": change["new_wallet_address"]},
    )

    short_wallet = (
        f"{change['new_wallet_address'][:6]}...{change['new_wallet_address'][-4:]}"
        if len(change["new_wallet_address"]) > 12
        else change["new_wallet_address"]
    )

    await query.edit_message_text(
        f"✅ <b>Wallet change cancelled.</b>\n\n"
        f"The scheduled change to <code>{short_wallet}</code> has been cancelled.\n"
        f"Your current wallet remains unchanged.\n\n"
        f"If you'd like to update your wallet, submit a new address from /menu.",
        parse_mode="HTML",
    )


def build_update_wallet_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_update_wallet, pattern=r"^(menu:update_wallet|grpsel:\d+:menu:update_wallet)$")],
        states={
            AWAIT_NEW_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_new_wallet)],
            AWAIT_WALLET_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_wallet_pin)],
        },
        fallbacks=[
            CommandHandler("cancel", lambda u, c: ConversationHandler.END),
            CallbackQueryHandler(cb_cancel_input, pattern=r"^input:cancel$"),
        ],
        per_chat=True,
        per_user=True,
    )


# ── Set / Change Group Wallet Passkey ─────────────────────────────────────────

async def cb_set_group_passkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the passkey setup flow from /menu → Settings → Set Passkey."""
    query = update.callback_query
    await query.answer()
    group = await _require_group(update, ctx, action="menu:set_passkey")
    if not group:
        return ConversationHandler.END
    ctx.user_data["passkey_group"] = group  # type: ignore[index]
    has_existing = bool(group.get("wallet_passcode_hash"))
    ctx.user_data["passkey_has_existing"] = has_existing  # type: ignore[index]

    if has_existing:
        await query.edit_message_text(
            "🔑 <b>Change Wallet Passkey</b>\n\nSend your <b>current</b> 4-digit passkey:",
            parse_mode="HTML",
            reply_markup=cancel_input_kb(),
        )
        return AWAIT_GROUP_PASSKEY_CURRENT
    else:
        await query.edit_message_text(
            "🔑 <b>Set Wallet Passkey</b>\n\n"
            "Create a 4-digit PIN that will be required to change your payout wallet.\n\n"
            "Send your new 4-digit passkey:",
            parse_mode="HTML",
            reply_markup=cancel_input_kb(),
        )
        return AWAIT_GROUP_PASSKEY_NEW


async def msg_passkey_current(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the current passkey when changing an existing one."""
    pin = update.message.text.strip()
    group = ctx.user_data.get("passkey_group")  # type: ignore[union-attr]
    if not group:
        return ConversationHandler.END

    ok = await queries.check_group_wallet_passcode(group["id"], pin)
    if not ok:
        await update.message.reply_text(
            "❌ Incorrect passkey. Please try again:",
            parse_mode="HTML",
            reply_markup=cancel_input_kb(),
        )
        return AWAIT_GROUP_PASSKEY_CURRENT

    ctx.user_data["passkey_current"] = pin  # type: ignore[index]
    await update.message.reply_text(
        "✅ Current passkey verified.\n\nNow send your <b>new</b> 4-digit passkey:",
        parse_mode="HTML",
        reply_markup=cancel_input_kb(),
    )
    return AWAIT_GROUP_PASSKEY_NEW


async def msg_passkey_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and save the new 4-digit passkey."""
    pin = update.message.text.strip()
    group   = ctx.user_data.pop("passkey_group", None)  # type: ignore[union-attr]
    current = ctx.user_data.pop("passkey_current", None)  # type: ignore[union-attr]
    ctx.user_data.pop("passkey_has_existing", None)  # type: ignore[union-attr]

    if not group:
        return ConversationHandler.END

    try:
        success = await queries.set_group_passcode(
            group["id"], pin, update.effective_user.id, current
        )
    except ValueError as e:
        await update.message.reply_text(
            f"❌ {e}  Please send exactly 4 digits:",
            parse_mode="HTML",
            reply_markup=cancel_input_kb(),
        )
        # Restore so the conversation can retry
        ctx.user_data["passkey_group"] = group  # type: ignore[index]
        if current:
            ctx.user_data["passkey_current"] = current  # type: ignore[index]
        return AWAIT_GROUP_PASSKEY_NEW

    if not success:
        await update.message.reply_text(
            "❌ Incorrect current passkey. Passkey not changed.",
            parse_mode="HTML",
            reply_markup=network_detail_kb(group["id"]),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🔒 <b>Passkey saved.</b>\n\n"
        "Your payout wallet is now protected. You'll need this passkey "
        "to authorize any future wallet changes.",
        parse_mode="HTML",
        reply_markup=network_detail_kb(group["id"]),
    )
    return ConversationHandler.END


def build_set_group_passkey_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_set_group_passkey, pattern=r"^(menu:set_passkey|grpsel:\d+:menu:set_passkey)$")],
        states={
            AWAIT_GROUP_PASSKEY_CURRENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_passkey_current)],
            AWAIT_GROUP_PASSKEY_NEW:     [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_passkey_new)],
        },
        fallbacks=[
            CommandHandler("cancel", lambda u, c: ConversationHandler.END),
            CallbackQueryHandler(cb_cancel_input, pattern=r"^input:cancel$"),
        ],
        per_chat=True,
        per_user=True,
    )


# ── Pause / Resume ────────────────────────────────────────────────────────────

async def cb_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group = await _require_group(update, ctx, action="menu:pause")
    if not group:
        return

    if group["status"] == "active":
        await queries.set_group_status(group["id"], "paused")
        await queries.audit(group["id"], "group_paused", update.effective_user.id)
        msg = "⏸ Network paused. New join requests will not be approved until you resume."
    else:
        await queries.set_group_status(group["id"], "active")
        await queries.audit(group["id"], "group_resumed", update.effective_user.id)
        msg = "▶️ Network resumed. New join requests will be processed normally."

    await query.edit_message_text(msg, parse_mode="HTML", reply_markup=network_detail_kb(group["id"]))


# ── Comp a Member ─────────────────────────────────────────────────────────────

async def cb_comp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    group = await _require_group(update, ctx, action="menu:comp")
    if not group:
        return ConversationHandler.END

    ctx.user_data["comp_group"] = group  # type: ignore[index]
    
    # We provide a button to jump to the members list where they can comp existing users,
    # or they can type a username/forward a message for new users.
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Select from existing users", callback_data=f"grpsel:{group['id']}:menu:members")],
        [InlineKeyboardButton("❌ Cancel", callback_data="input:cancel")]
    ])
    
    await query.edit_message_text(
        "🎁 <b>Comp a Member</b>\n\n"
        "Send their @username, forward one of their messages, or select from existing users below:",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return AWAIT_COMP_USERNAME


async def msg_comp_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    group = ctx.user_data.get("comp_group")  # type: ignore[union-attr]

    # Handle forwarded message
    if update.message.forward_from:
        target_user = update.message.forward_from
        target_id = target_user.id
        display = f"@{target_user.username}" if target_user.username else str(target_id)
    else:
        text = update.message.text.strip().lstrip("@")
        # We only have the username resolve via get_chat
        try:
            chat = await ctx.bot.get_chat(f"@{text}")
            target_id = chat.id
            display = f"@{text}"
        except Exception:
            await update.message.reply_text(
                "❌ Couldn't find that user. Make sure they've started the bot or forward their message.",
                reply_markup=cancel_input_kb(),
            )
            return AWAIT_COMP_USERNAME

    first_name = target_user.first_name if update.message.forward_from else chat.first_name
    username = target_user.username if update.message.forward_from else chat.username
    
    user_db_id = await queries.upsert_user(
        telegram_user_id=target_id,
        first_name=first_name,
        username=username
    )
    await queries.comp_subscription(user_db_id, group["id"])
    await queries.audit(
        group["id"], "member_comped", update.effective_user.id, {"comped_user": target_id}
    )

    # Try to approve their pending join request if any
    try:
        await ctx.bot.approve_chat_join_request(chat_id=group["telegram_chat_id"], user_id=target_id)
    except Exception as e:
        log.debug("approve_chat_join_request skipped for %s: %s", target_id, e)

    ctx.user_data.pop("comp_group", None)  # type: ignore[union-attr]
    await update.message.reply_text(
        f"\u2705 {html.escape(display)} has been comped access granted, logged as <code>comped</code>.",
        parse_mode="HTML",
        reply_markup=network_detail_kb(group["id"]),
    )
    return ConversationHandler.END


def build_comp_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_comp, pattern=r"^(menu:comp|grpsel:\d+:menu:comp)$")],
        states={
            AWAIT_COMP_USERNAME: [
                MessageHandler(
                    (filters.TEXT & ~filters.COMMAND) | filters.FORWARDED,
                    msg_comp_username,
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", lambda u, c: ConversationHandler.END),
            CallbackQueryHandler(cb_cancel_input, pattern=r"^input:cancel$"),
        ],
        per_chat=True,
        per_user=True,
    )


# ── Support ───────────────────────────────────────────────────────────────────

async def cb_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "📞 <b>Contact Support</b>\n\n"
        "Please type your message to the Renewise Super-Admins below:",
        parse_mode="HTML",
        reply_markup=cancel_input_kb(),
    )
    return AWAIT_SUPPORT_MSG

async def msg_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    user = update.effective_user
    username_str = html.escape(f"@{user.username}" if user.username else "No Username")
    safe_full_name = html.escape(user.full_name or str(user.id))
    safe_text = html.escape(text)

    # Determine the sender's role on the platform so superadmin has context.
    from renewise.db.queries import get_user_by_telegram_id
    from renewise.db.connection import _db as _conn
    role_parts = []
    try:
        user_row = await get_user_by_telegram_id(user.id)
        if user_row:
            uid = user_row["id"]
            async with _conn() as db:
                # Check active member subscriptions
                active_subs = int(await db.fetchval(
                    "SELECT COUNT(*) FROM subscriptions WHERE user_id=$1 AND status='active'",
                    uid,
                ) or 0)
                if active_subs:
                    role_parts.append(f"👤 Member ({active_subs} active sub{'s' if active_subs > 1 else ''})")

                # Check if they own any groups (platform admin)
                from renewise.config import USE_POSTGRES
                if USE_POSTGRES:
                    row = await db.fetchrow(
                        "SELECT COUNT(*) AS cnt, STRING_AGG(chat_title, ', ') AS titles "
                        "FROM groups WHERE admin_telegram_id=$1 AND status='active'",
                        user.id,
                    )
                else:
                    row = await db.fetchrow(
                        "SELECT COUNT(*) AS cnt, GROUP_CONCAT(chat_title, ', ') AS titles "
                        "FROM groups WHERE admin_telegram_id=$1 AND status='active'",
                        user.id,
                    )
                if row and row["cnt"]:
                    titles = (row["titles"] or "")[:80]
                    role_parts.append(f"👑 Admin ({row['cnt']} group{'s' if row['cnt'] > 1 else ''}: {html.escape(titles)})")

                # Check if they are a Developer (own platforms)
                if USE_POSTGRES:
                    dev_row = await db.fetchrow(
                        "SELECT COUNT(*) AS cnt, STRING_AGG(platform_name, ', ') AS names "
                        "FROM platforms WHERE owner_telegram_id=$1",
                        user.id,
                    )
                else:
                    dev_row = await db.fetchrow(
                        "SELECT COUNT(*) AS cnt, GROUP_CONCAT(platform_name, ', ') AS names "
                        "FROM platforms WHERE owner_telegram_id=$1",
                        user.id,
                    )
                if dev_row and dev_row["cnt"]:
                    names = (dev_row["names"] or "")[:80]
                    role_parts.append(f"👨‍💻 Developer ({dev_row['cnt']} app{'s' if dev_row['cnt'] > 1 else ''}: {html.escape(names)})")
    except Exception as _e:
        log.warning("msg_support: could not resolve role for user %d: %s", user.id, _e)

    role_line = " | ".join(role_parts) if role_parts else "❓ Unknown / New user"

    from renewise.config import SUPERADMIN_BOT_TOKEN, ALLOWED_SUPERADMIN_IDS
    from telegram import Bot

    # Notify super admins
    if SUPERADMIN_BOT_TOKEN and ALLOWED_SUPERADMIN_IDS:
        sa_bot = Bot(SUPERADMIN_BOT_TOKEN)
        for sa_id in ALLOWED_SUPERADMIN_IDS:
            try:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("💬 Reply", callback_data=f"sa_reply_{user.id}")
                ]])
                await sa_bot.send_message(
                    chat_id=sa_id,
                    text=(
                        f"🎫 <b>New Support Ticket</b>\n\n"
                        f"<b>From:</b> {safe_full_name} ({username_str})\n"
                        f"<b>User ID:</b> <code>{user.id}</code>\n"
                        f"<b>Role:</b> {role_line}\n\n"
                        f"<b>Message:</b>\n{safe_text}"
                    ),
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            except Exception as e:
                log.warning("Failed to send support message to super admin %s: %s", sa_id, e)
    
    # Send confirmation back to user — return to picker since we don't have a group in context
    await update.message.reply_text(
        "✅ Your message has been sent to our support team. We'll get back to you shortly!",
    )
    await _show_network_picker(update, ctx)
    return ConversationHandler.END

def build_support_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_support, pattern=r"^menu:support$")],
        states={
            AWAIT_SUPPORT_MSG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_support)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", lambda u, c: ConversationHandler.END),
            CallbackQueryHandler(cb_cancel_input, pattern=r"^input:cancel$"),
        ],
        per_chat=True,
        per_user=True,
    )


# ── View Members (paginated) ──────────────────────────────────────────────────

async def _send_members_page(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, group_id: int, chat_id_tg: int, page: int
) -> None:
    offset = page * MEMBERS_PAGE_SIZE
    members = await queries.get_members_page(group_id, offset, MEMBERS_PAGE_SIZE)
    total = await queries.count_members(group_id)
    has_next = (offset + MEMBERS_PAGE_SIZE) < total

    if not members:
        text = "👥 No active or pending members found."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data=f"grpsel:{group_id}:menu:home")]])
    else:
        lines = [f"👥 <b>Members</b> (page {page + 1}):\n"]
        buttons = []
        for m in members:
            uid = m["telegram_user_id"]
            first_name = m["first_name"] or "Unknown"
            username = f" (@{m['username']})" if m["username"] else ""
            status = m["status"]
            lines.append(f"• <b>{first_name}</b>{username} [{status}]")
            buttons.append([
                InlineKeyboardButton(
                    f"👤 {first_name}",
                    callback_data=f"member:view:{uid}:{group_id}",
                )
            ])
        text = "\n".join(lines)
        kb = members_nav_kb(group_id, page, has_next)
        # Prepend member buttons above nav
        kb.inline_keyboard = buttons + kb.inline_keyboard

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

async def _verify_admin_access(user_id: int, group_id: int) -> bool:
    """Check if the user is the admin of the specified group."""
    group = await queries.get_group_by_id(group_id)
    return bool(group and group["admin_telegram_id"] == user_id)

async def cb_view_members(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group = await _require_group(update, ctx, action="menu:members")
    if not group:
        return
    await _send_members_page(update, ctx, group["id"], group["telegram_chat_id"], 0)


async def cb_members_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, group_id_str, page_str = query.data.split(":")
    gid = int(group_id_str)
    if not await _verify_admin_access(update.effective_user.id, gid):
        await query.edit_message_text("❌ Unauthorized.")
        return
    await _send_members_page(update, ctx, gid, 0, int(page_str))


async def cb_member_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, uid_str, gid_str = query.data.split(":")
    uid, gid = int(uid_str), int(gid_str)
    if not await _verify_admin_access(update.effective_user.id, gid):
        await query.edit_message_text("❌ Unauthorized.")
        return
    user_db = await queries.get_user_by_telegram_id(uid)
    if user_db:
        first_name = user_db.get("first_name") or "Unknown"
        username_str = f" (@{user_db['username']})" if user_db.get("username") else ""
        display_name = f"{first_name}{username_str} (<code>{uid}</code>)"
    else:
        display_name = f"<code>{uid}</code>"

    await query.edit_message_text(
        f"👤 User {display_name}",
        parse_mode="HTML",
        reply_markup=member_action_kb(uid, gid),
    )


async def cb_member_comp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, uid_str, gid_str = query.data.split(":")
    uid, gid = int(uid_str), int(gid_str)
    if not await _verify_admin_access(update.effective_user.id, gid):
        await query.edit_message_text("❌ Unauthorized.")
        return
    group = await queries.get_group_by_id(gid)
    user_db_id = await queries.upsert_user(telegram_user_id=uid)
    await queries.comp_subscription(user_db_id, gid)
    await queries.audit(gid, "member_comped", update.effective_user.id, {"comped_user": uid})
    try:
        await ctx.bot.approve_chat_join_request(chat_id=group["telegram_chat_id"], user_id=uid)
    except Exception as e:
        log.debug("approve_chat_join_request skipped for %s: %s", uid, e)
    await query.edit_message_text(
        f"✅ User <code>{uid}</code> comped.", parse_mode="HTML", reply_markup=network_detail_kb(gid)
    )


async def cb_member_kick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, uid_str, gid_str = query.data.split(":")
    uid, gid = int(uid_str), int(gid_str)
    if not await _verify_admin_access(update.effective_user.id, gid):
        await query.edit_message_text("❌ Unauthorized.")
        return
    group = await queries.get_group_by_id(gid)
    user_db = await queries.get_user_by_telegram_id(uid)
    if user_db:
        await queries.cancel_subscription(user_db["id"], gid)
    try:
        await ctx.bot.ban_chat_member(chat_id=group["telegram_chat_id"], user_id=uid)
        await ctx.bot.unban_chat_member(chat_id=group["telegram_chat_id"], user_id=uid)
    except Exception as e:
        log.warning("Kick failed for %s: %s", uid, e)
    await queries.audit(gid, "member_kicked", update.effective_user.id, {"kicked_user": uid})
    await query.edit_message_text(
        f"🚫 User <code>{uid}</code> removed.", parse_mode="HTML", reply_markup=network_detail_kb(gid)
    )


# ── Settings ──────────────────────────────────────────────────────────────────

async def cb_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group = await _require_group(update, ctx, action="menu:settings")
    if not group:
        return

    try:
        chat = await ctx.bot.get_chat(group["telegram_chat_id"])
        title = html.escape(chat.title or str(group["telegram_chat_id"]))
    except Exception:
        title = str(group["telegram_chat_id"])

    usd = (group.get("price_usd_cents") or 0) / 100.0
    interval = group.get("billing_interval_days", 30)
    wallet = group.get("payout_wallet_address") or "Not set"
    wallet_disp = f"{wallet[:8]}…{wallet[-5:]}" if len(wallet) > 16 else wallet
    status = group.get("status", "active")
    status_icon = "✅ Active" if status == "active" else ("⏸ Paused" if status == "paused" else f"❄️ {status.title()}")
    has_passkey = bool(group.get("wallet_passcode_hash"))
    passkey_line = "🔒 Set" if has_passkey else "Not set"

    text = (
        f"⚙️ <b>Settings {title}</b>\n\n"
        f"💰 Price:        <b>${usd:.2f} USD</b> / {interval} days\n"
        f"👛 Wallet:       <code>{wallet_disp}</code>\n"
        f"🔑 Passkey:      {passkey_line}\n"
        f"📶 Status:       {status_icon}\n"
        f"📅 Billing:      every {interval} days\n\n"
        "Tap an action below to change a setting:"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Update Price",  callback_data=f"grpsel:{group['id']}:menu:update_price"),
            InlineKeyboardButton("👛 Update Wallet", callback_data=f"grpsel:{group['id']}:menu:update_wallet"),
        ],
        [
            InlineKeyboardButton(
                "🔑 Change Passkey" if has_passkey else "🔑 Set Passkey",
                callback_data=f"grpsel:{group['id']}:menu:set_passkey",
            ),
        ],
        [
            InlineKeyboardButton(
                "⏸ Pause" if status == "active" else "▶️ Resume",
                callback_data=f"grpsel:{group['id']}:menu:pause",
            ),
        ],
        [InlineKeyboardButton("◀️ Back", callback_data=f"grpsel:{group['id']}:menu:home")],
    ])

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


# ── Payment History (paginated) ───────────────────────────────────────────────────────────

PMT_PAGE_SIZE = 5


async def _send_payment_history_page(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, group_id: int, page: int
) -> None:
    offset = page * PMT_PAGE_SIZE
    rows, total = await queries.get_payment_history(group_id, offset, PMT_PAGE_SIZE)
    has_next = (offset + PMT_PAGE_SIZE) < total

    if not rows:
        text = "💳 No payment records found for this group yet."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data=f"grpsel:{group_id}:menu:home")]])
    else:
        status_icon = {
            "active": "✅",
            "comped": "🎁",
            "expired": "⏰",
            "cancelled": "❌",
            "pending": "⏳",
        }
        lines = [f"💳 <b>Payment History</b> (page {page + 1} / {max(1, -(-total // PMT_PAGE_SIZE))})\n"]
        buttons = []
        for r in rows:
            first_name = html.escape(r["first_name"] or "Unknown")
            uname = f" (@{html.escape(r['username'])})" if r["username"] else ""
            icon = status_icon.get(r["status"], "•")
            # price_locked_in is stored as USD dollars (e.g. 10.0 = $10.00).
            # Values > 10_000 are legacy nano-GRAM entries.
            raw = r["price_locked_in"]
            price_str = (
                f"{raw / 1e9:.4f} GRAM" if raw and raw > 10_000
                else (f"${raw:.2f}" if raw else "N/A")
            )
            tx_snip = (
                f"<code>{r['last_payment_tx_hash'][:10]}…</code>"
                if r["last_payment_tx_hash"] else "—"
            )
            lines.append(
                f"{icon} <b>{first_name}</b>{uname}\n"
                f"   💰 {price_str} | 🗓 {r['start_date'] or 'N/A'}\n"
                f"   🔄 Renews: {r['next_renewal_date'] or 'N/A'}\n"
                f"   TX: {tx_snip}\n"
            )
            buttons.append([
                InlineKeyboardButton(
                    f"💳 {first_name} - {price_str}",
                    callback_data=f"pmthist:detail:{r['id']}:{group_id}:{page}"
                )
            ])
        text = "\n".join(lines)
        kb = payment_history_nav_kb(group_id, page, has_next)
        # Prepend payment buttons above nav
        kb.inline_keyboard = buttons + kb.inline_keyboard

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def cb_payment_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group = await _require_group(update, ctx, action="menu:payment_history")
    if not group:
        return
    await _send_payment_history_page(update, ctx, group["id"], 0)


async def cb_payment_history_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # callback_data: pmthist:page:<group_id>:<page>
    _, _, group_id_str, page_str = query.data.split(":")
    gid = int(group_id_str)
    if not await _verify_admin_access(update.effective_user.id, gid):
        await query.edit_message_text("❌ Unauthorized.")
        return
    await _send_payment_history_page(update, ctx, gid, int(page_str))


async def cb_payment_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # callback_data: pmthist:detail:<sub_id>:<group_id>:<page>
    parts = query.data.split(":")
    if len(parts) == 5:
        _, _, sub_id_str, group_id_str, page_str = parts
        sub_id, gid, page = int(sub_id_str), int(group_id_str), int(page_str)
    else:
        await query.edit_message_text("❌ Invalid data.")
        return

    if not await _verify_admin_access(update.effective_user.id, gid):
        await query.edit_message_text("❌ Unauthorized.")
        return

    row = await queries.get_payment_detail(sub_id)
    if not row:
        await query.edit_message_text("⚠️ Payment not found.")
        return

    first_name = html.escape(row["first_name"] or "Unknown")
    uname = f" (@{html.escape(row['username'])})" if row["username"] else ""
    uid = row["telegram_user_id"]

    status_icon = {
        "active": "✅",
        "comped": "🎁",
        "expired": "⏰",
        "cancelled": "❌",
        "pending": "⏳",
    }
    icon = status_icon.get(row["status"], "•")
    
    raw = row["price_locked_in"]
    price_str = (
        f"{raw / 1e9:.4f} GRAM" if raw and raw > 10_000
        else (f"${raw:.2f}" if raw else "N/A")
    )
    
    text = (
        f"💳 <b>Payment Details</b>\n\n"
        f"👤 <b>User:</b> {first_name}{uname} (<code>{uid}</code>)\n"
        f"📋 <b>Status:</b> {icon} {row['status'].title()}\n"
        f"💰 <b>Amount:</b> {price_str}\n"
        f"🗓 <b>Started:</b> {row['start_date'] or 'N/A'}\n"
        f"🔄 <b>Renews:</b> {row['next_renewal_date'] or 'N/A'}\n"
        f"🔗 <b>TX Hash:</b> <code>{row['last_payment_tx_hash'] or '—'}</code>\n"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Back", callback_data=f"pmthist:page:{gid}:{page}")]
    ])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


# ── Wallet Change History ─────────────────────────────────────────────────────

async def cb_wallet_change_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group = await _require_group(update, ctx, action="menu:wallet_history")
    if not group:
        return

    rows = await queries.get_wallet_change_history(group["id"])
    status_icon = {"pending": "⏳", "applied": "✅", "cancelled": "❌"}

    if not rows:
        text = "📜 <b>Wallet Change History</b>\n\nNo wallet changes recorded yet."
    else:
        lines = ["📜 <b>Wallet Change History</b>\n"]
        for r in rows:
            icon = status_icon.get(r["status"], "•")
            old_w = r["old_wallet_address"] or "(none)"
            new_w = r["new_wallet_address"]
            old_short = f"{old_w[:6]}...{old_w[-4:]}" if len(old_w) > 12 else old_w
            new_short = f"{new_w[:6]}...{new_w[-4:]}" if len(new_w) > 12 else new_w
            date = (r["requested_at"] or "")[:16]
            lines.append(
                f"{icon} <code>{old_short}</code> → <code>{new_short}</code>\n"
                f"   {r['status'].title()} • {date}"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Back", callback_data=f"grpsel:{group['id']}:menu:home")
    ]])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


# ── Delete Network ───────────────────────────────────────────────────────────

async def cb_delete_network(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 1: show the warning screen explaining exactly what will happen."""
    query = update.callback_query
    await query.answer()
    group = await _require_group(update, ctx, action="menu:delete")
    if not group:
        return ConversationHandler.END

    try:
        chat = await ctx.bot.get_chat(group["telegram_chat_id"])
        title = html.escape(chat.title or str(group["telegram_chat_id"]))
    except Exception:
        title = str(group["telegram_chat_id"])

    ctx.user_data["delete_group"] = group  # type: ignore[index]
    ctx.user_data["delete_group_title"] = title  # type: ignore[index]

    await query.edit_message_text(
        f"🗑️ <b>Delete Network {title}</b>\n\n"
        "This will permanently do the following, <b>in order</b>:\n\n"
        "1️⃣ All new and pending payments for this network are <b>immediately paused</b>\n"
        "2️⃣ The bot <b>leaves the group/channel</b> automatically\n"
        "3️⃣ All subscriber and group records are <b>permanently deleted</b>\n"
        "4️⃣ <b>This cannot be undone.</b> Active subscribers lose access immediately.\n\n"
        "Tap <b>Continue</b> to proceed to the confirmation step, or Cancel to go back.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ Continue to Confirmation", callback_data="delete:step2")],
            [InlineKeyboardButton("❌ Cancel", callback_data="input:cancel")],
        ]),
    )
    return AWAIT_DELETE_CONFIRM_NAME


async def cb_delete_step2(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 2: ask admin to type the exact group name to confirm."""
    query = update.callback_query
    await query.answer()
    title = ctx.user_data.get("delete_group_title", "")  # type: ignore[union-attr]
    await query.edit_message_text(
        f"⚠️ <b>Final Confirmation</b>\n\n"
        f"Type the exact name of the network to confirm deletion:\n"
        f"<code>{html.escape(title)}</code>\n\n"
        "<i>This action is irreversible.</i>",
        parse_mode="HTML",
        reply_markup=cancel_input_kb(),
    )
    return AWAIT_DELETE_CONFIRM_NAME


async def msg_delete_confirm_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the typed name; execute deletion if it matches."""
    typed = update.message.text.strip()
    group = ctx.user_data.get("delete_group")  # type: ignore[union-attr]
    title = ctx.user_data.get("delete_group_title", "")  # type: ignore[union-attr]

    if typed != title:
        await update.message.reply_text(
            f"❌ Name doesn't match. Type exactly: <code>{html.escape(title)}</code>",
            parse_mode="HTML",
            reply_markup=cancel_input_kb(),
        )
        return AWAIT_DELETE_CONFIRM_NAME

    admin_id = update.effective_user.id

    # ── i. Pause immediately ──────────────────────────────────────────────────
    await queries.set_group_status(group["id"], "paused")

    # ── iv. Notify active/pending subscribers BEFORE leaving ─────────────────
    subscribers = await queries.get_active_subscribers(group["id"])
    for row in subscribers:
        try:
            await ctx.bot.send_message(
                chat_id=row["telegram_user_id"],
                text=f"ℹ️ Access to <b>{html.escape(title)}</b> has ended because "
                     "the admin closed this paywall.",
                parse_mode="HTML",
            )
        except Exception as e:
            log.debug("subscriber DM skipped for %s: %s", row["telegram_user_id"], e)

    # ── ii. Leave the chat ────────────────────────────────────────────────────
    try:
        await ctx.bot.leave_chat(group["telegram_chat_id"])
    except Exception as e:
        log.warning("leave_chat failed for %s: %s", group["telegram_chat_id"], e)

    # ── iii–v. Audit + cascade delete (single transaction) ───────────────────
    await queries.delete_group(
        group_id=group["id"],
        actor_id=admin_id,
        group_title=title,
        telegram_chat_id=group["telegram_chat_id"],
    )

    ctx.user_data.pop("delete_group", None)  # type: ignore[union-attr]
    ctx.user_data.pop("delete_group_title", None)  # type: ignore[union-attr]

    await update.message.reply_text(
        f"✅ <b>{html.escape(title)}</b> has been deleted. "
        "All subscriber records have been removed and the bot has left the group.",
        parse_mode="HTML",
    )
    await _show_network_picker(update, ctx)
    return ConversationHandler.END


def build_delete_network_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_delete_network, pattern=r"^(menu:delete|grpsel:\d+:menu:delete)$")
        ],
        states={
            AWAIT_DELETE_CONFIRM_NAME: [
                CallbackQueryHandler(cb_delete_step2, pattern=r"^delete:step2$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_delete_confirm_name),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", lambda u, c: ConversationHandler.END),
            CallbackQueryHandler(cb_cancel_input, pattern=r"^input:cancel$"),
        ],
        per_chat=True,
        per_user=True,
    )


# ── standalone callback dispatcher (non-conversation callbacks) ───────────────

def register_menu_callbacks(app) -> None:
    """Register all non-conversation menu callbacks on the Application."""
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CallbackQueryHandler(cb_menu_back,         pattern=r"^menu:back$"))
    # Network picker selection → detail screen
    app.add_handler(CallbackQueryHandler(cb_network_selected,  pattern=r"^grpsel:\d+:menu:home$"))
    app.add_handler(CallbackQueryHandler(cb_stats,             pattern=r"^(menu:stats|grpsel:\d+:menu:stats)$"))
    app.add_handler(CallbackQueryHandler(cb_pause,             pattern=r"^(menu:pause|grpsel:\d+:menu:pause)$"))
    app.add_handler(CallbackQueryHandler(cb_view_members,      pattern=r"^(menu:members|grpsel:\d+:menu:members)$"))
    app.add_handler(CallbackQueryHandler(cb_members_page,      pattern=r"^members:page:"))
    app.add_handler(CallbackQueryHandler(cb_member_view,       pattern=r"^member:view:"))
    app.add_handler(CallbackQueryHandler(cb_member_comp,       pattern=r"^member:comp:"))
    app.add_handler(CallbackQueryHandler(cb_member_kick,       pattern=r"^member:kick:"))
    app.add_handler(CallbackQueryHandler(cb_settings,          pattern=r"^menu:settings$"))
    app.add_handler(CallbackQueryHandler(cb_payment_history,   pattern=r"^(menu:payment_history|grpsel:\d+:menu:payment_history)$"))
    app.add_handler(CallbackQueryHandler(cb_payment_history_page, pattern=r"^pmthist:page:"))
    app.add_handler(CallbackQueryHandler(cb_payment_detail,    pattern=r"^pmthist:detail:"))
    app.add_handler(CallbackQueryHandler(cb_cancel_wallet_change, pattern=r"^wallet_change:cancel:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_wallet_change_history, pattern=r"^(menu:wallet_history|grpsel:\d+:menu:wallet_history)$"))
    app.add_handler(build_update_price_handler())
    app.add_handler(build_update_wallet_handler())
    app.add_handler(build_comp_handler())
    app.add_handler(build_support_handler())
    app.add_handler(build_delete_network_handler())
    app.add_handler(build_set_group_passkey_handler())

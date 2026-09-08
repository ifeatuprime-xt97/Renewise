"""
/createpaywall wizard DM-only, button-driven multi-step flow.

Steps (new design):
  STEP_INSTRUCTIONS  → user reads how to add bot as admin
  STEP_DETECT        → bot detects recent admin-grant events (one/many/none)
  STEP_CONFIRM_CHAT  → user confirms which chat (after multiple-match list)
  ... Steps 3-7 (fee transparency, price, wallet, confirm, live)
      to be implemented in the next chunk.

States are integers for compatibility with PTB ConversationHandler.
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
    confirm_paywall_kb, post_activate_kb,
)

log = logging.getLogger(__name__)

# ── conversation states ───────────────────────────────────────────────────────
(
    STEP_INSTRUCTIONS,   # 0 – show "add me as admin" screen
    STEP_DETECT,         # 1 – "Done Check Now" pressed; detect chats
    STEP_CONFIRM_CHAT,   # 2 – user picks from multiple matches
    STEP_FEE_INFO,       # 3 – fee transparency (stub for next chunk)
    STEP_INTERVAL,       # 4 – weekly / monthly
    STEP_PRICE,          # 5 – enter USD price
    STEP_PRICE_CONFIRM,  # 6 – confirm price + live GRAM equivalent
    STEP_WALLET,         # 7 – enter GRAM wallet
    STEP_FINAL_CONFIRM,  # 8 – final summary confirm
) = range(9)

_W = "wizard"   # user_data key for wizard state dict


def _w(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    ctx.user_data.setdefault(_W, {})  # type: ignore[union-attr]
    return ctx.user_data[_W]          # type: ignore[index]


# ── helpers ───────────────────────────────────────────────────────────────────

def _chat_kind(chat_type: str) -> str:
    """Human-readable label for a chat type."""
    return "Channel" if chat_type == "channel" else "Group"


def _check_permissions(grant: object, chat_type: str) -> list[str]:
    """
    Return list of missing permission labels for this chat type.
    Bot API field          Telegram UI label
    can_invite_users    =  Add Users + Process Join Requests
    can_manage_chat     =  Ban Users / Manage Chat
    can_post_messages   =  Manage Messages (channels only)
    """
    if chat_type == "channel":
        required = {
            "can_invite_users":  "Add Users / Process Join Requests",
            "can_manage_chat":   "Ban Users",
            "can_post_messages": "Manage Messages",
        }
    else:
        required = {
            "can_invite_users": "Add Users / Process Join Requests",
            "can_manage_chat":  "Ban Users",
        }
    missing = []
    for field, label in required.items():
        if not getattr(grant, field, False):
            missing.append(label)
    return missing


def _grant_has_perms(grant, chat_type: str) -> list[str]:
    """
    Accept an aiosqlite.Row from recent_admin_grants.
    Returns list of missing human-readable permission labels.
    Uses the actual DB column names written by chat_member.py:
      can_invite_users, can_manage_chat, can_post_messages
    """
    if chat_type == "channel":
        checks = [
            ("can_invite_users",  "Add Users / Process Join Requests"),
            ("can_manage_chat",   "Ban Users"),
            ("can_post_messages", "Manage Messages"),
        ]
    else:
        checks = [
            ("can_invite_users", "Add Users / Process Join Requests"),
            ("can_manage_chat",  "Ban Users"),
        ]
    return [label for col, label in checks if not grant[col]]


def _instructions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Done Check Now", callback_data="pw:check_now")],
        [InlineKeyboardButton("🔙 Back",             callback_data="pw:back_to_start")],
    ])


def _no_match_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Try Again",       callback_data="pw:check_now")],
        [InlineKeyboardButton("💬 Contact Support", callback_data="pw:support")],
        [InlineKeyboardButton("🔙 Back",            callback_data="pw:back_to_start")],
    ])


async def _close_if_group_already_active(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    telegram_chat_id: int | None,
    chat_title: str | None = None,
) -> bool:
    """End a stale wizard when the paywall was already activated in the Mini App."""
    if telegram_chat_id is None:
        return False

    group = await queries.get_group_by_chat_id(telegram_chat_id)
    if not group or group.get("status") != "active":
        return False

    title = html.escape(chat_title or group.get("chat_title") or "your group")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Main Menu", callback_data="start:my_groups")],
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(
            f"✅ This group is already active. Your paywall for <b>{title}</b> is already live.\n\n"
            "You can manage it from the menu below.",
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        await update.message.reply_text(
            f"✅ This group is already active. Your paywall for <b>{title}</b> is already live.",
            parse_mode="HTML",
            reply_markup=kb,
        )

    ctx.user_data.pop(_W, None)
    return True


# ── entry: "Set Up a Paywall" (from /start or /createpaywall) ─────────────────

async def cmd_create_paywall(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: show Step 1 instructions screen."""
    if update.callback_query:
        await update.callback_query.answer()
        reply = update.callback_query.edit_message_text
    else:
        reply = update.message.reply_text  # type: ignore[assignment]

    if update.effective_chat.type != "private":
        await reply("⚠️ Please use this in a DM with me.")
        return ConversationHandler.END

    user_id = update.effective_user.id
    
    ban_reason = await queries.get_admin_ban_reason(user_id)
    if ban_reason:
        await reply(f"⛔️ You have been banned from using this platform.\nReason: {ban_reason}")
        return ConversationHandler.END

    if await queries.is_admin_suspended(user_id):
        await reply("⛔️ Your admin account has been suspended.")
        return ConversationHandler.END

    # ── ToS gate for paywall creators ────────────────────────────────────────
    # Only admin/paywall-creator users see the Terms of Service.
    # Members paying to join a channel are not gated — they go straight to payment.
    from renewise.db.queries import has_accepted_terms, upsert_user as _upsert
    user = update.effective_user
    await _upsert(
        telegram_user_id=user.id,
        first_name=user.first_name,
        username=user.username,
    )
    if not await has_accepted_terms(user_id):
        # Store where to resume after acceptance
        ctx.user_data["tos_resume"] = "create_paywall"
        from renewise.bot import _show_terms
        await _show_terms(update)
        return ConversationHandler.END

    ctx.user_data[_W] = {}   # clear any stale wizard state

    msg = await reply(
        "🛠️ <b>Step 1: Prepare your group or channel</b>\n\n"
        "Before adding me, you need to set up your group or channel correctly:\n\n"
        "<b>1️⃣ Set it to Private</b>\n"
        "Go to your group/channel settings and change the invite link type to <b>Private</b> "
        "(not public). This ensures non-paying members can't join freely.\n\n"
        "<b>2️⃣ Enable Join Approval</b>\n"
        "In your group settings → Members, turn on <b>Approve New Members</b> "
        "(for groups) or in channel settings enable <b>Join Requests</b> (for channels). "
        "This is how I control who gets in I approve paying members and reject non-payers.\n\n"
        "<b>3️⃣ Add me as Admin</b>\n"
        "Add me as an <b>Admin</b> with these permissions:\n"
        "✅ Add Users <i>(enables both 'Add Users' and 'Process Join Requests')</i>\n"
        "✅ Ban Users\n"
        "✅ Manage Messages <i>(channels only)</i>\n\n"
        "⚠️ <b>Important:</b> Steps 1 and 2 must be done <b>before</b> sharing the invite link "
        "with your members, otherwise anyone can join without paying.\n\n"
        "Once all three steps are done, tap below.",
        parse_mode="HTML",
        reply_markup=_instructions_kb(),
    )
    if hasattr(msg, 'message_id'):
        ctx.user_data["paywall_msg_id"] = msg.message_id
    elif update.effective_message:
        ctx.user_data["paywall_msg_id"] = update.effective_message.message_id
    return STEP_INSTRUCTIONS


# ── Step 1 callback: user says "Done Check Now" ─────────────────────────────

async def cb_check_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Query recent_admin_grants for this user (last 15 min) and branch."""
    query = update.callback_query
    await query.answer("Checking…")

    user_id = update.effective_user.id
    grants  = await queries.get_recent_admin_grants(user_id, minutes=15)

    if not grants:
        await query.edit_message_text(
            "🔍 I couldn't find a group or channel where you've added me as admin recently.\n\n"
            "Please double-check the permissions listed above and try again. "
            "Make sure you're adding me as <b>Admin</b> (not just a member).",
            parse_mode="HTML",
            reply_markup=_no_match_kb(),
        )
        return STEP_INSTRUCTIONS

    for grant in grants:
        if await _close_if_group_already_active(update, ctx, grant["telegram_chat_id"], grant["chat_title"]):
            return ConversationHandler.END

    if len(grants) == 1:
        return await _present_single_grant(query, ctx, grants[0])

    # Multiple matches list them as buttons
    buttons = [
        [InlineKeyboardButton(
            f"{'📢' if g['chat_type'] == 'channel' else '👥'} {g['chat_title']}",
            callback_data=f"pw:pick:{g['id']}",
        )]
        for g in grants
    ]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="pw:back_to_instructions")])

    await query.edit_message_text(
        "I found <b>multiple</b> groups/channels you've recently added me to as admin.\n"
        "Which one do you want to set up a paywall for?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return STEP_CONFIRM_CHAT


async def _present_single_grant(query, ctx, grant) -> int:
    """Edit the existing message in-place to show the confirmation screen (or a permission error)."""
    missing = _grant_has_perms(grant, grant["chat_type"])
    kind    = _chat_kind(grant["chat_type"])
    title   = html.escape(grant["chat_title"])
    grant_id = grant["id"]

    if missing:
        missing_str = "\n".join(f"  \u274c {p}" for p in missing)
        msg = await query.edit_message_text(
            f"\u26a0\ufe0f I found <b>{title}</b> ({kind}), but I'm missing required permissions:\n\n"
            f"{missing_str}\n\n"
            "Please update my permissions in the group/channel settings and try again.",
            parse_mode="HTML",
            reply_markup=_instructions_kb(),
        )
        if hasattr(msg, 'message_id'):
            ctx.user_data["paywall_msg_id"] = msg.message_id
        elif query.message:
            ctx.user_data["paywall_msg_id"] = query.message.message_id
        return STEP_INSTRUCTIONS

    # All good — store in wizard state and ask user to confirm.
    # The Yes button embeds the grant_id so it works as a self-contained entry point
    # even after a bot restart (no ConversationHandler state needed).
    _w(ctx)["grant_id"]   = grant_id
    _w(ctx)["chat_id"]    = grant["telegram_chat_id"]
    _w(ctx)["chat_title"] = title
    _w(ctx)["chat_type"]  = grant["chat_type"]

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 Yes, set up paywall",  callback_data=f"pw:grant_confirm:{grant_id}")],
        [InlineKeyboardButton("\u274c No, redo the setup",   callback_data="pw:back_to_instructions")],
    ])
    icon = "\U0001f4e2" if grant["chat_type"] == "channel" else "\U0001f465"
    # Edit the existing "Done Check Now" message in-place — no new message sent.
    # Success confirmation + the channel question are merged into one clean message.
    await query.edit_message_text(
        f"\u2705 I'm now admin in <b>{title}</b> with all required permissions!\n\n"
        f"Is this the right {kind.lower()} to set up a paywall for?\n\n"
        f"\U0001f4cc <b>{title}</b> ({kind})",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return STEP_DETECT


async def cb_grant_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point: fires when admin taps 'Yes, set up paywall'.
    The grant_id is embedded in the callback data so this works even after
    a bot restart with no prior ConversationHandler state.
    """
    query = update.callback_query
    await query.answer()

    grant_id = int(query.data.split(":")[2])

    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        grant = await db.fetchrow(
            "SELECT * FROM recent_admin_grants WHERE id=$1", grant_id
        )

    if not grant or grant["from_user_id"] != update.effective_user.id:
        await query.edit_message_text(
            "This confirmation has expired. Please use /createpaywall to start again."
        )
        return ConversationHandler.END

    if await _close_if_group_already_active(update, ctx, grant["telegram_chat_id"], grant["chat_title"]):
        return ConversationHandler.END

    missing = _grant_has_perms(grant, grant["chat_type"])
    kind  = _chat_kind(grant["chat_type"])
    title = html.escape(grant["chat_title"])

    if missing:
        missing_str = "\n".join(f"  \u274c {p}" for p in missing)
        await query.edit_message_text(
            f"\u26a0\ufe0f I found <b>{title}</b> ({kind}), but I'm still missing required permissions:\n\n"
            f"{missing_str}\n\n"
            "Please grant them and tap <b>\u2705 Done Check Now</b> in our DM.",
            parse_mode="HTML",
            reply_markup=_instructions_kb(),
        )
        return STEP_INSTRUCTIONS

    # Set fresh wizard state and jump directly to fee transparency screen
    ctx.user_data[_W] = {}
    _w(ctx)["grant_id"]   = grant["id"]
    _w(ctx)["chat_id"]    = grant["telegram_chat_id"]
    _w(ctx)["chat_title"] = title
    _w(ctx)["chat_type"]  = grant["chat_type"]

    return await cb_confirm_chat(update, ctx)


# ── Step 2 callback: user picks from multi-match list ────────────────────────

async def cb_pick_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """User selected a specific chat from the multiple-match list."""
    query = update.callback_query
    await query.answer()

    grant_id = int(query.data.split(":")[2])

    # Fetch the specific grant row
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        grant = await db.fetchrow(
            "SELECT * FROM recent_admin_grants WHERE id=$1", grant_id
        )

    if not grant or grant["from_user_id"] != update.effective_user.id:
        await query.edit_message_text(
            "That selection is no longer valid. Please try /start again.",
        )
        return ConversationHandler.END

    return await _present_single_grant(query, ctx, grant)


# ── Step 2 callback: user confirms the detected chat ─────────────────────────

async def cb_confirm_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """
    User confirmed the chat is correct.
    Stub transition to Step 3 (fee transparency) to be implemented in next chunk.
    """
    query = update.callback_query
    await query.answer()

    w = _w(ctx)
    title = w.get("chat_title", "your group/channel")
    kind  = _chat_kind(w.get("chat_type", "group"))

    # ── Step 3: Fee Transparency ──────────────────────────────────────────────
    from renewise.db.queries import get_global_fees, get_group_by_chat_id
    
    global_buyer, global_admin = await get_global_fees()
    
    # Check if this group already exists and has negotiated overrides
    chat_id = w.get("chat_id")
    group = await get_group_by_chat_id(chat_id) if chat_id else None
    
    buyer_bps = group["buyer_fee_bps"] if (group and group["buyer_fee_bps"] is not None) else global_buyer
    admin_bps = group["admin_fee_bps"] if (group and group["admin_fee_bps"] is not None) else global_admin
    
    buyer_pct = buyer_bps / 100.0   # e.g. 200 bps → 2.00 (for display as "2.00%")
    admin_pct = admin_bps / 100.0
    
    # Worked example at $20 — use bps/10000 for the actual arithmetic
    example_base = 20.0
    buyer_fee = example_base * buyer_bps / 10000
    admin_fee = example_base * admin_bps / 10000
    total_paid = example_base + buyer_fee
    total_received = example_base - admin_fee
    platform_keeps = buyer_fee + admin_fee
    
    text = (
        f"✅ <b>Confirmed:</b> {title} ({kind})\n\n"
        f"<b>Transparent Pricing 💸</b>\n"
        f"Renewise charges a {buyer_pct:.2f}% fee to members and a {admin_pct:.2f}% fee on payouts. "
        f"There are no hidden costs.\n\n"
        f"<i>Example at a $20.00 price point:</i>\n"
        f"• Member pays: <b>${total_paid:.2f}</b>\n"
        f"• You receive: <b>${total_received:.2f}</b>\n"
        f"• Renewise keeps: <b>${platform_keeps:.2f}</b>\n\n"
        f"Prices are pegged to USD but paid in GRAM. The conversion happens automatically at checkout."
    )
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Proceed to Pricing ➡️", callback_data="pw:fee_ok")],
        [InlineKeyboardButton("🔙 Back", callback_data="pw:back_to_instructions")],
    ])
    
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    return STEP_FEE_INFO


# ── Step 4: Interval & Pricing ────────────────────────────────────────────────

async def cb_fee_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """User accepted fees. Ask for billing interval."""
    query = update.callback_query
    await query.answer()

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Weekly", callback_data="pw:interval:7")],
        [InlineKeyboardButton("Monthly", callback_data="pw:interval:30")],
        [InlineKeyboardButton("🔙 Back", callback_data="pw:confirm_chat")],
    ])
    
    await query.edit_message_text(
        "📅 <b>Billing Interval</b>\n\n"
        "How often should members be billed?",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return STEP_INTERVAL


async def cb_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """User selected interval. Ask for USD price."""
    query = update.callback_query
    await query.answer()
    
    days = int(query.data.split(":")[2])
    _w(ctx)["interval"] = days
    
    label = "Weekly" if days == 7 else "Monthly"
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data="pw:fee_ok")]
    ])
    
    await query.edit_message_text(
        f"✅ Interval set to: <b>{label}</b>\n\n"
        "💰 <b>Set Price (USD)</b>\n\n"
        "Enter the subscription price in USD (e.g. <code>9.99</code> or <code>20</code>).\n"
        "Prices are pegged to USD but paid in GRAM. The conversion happens automatically at checkout.",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return STEP_PRICE


async def msg_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed a price. Validate and show equivalent GRAM rate."""
    text = update.message.text.strip().lstrip("$")
    try:
        usd_price = float(text)
        if usd_price < 1.0:
            raise ValueError("Price must be at least $1.00")
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid price. Send a positive USD amount (minimum $1.00), e.g. <code>9.99</code>."
        )
        return STEP_PRICE
        
    cents = round(usd_price * 100)
    _w(ctx)["price_usd_cents"] = cents
    
    from renewise.utils.coingecko import get_ton_usd_price
    ton_price_usd = await get_ton_usd_price()
    equivalent_gram = usd_price / ton_price_usd
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Price", callback_data="pw:price_ok")],
        [InlineKeyboardButton("🔙 Change Price", callback_data=f"pw:interval:{_w(ctx)['interval']}")],
    ])
    
    await update.message.reply_text(
        f"💰 <b>Price Confirmation</b>\n\n"
        f"You set the price to <b>${usd_price:.2f} USD</b>.\n"
        f"At the current market rate ($1 GRAM = ${ton_price_usd:.2f}), members would pay approximately <b>{equivalent_gram:.2f} GRAM</b>.\n\n"
        "Does this look correct?",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return STEP_PRICE_CONFIRM


# ── Step 6: Confirm Price & Setup Wallet ──────────────────────────────────────

async def cb_price_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Price confirmed. Offer wallet reuse if the admin already has one registered."""
    query = update.callback_query
    await query.answer()

    admin_id = update.effective_user.id
    interval  = _w(ctx).get("interval")

    from renewise.db.queries import get_admin_prior_wallet
    prior_wallet = await get_admin_prior_wallet(admin_id)

    if prior_wallet:
        short = f"{prior_wallet[:6]}...{prior_wallet[-4:]}"
        _w(ctx)["prior_wallet"] = prior_wallet   # stash for cb_reuse_wallet
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Use {short} again", callback_data="pw:reuse_wallet")],
            [InlineKeyboardButton("✏️ Enter a different wallet",  callback_data="pw:new_wallet")],
            [InlineKeyboardButton("🔙 Back", callback_data=f"pw:interval:{interval}")],
        ])
        await query.edit_message_text(
            "💼 <b>Payout Wallet</b>\n\n"
            "You already have a wallet registered from a previous paywall.\n"
            f"<code>{prior_wallet}</code>\n\n"
            "Would you like to reuse it or enter a different one?",
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        await _show_wallet_entry(query, interval)

    return STEP_WALLET


async def _show_wallet_entry(query, interval) -> None:
    """Render the plain wallet-address text-entry screen."""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data=f"pw:interval:{interval}")]
    ])
    await query.edit_message_text(
        "💼 <b>Payout Wallet</b>\n\n"
        "Please enter your GRAM wallet address where you'd like to receive payouts.\n\n"
        "<i>Don't have one? You can use the built-in @wallet in Telegram.</i>",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def cb_reuse_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin chose to reuse their previously stored wallet skip text entry."""
    query = update.callback_query
    await query.answer()

    wallet = _w(ctx).get("prior_wallet", "")
    _w(ctx)["wallet_address"] = wallet

    return await _show_final_summary(query, ctx, wallet)


async def cb_enter_new_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin chose to enter a different wallet show the text-entry screen."""
    query = update.callback_query
    await query.answer()
    interval = _w(ctx).get("interval")
    await _show_wallet_entry(query, interval)
    return STEP_WALLET


# ── Step 7: Wallet Validation & Final Summary ─────────────────────────────────

async def msg_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    is_valid = await validate_ton_address(text)
    if not is_valid:
        await update.message.reply_text(
            "❌ Invalid wallet address format.\n\nPlease try again."
        )
        return STEP_WALLET

    _w(ctx)["wallet_address"] = text
    return await _show_final_summary(update.message, ctx, text)


async def _show_final_summary(msg_or_query, ctx: ContextTypes.DEFAULT_TYPE, wallet: str) -> int:
    """Edit or reply with the final summary, then enter STEP_FINAL_CONFIRM."""
    w = _w(ctx)
    # chat_title was already html.escape()'d when stored in wizard state
    title        = w.get("chat_title", "Unknown")
    kind         = _chat_kind(w.get("chat_type", "group"))
    usd_price    = w.get("price_usd_cents", 0) / 100.0
    interval_days = w.get("interval", 30)
    interval_str = "Weekly" if interval_days == 7 else "Monthly"
    short_wallet = f"{wallet[:6]}...{wallet[-4:]}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Activate Paywall", callback_data="pw:final_ok")],
        [InlineKeyboardButton("🔙 Change Wallet",   callback_data="pw:price_ok")],
    ])
    text = (
        f"📝 <b>Final Summary</b>\n\n"
        f"<b>Chat:</b> {title} ({kind})\n"
        f"<b>Price:</b> ${usd_price:.2f} {interval_str}\n"
        f"<b>Payout Wallet:</b> <code>{short_wallet}</code>\n\n"
        "Everything look good? Tap below to activate your paywall."
    )

    # Works whether called from a message handler or a callback query handler
    from telegram import Message, CallbackQuery
    if hasattr(msg_or_query, "edit_message_text"):   # CallbackQuery
        await msg_or_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:                                             # Message
        await msg_or_query.reply_text(text, parse_mode="HTML", reply_markup=kb)

    return STEP_FINAL_CONFIRM


# ── Step 8: Activation ────────────────────────────────────────────────────────

async def cb_final_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Activating paywall...")

    w = _w(ctx)
    chat_id       = w.get("chat_id")
    admin_id      = update.effective_user.id
    price_cents   = w.get("price_usd_cents")
    interval_days = w.get("interval")
    wallet        = w.get("wallet_address")

    # Guard against missing wizard state (e.g. after bot restart)
    if not all([chat_id, price_cents, interval_days, wallet]):
        await query.edit_message_text(
            "⚠️ Session expired. Please use /createpaywall to start again.",
        )
        return ConversationHandler.END

    try:
        from renewise.db.queries import upsert_group, activate_paywall, update_group_price_usd_cents

        chat_title = w.get("chat_title")
        chat_type  = w.get("chat_type", "group")
        group_id = await upsert_group(chat_id, admin_id, chat_type=chat_type)

        # Generate invite link; gracefully continue if it fails
        invite_link = None
        try:
            invite_link_obj = await ctx.bot.create_chat_invite_link(
                chat_id=chat_id,
                creates_join_request=True,
            )
            invite_link = invite_link_obj.invite_link
        except Exception as e:
            log.warning(
                "cb_final_confirm: could not create invite link for chat %s: %s",
                chat_id, e,
            )

        await activate_paywall(
            group_id, interval_days, wallet,
            chat_title=chat_title, invite_link=invite_link, chat_type=chat_type,
        )
        await update_group_price_usd_cents(group_id, price_cents)

        if invite_link:
            link_section = (
                "Share this link with your audience so they can request to join:\n"
                f"👉 <code>{invite_link}</code>\n\n"
            )
        else:
            link_section = (
                "⚠️ I couldn't generate an invite link automatically "
                "please create one manually in your group/channel settings "
                "(set it to require approval) and share it with your audience.\n\n"
            )

        await query.edit_message_text(
            "🎉 <b>You're live!</b>\n\n"
            "Your paywall has been successfully set up and is now active.\n\n"
            + link_section
            + "You can manage your communities anytime from the menu below.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Main Menu", callback_data="start:my_groups")],
            ]),
        )
        ctx.user_data.pop(_W, None)
        return ConversationHandler.END

    except Exception as e:
        log.error("cb_final_confirm failed: chat_id=%s admin=%s error=%s", chat_id, admin_id, e)
        try:
            await query.edit_message_text(
                "❌ Something went wrong activating your paywall.\n\n"
                "Please try /createpaywall again.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return ConversationHandler.END


# ── back navigation callbacks ─────────────────────────────────────────────────

async def cb_back_to_instructions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Return to the 'add me as admin' instructions screen."""
    query = update.callback_query
    await query.answer()
    msg = await query.edit_message_text(
        "🛠️ <b>Step 1: Prepare your group or channel</b>\n\n"
        "Before adding me, you need to set up your group or channel correctly:\n\n"
        "<b>1️⃣ Set it to Private</b>\n"
        "Go to your group/channel settings and change the invite link type to <b>Private</b> "
        "(not public). This ensures non-paying members can't join freely.\n\n"
        "<b>2️⃣ Enable Join Approval</b>\n"
        "In your group settings → Members, turn on <b>Approve New Members</b> "
        "(for groups) or in channel settings enable <b>Join Requests</b> (for channels). "
        "This is how I control who gets in I approve paying members and reject non-payers.\n\n"
        "<b>3️⃣ Add me as Admin</b>\n"
        "Add me as an <b>Admin</b> with these permissions:\n"
        "✅ Add Users <i>(enables both 'Add Users' and 'Process Join Requests')</i>\n"
        "✅ Ban Users\n"
        "✅ Manage Messages <i>(channels only)</i>\n\n"
        "⚠️ <b>Important:</b> Steps 1 and 2 must be done <b>before</b> sharing the invite link "
        "with your members, otherwise anyone can join without paying.\n\n"
        "Once all three steps are done, tap below.",
        parse_mode="HTML",
        reply_markup=_instructions_kb(),
    )
    if hasattr(msg, 'message_id'):
        ctx.user_data["paywall_msg_id"] = msg.message_id
    elif update.effective_message:
        ctx.user_data["paywall_msg_id"] = update.effective_message.message_id
    return STEP_INSTRUCTIONS


async def cb_back_to_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """End the wizard and show the start menu."""
    query = update.callback_query
    await query.answer()
    ctx.user_data.pop(_W, None)   # type: ignore[union-attr]

    from renewise.db import queries as q
    user_id = update.effective_user.id
    groups  = await q.get_groups_for_admin(user_id)

    if not groups:
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
        n = len(groups)
        label = "community" if n == 1 else "communities"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 My Networks", callback_data="start:my_groups")],
            [InlineKeyboardButton("🛠️ Set Up Another",            callback_data="start:create_paywall")],
            [InlineKeyboardButton("❓ How This Works",             callback_data="start:how_it_works")],
        ])
        text = f"👋 <b>Welcome back,{update.effective_user.first_name}</b>\n\nYou're managing <b>{n}</b> paywalled {label}."

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    return ConversationHandler.END


async def cb_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "💬 Need help? Contact us at @RenewiseSupport or reply to this message.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="pw:back_to_instructions")]
        ]),
    )
    return STEP_INSTRUCTIONS


# ── cancel fallback ───────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.pop(_W, None)  # type: ignore[union-attr]
    await update.message.reply_text(
        "Wizard cancelled. Use /start to begin again."
    )
    return ConversationHandler.END


# ── ConversationHandler assembly ──────────────────────────────────────────────

def build_create_paywall_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("createpaywall", cmd_create_paywall),
            CallbackQueryHandler(cmd_create_paywall,  pattern=r"^start:create_paywall$"),
            # Self-contained entry: works after bot restart, grant_id embedded in callback
            CallbackQueryHandler(cb_grant_confirm,    pattern=r"^pw:grant_confirm:\d+$"),
        ],
        states={
            STEP_INSTRUCTIONS: [
                CallbackQueryHandler(cb_check_now,             pattern=r"^pw:check_now$"),
                CallbackQueryHandler(cb_back_to_start,         pattern=r"^pw:back_to_start$"),
                CallbackQueryHandler(cb_support,               pattern=r"^pw:support$"),
            ],
            STEP_DETECT: [
                # Legacy fallback for sessions started before this patch
                CallbackQueryHandler(cb_confirm_chat,          pattern=r"^pw:confirm_chat$"),
                CallbackQueryHandler(cb_back_to_instructions,  pattern=r"^pw:back_to_instructions$"),
                # New self-contained confirm (re-entry after restart)
                CallbackQueryHandler(cb_grant_confirm,         pattern=r"^pw:grant_confirm:\d+$"),
            ],
            STEP_CONFIRM_CHAT: [
                CallbackQueryHandler(cb_pick_chat,             pattern=r"^pw:pick:\d+$"),
                CallbackQueryHandler(cb_back_to_instructions,  pattern=r"^pw:back_to_instructions$"),
            ],
            STEP_FEE_INFO: [
                CallbackQueryHandler(cb_fee_ok,                pattern=r"^pw:fee_ok$"),
                CallbackQueryHandler(cb_back_to_instructions,  pattern=r"^pw:back_to_instructions$"),
            ],
            STEP_INTERVAL: [
                CallbackQueryHandler(cb_interval,              pattern=r"^pw:interval:\d+$"),
                CallbackQueryHandler(cb_confirm_chat,          pattern=r"^pw:confirm_chat$"),
            ],
            STEP_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_price),
                CallbackQueryHandler(cb_fee_ok,                pattern=r"^pw:fee_ok$"),
                CallbackQueryHandler(cb_interval,              pattern=r"^pw:interval:\d+$"),
            ],
            STEP_PRICE_CONFIRM: [
                CallbackQueryHandler(cb_price_ok,              pattern=r"^pw:price_ok$"),
                CallbackQueryHandler(cb_interval,              pattern=r"^pw:interval:\d+$"),
            ],
            STEP_WALLET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_wallet),
                CallbackQueryHandler(cb_reuse_wallet,          pattern=r"^pw:reuse_wallet$"),
                CallbackQueryHandler(cb_enter_new_wallet,      pattern=r"^pw:new_wallet$"),
                CallbackQueryHandler(cb_interval,              pattern=r"^pw:interval:\d+$"),
            ],
            STEP_FINAL_CONFIRM: [
                CallbackQueryHandler(cb_final_confirm,         pattern=r"^pw:final_ok$"),
                CallbackQueryHandler(cb_price_ok,              pattern=r"^pw:price_ok$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CallbackQueryHandler(cb_back_to_start, pattern=r"^pw:back_to_start$"),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
        allow_reentry=True,
    )

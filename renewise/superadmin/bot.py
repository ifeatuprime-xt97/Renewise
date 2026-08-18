"""
renewise Super-Admin Bot
========================
Authorised users only — access_control middleware silently blocks everyone else.

Available commands
------------------
/start          — main menu
/overview       — platform stats
/groups         — paginated group list
/killswitch     — global kill switch
/lookup <term>  — search by TX hash or Telegram user ID
/msgadmin <id> <text>  — DM an admin via the main bot
/pendingrefunds — overpayment refund queue
/auditlog       — recent platform audit events
/help           — command list
"""
import html
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, TypeHandler, CallbackQueryHandler,
    ApplicationHandlerStop, MessageHandler, filters,
)

from renewise.config import SUPERADMIN_BOT_TOKEN, ALLOWED_SUPERADMIN_IDS, BOT_TOKEN
from renewise.config import BUYER_FEE_BPS, ADMIN_FEE_BPS
from renewise.superadmin.queries import (
    get_platform_overview,
    search_by_tx_hash, search_by_user_id,
    audit_manual_recheck, get_vault_by_tx_hash, get_tx_processed_status,
    set_group_fees, get_group_fee_config,
    get_groups_page, get_total_groups_count,
    get_users_page, get_total_users_count,
    get_group_details, set_group_status,
    get_groups_by_admin,
    get_active_admins_page, get_total_active_admins_count,
    get_banned_admins_page, get_total_banned_admins_count,
    get_pending_send_refund_count, get_trigger_wallet_balance,
)
from renewise.watcher.toncenter import fetch_single_transaction, extract_in_msg_value
from renewise.watcher.db import is_tx_processed

log = logging.getLogger(__name__)

# ── Middleware ────────────────────────────────────────────────────────────────

async def access_control(update: Update, context):
    """Block every update from non-allowlisted users — silently, no reply."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in ALLOWED_SUPERADMIN_IDS:
        raise ApplicationHandlerStop()

# ── Main menu ─────────────────────────────────────────────────────────────────

# ── Trigger wallet health helpers ─────────────────────────────────────────────

_TW_LOW_TON      = 0.10   # warn below this
_TW_CRITICAL_TON = 0.02   # critical below this
_GAS_PER_REFUND  = 0.01   # TON attached per Refund{} message


def _trigger_health(balance: float | None, configured: bool) -> tuple[str, str]:
    """
    Returns (icon, label) for the trigger wallet.
    balance is in TON, or None if fetch failed.
    configured=False when TRIGGER_WALLET/TRIGGER_MNEMONIC are absent.
    """
    if not configured:
        return "⚫", "Not configured"
    if balance is None:
        return "❓", "Balance unavailable (TonCenter error)"
    if balance < _TW_CRITICAL_TON:
        return "🔴", f"CRITICAL — {balance:.4f} TON"
    if balance < _TW_LOW_TON:
        return "🟡", f"Low — {balance:.4f} TON"
    return "🟢", f"Healthy — {balance:.4f} TON"


async def _guard_group_exists(update: Update, group_id: int) -> bool:
    """Return False and close stale super-admin flows when the target group is gone."""
    from renewise.db.queries import get_group_by_id
    group = await get_group_by_id(group_id)
    if group is not None:
        return True

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "⚠️ This group no longer exists or was deleted.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")]]),
        )
    else:
        await update.message.reply_text("⚠️ This group no longer exists or was deleted.")
    return False


async def _guard_refund_exists(update: Update, refund_id: int) -> bool:
    """Return False if the refund row was already cancelled or removed."""
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        row = await db.fetchrow("SELECT id FROM overpayment_refunds WHERE id=$1", refund_id)
    if row is not None:
        return True

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "⚠️ This refund is no longer available.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")]]),
        )
    else:
        await update.message.reply_text("⚠️ This refund is no longer available.")
    return False


async def show_main_menu(update: Update, context):
    from renewise.db.queries import is_payments_paused
    from renewise.config import TRIGGER_WALLET, TONCENTER_API_KEY, TONCENTER_TESTNET
    paused   = await is_payments_paused()
    status   = "🔴 PAUSED" if paused else "🟢 Live"

    # Quick trigger health badge for the menu header
    tw_configured = bool(TRIGGER_WALLET)
    tw_balance    = await get_trigger_wallet_balance(
        TRIGGER_WALLET, TONCENTER_API_KEY, TONCENTER_TESTNET
    ) if tw_configured else None
    tw_icon, _    = _trigger_health(tw_balance, tw_configured)

    pending_refunds = await get_pending_send_refund_count()
    refund_badge    = f" ⚠️ {pending_refunds} pending" if pending_refunds else ""

    text = (
        f"🛡 <b>renewise Super-Admin</b>\n\n"
        f"Platform: <b>{status}</b>\n"
        f"Trigger wallet: <b>{tw_icon}</b>{refund_badge}\n\n"
        "Choose an action:"
    )
    keyboard = [
        [
            InlineKeyboardButton("📊 Platform Stats",    callback_data="sa_home_stats"),
            InlineKeyboardButton("📂 Groups",            callback_data="sa_page_0"),
        ],
        [
            InlineKeyboardButton("👥 Users",             callback_data="sa_upage_0"),
            InlineKeyboardButton("🔍 Lookup TX/User",    callback_data="sa_lookup_prompt"),
        ],
        [
            InlineKeyboardButton("💸 Refunds",           callback_data="sa_refunds_0"),
            InlineKeyboardButton("📋 Audit Log",         callback_data="sa_audit_0"),
        ],
        [
            InlineKeyboardButton("⏳ Wallet Changes",    callback_data="sa_wc_pending"),
            InlineKeyboardButton("🚨 Kill Switch",       callback_data="sa_ks_status"),
        ],
        [
            InlineKeyboardButton("⚡ Trigger Wallet",   callback_data="sa_trigger_status"),
            InlineKeyboardButton("⚙️ Settings",          callback_data="sa_settings_menu"),
        ],
    ]
    # Clear all multi-step flow state
    for key in ("fee_group_id", "sa_price_group_id", "sa_price_pending_cents",
                "msg_admin_target", "reply_target", "lookup_pending", "global_fee_pending"):
        context.user_data.pop(key, None)

    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

async def start_cmd(update: Update, context):
    await show_main_menu(update, context)

async def help_cmd(update: Update, context):
    text = (
        "🛡 <b>Super-Admin Commands</b>\n\n"
        "/start — main menu\n"
        "/overview — platform stats\n"
        "/groups — paginated group list\n"
        "/killswitch — global kill switch\n"
        "/lookup &lt;tx_hash|user_id&gt; — search\n"
        "/msgadmin &lt;user_id&gt; &lt;text&gt; — DM admin via main bot\n"
        "/pendingrefunds — overpayment refund queue\n"
        "/auditlog — recent audit events\n"
        "/help — this list"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ── Platform overview ─────────────────────────────────────────────────────────

async def show_overview(update: Update, context):
    from renewise.db.queries import get_global_fees, is_payments_paused
    from renewise.config import TRIGGER_WALLET, TONCENTER_API_KEY, TONCENTER_TESTNET
    stats            = await get_platform_overview()
    buyer_bps, admin_bps = await get_global_fees()
    fee_pct          = (buyer_bps + admin_bps) / 10000.0
    fee_rev          = stats["monthly_gmv"] * fee_pct
    paused           = await is_payments_paused()
    ks_status        = "🔴 PAUSED" if paused else "🟢 Live"

    # Trigger wallet health
    tw_configured    = bool(TRIGGER_WALLET)
    tw_balance       = await get_trigger_wallet_balance(
        TRIGGER_WALLET, TONCENTER_API_KEY, TONCENTER_TESTNET
    ) if tw_configured else None
    tw_icon, tw_label = _trigger_health(tw_balance, tw_configured)
    pending_refunds  = await get_pending_send_refund_count()
    refund_warn      = f" ⚠️ <b>{pending_refunds} pending send</b>" if pending_refunds else " ✅ None pending"

    # Estimate how many more refunds the trigger can cover
    if tw_balance is not None and tw_balance > 0:
        can_cover = int(tw_balance / _GAS_PER_REFUND)
        gas_note  = f"Can cover ~{can_cover} more refund{'s' if can_cover != 1 else ''} at 0.01 TON/each"
    elif tw_configured:
        gas_note = "⚠️ Cannot cover any refunds — top up required!"
    else:
        gas_note = "Manual refunds only (TRIGGER_WALLET not set)"

    msg = (
        "📊 <b>Platform Overview — This Month</b>\n\n"
        f"<b>Kill Switch:</b>    {ks_status}\n"
        f"<b>GMV:</b>            ${stats['monthly_gmv']:.2f} USD\n"
        f"<b>Fee Revenue:</b>    ${fee_rev:.2f} USD\n"
        f"<b>Active Admins:</b>  {stats['active_admins']}\n"
        f"<b>Active Subs:</b>    {stats['active_subs']}\n\n"
        f"⚡ <b>Trigger Wallet:</b> {tw_icon} {tw_label}\n"
        f"   {gas_note}\n"
        f"💸 <b>Pending Refunds:</b>{refund_warn}"
    )
    kb = [
        [InlineKeyboardButton("⚡ Trigger Wallet", callback_data="sa_trigger_status")],
        [InlineKeyboardButton("◀️ Main Menu",      callback_data="sa_home_main")],
    ]
    markup = InlineKeyboardMarkup(kb)
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=markup)

async def overview_cmd(update: Update, context):
    await show_overview(update, context)

# ── Groups directory ──────────────────────────────────────────────────────────

async def groups_cmd(update: Update, context):
    await show_groups_page(update, context, page=0)

async def show_groups_page(update: Update, context, page: int):
    LIMIT = 5
    offset = page * LIMIT
    groups = await get_groups_page(limit=LIMIT, offset=offset)
    total  = await get_total_groups_count()

    if not groups and page == 0:
        msg = "No groups found."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return

    text = f"📂 <b>Groups Directory</b> (Page {page + 1})\n\n"
    keyboard = []
    for g in groups:
        dot = "🟢" if g["status"] == "active" else ("🔴" if g["status"] == "suspended" else "⚪")
        price_cents = g.get("price_usd_cents") or int((g.get("price") or 0) * 100)
        title = html.escape(g.get("chat_title") or str(g["telegram_chat_id"]))
        text += (
            f"{dot} ID:<b>{g['id']}</b> <i>{title}</i>\n"
            f"   Admin: <code>{g['admin_telegram_id']}</code> | "
            f"${price_cents / 100:.2f} USD\n\n"
        )
        keyboard.append([InlineKeyboardButton(f"Manage #{g['id']} {title[:20]}", callback_data=f"sa_group_{g['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sa_page_{page - 1}"))
    if offset + LIMIT < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"sa_page_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")])

    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

# ── Group detail ──────────────────────────────────────────────────────────────

async def show_group_details(update: Update, context, group_id: int):
    g = await get_group_details(group_id)
    if not g:
        await update.callback_query.edit_message_text("Group not found.")
        return

    price_cents = g.get("price_usd_cents") or int((g.get("price") or 0) * 100)
    title = html.escape(g.get("chat_title") or str(g["telegram_chat_id"]))
    text = (
        f"📝 <b>Group #{g['id']} — {title}</b>\n\n"
        f"<b>Chat ID:</b>   <code>{g['telegram_chat_id']}</code>\n"
        f"<b>Admin ID:</b>  <code>{g['admin_telegram_id']}</code>\n"
        f"<b>Price:</b>     ${price_cents / 100:.2f} USD / {g.get('billing_interval_days', 30)}d\n"
        f"<b>Status:</b>    {g['status'].upper()}\n"
        f"<b>Type:</b>      {g.get('chat_type', 'group')}\n\n"
        f"👥 <b>Active Subscribers:</b> {g['active_subs']}\n"
        f"💰 <b>Total Revenue:</b>      ${g['total_revenue']:.2f} USD\n"
    )
    keyboard = []
    if g["status"] != "suspended":
        keyboard.append([InlineKeyboardButton("⛔ Suspend Group", callback_data=f"sa_suspend_{g['id']}")])
    else:
        keyboard.append([InlineKeyboardButton("✅ Unsuspend Group", callback_data=f"sa_unsuspend_{g['id']}")])

    keyboard.append([
        InlineKeyboardButton("💰 Update Price",    callback_data=f"sa_price_{g['id']}"),
        InlineKeyboardButton("⚙️ Override Fees",   callback_data=f"sa_fees_{g['id']}"),
    ])
    keyboard.append([InlineKeyboardButton("💳 Payment History", callback_data=f"sa_pmthist_{g['id']}_0")])
    keyboard.append([InlineKeyboardButton("✉️ Message Admin",   callback_data=f"sa_msgadmin_{g['admin_telegram_id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Back to List",    callback_data="sa_page_0")])
    keyboard.append([InlineKeyboardButton("◀️ Main Menu",       callback_data="sa_home_main")])

    await update.callback_query.edit_message_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ── Payment history (SA view) ─────────────────────────────────────────────────

async def show_sa_payment_history(update: Update, context, group_id: int, page: int):
    from renewise.db.queries import get_payment_history
    LIMIT = 5
    offset = page * LIMIT
    rows, total = await get_payment_history(group_id, offset, LIMIT)
    total_pages = max(1, -(-total // LIMIT))

    icon_map = {"active": "✅", "comped": "🎁", "expired": "⏰", "cancelled": "❌", "pending": "⏳"}
    if not rows:
        text = f"💳 <b>Payment History — Group {group_id}</b>\n\nNo records."
    else:
        lines = [f"💳 <b>Payment History — Group {group_id}</b> ({page + 1}/{total_pages})\n"]
        for r in rows:
            name  = html.escape(r["first_name"] or "Unknown")
            uname = f" (@{html.escape(r['username'])})" if r["username"] else ""
            raw   = r["price_locked_in"] or 0
            # price_locked_in is stored in USD dollars (e.g. 9.99 = $9.99)
            price_str = f"${raw:.2f}" if raw < 1000 else f"{raw / 1e9:.4f} TON"
            tx = f"<code>{r['last_payment_tx_hash'][:12]}…</code>" if r["last_payment_tx_hash"] else "—"
            lines.append(
                f"{icon_map.get(r['status'], '•')} <b>{name}</b>{uname}\n"
                f"   💰 {price_str} | 🗓 {r['start_date'] or 'N/A'}\n"
                f"   🔄 {r['next_renewal_date'] or 'N/A'} | TX: {tx}\n"
            )
        text = "\n".join(lines)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sa_pmthist_{group_id}_{page - 1}"))
    if (offset + LIMIT) < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"sa_pmthist_{group_id}_{page + 1}"))

    kb = []
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 Back to Group", callback_data=f"sa_group_{group_id}")])
    kb.append([InlineKeyboardButton("◀️ Main Menu",     callback_data="sa_home_main")])
    await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

# ── Users directory ───────────────────────────────────────────────────────────

async def show_users_page(update: Update, context, page: int):
    LIMIT = 5
    offset = page * LIMIT
    users = await get_users_page(limit=LIMIT, offset=offset)
    total = await get_total_users_count()

    if not users and page == 0:
        msg = "No users found."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return

    text = f"👥 <b>Users Directory</b> (Page {page + 1})\n\n"
    keyboard = []
    for u in users:
        name  = html.escape(u.get("first_name") or "Unknown")
        uname = f" (@{html.escape(u['username'])})" if u.get("username") else ""
        text += f"👤 <b>{name}</b>{uname} | TG: <code>{u['telegram_user_id']}</code>\n"
        keyboard.append([InlineKeyboardButton(f"View {name}", callback_data=f"sa_u_{u['telegram_user_id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sa_upage_{page - 1}"))
    if offset + LIMIT < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"sa_upage_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")])

    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

# ── Kill switch ───────────────────────────────────────────────────────────────

async def show_killswitch_status(update: Update, context):
    from renewise.db.queries import is_payments_paused
    paused = await is_payments_paused()
    if paused:
        text = "🚨 <b>Kill Switch: PAUSED</b>\n\nAll new payment processing is stopped."
        kb   = [[InlineKeyboardButton("✅ Resume Payments", callback_data="sa_ks_resume_prompt")]]
    else:
        text = "✅ <b>Kill Switch: ACTIVE</b>\n\nPayments are processing normally."
        kb   = [[InlineKeyboardButton("⛔ Pause Payments",  callback_data="sa_ks_pause_prompt")]]
    kb.append([InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")])
    markup = InlineKeyboardMarkup(kb)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

async def killswitch_cmd(update: Update, context):
    await show_killswitch_status(update, context)

# ── Trigger Wallet Status ─────────────────────────────────────────────────────

async def show_trigger_status(update: Update, context):
    """Live trigger wallet health screen with balance, gas estimate, and refund count."""
    from renewise.config import (
        TRIGGER_WALLET, TRIGGER_MNEMONIC, TONCENTER_API_KEY, TONCENTER_TESTNET
    )

    tw_configured = bool(TRIGGER_WALLET and TRIGGER_MNEMONIC)
    tw_balance    = await get_trigger_wallet_balance(
        TRIGGER_WALLET, TONCENTER_API_KEY, TONCENTER_TESTNET
    ) if tw_configured else None

    tw_icon, tw_label = _trigger_health(tw_balance, tw_configured)
    pending_refunds   = await get_pending_send_refund_count()
    refund_warn       = (
        f"⚠️ <b>{pending_refunds} refund(s) stuck in pending_send</b>\n"
        "These will process automatically once the wallet is topped up."
        if pending_refunds
        else "✅ No refunds are pending."
    )

    if tw_balance is not None and tw_balance > 0:
        can_cover = int(tw_balance / _GAS_PER_REFUND)
        gas_note  = (
            f"At 0.01 TON/refund, this covers <b>~{can_cover} more refund"
            f"{'s' if can_cover != 1 else ''}</b>."
        )
    elif tw_configured:
        gas_note  = "⚠️ <b>Cannot cover any refunds — please top up immediately!</b>"
    else:
        gas_note  = "Automatic refunds are <b>disabled</b>. Set TRIGGER_WALLET and TRIGGER_MNEMONIC."

    # Truncated address for display
    addr_display = (
        f"<code>{TRIGGER_WALLET[:10]}…{TRIGGER_WALLET[-6:]}</code>"
        if tw_configured and len(TRIGGER_WALLET) > 18
        else (f"<code>{TRIGGER_WALLET}</code>" if tw_configured else "—")
    )
    network_label = "Testnet" if TONCENTER_TESTNET else "Mainnet"

    text = (
        f"⚡ <b>Trigger Wallet Status</b> ({network_label})\n\n"
        f"<b>Health:</b>    {tw_icon} {tw_label}\n"
        f"<b>Address:</b>   {addr_display}\n\n"
        f"{gas_note}\n\n"
        f"💸 <b>Pending Refunds:</b>\n{refund_warn}"
    )

    kb = [
        [InlineKeyboardButton("🔄 Refresh Balance", callback_data="sa_trigger_refresh")],
        [InlineKeyboardButton("💸 View Refund Queue", callback_data="sa_refunds_0")],
        [InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")],
    ]
    markup = InlineKeyboardMarkup(kb)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

# ── Overpayment refunds ───────────────────────────────────────────────────────

async def show_refunds_page(update: Update, context, page: int):
    """Paginated view of the overpayment_refunds table with cancel action."""
    from renewise.db.connection import _db as _conn
    LIMIT = 8
    offset = page * LIMIT

    async with _conn() as db:
        rows = await db.fetch(
            "SELECT r.id, r.status, r.refund_nano, r.refund_usd, "
            "r.refund_wallet, r.created_at, r.resolved_at, "
            "u.telegram_user_id, g.chat_title, g.id AS group_id "
            "FROM overpayment_refunds r "
            "JOIN users  u ON u.id = r.user_id "
            "JOIN groups g ON g.id = r.group_id "
            "ORDER BY r.created_at DESC "
            "LIMIT $1 OFFSET $2",
            LIMIT, offset,
        )
        total = int(await db.fetchval("SELECT COUNT(*) FROM overpayment_refunds") or 0)

    total_pages = max(1, -(-total // LIMIT))
    icon = {"pending_wallet": "⏳", "pending_send": "📤", "sent": "✅", "cancelled": "❌"}
    if not rows:
        text = "💸 <b>Overpayment Refunds</b>\n\nNo records."
    else:
        lines = [f"💸 <b>Overpayment Refunds</b> ({page + 1}/{total_pages})\n"]
        for r in rows:
            ton = (r["refund_nano"] or 0) / 1_000_000_000
            w   = r["refund_wallet"] or "—"
            w_s = f"{w[:8]}…{w[-5:]}" if len(w) > 16 else w
            lines.append(
                f"{icon.get(r['status'], '•')} <b>#{r['id']}</b> | "
                f"{html.escape(r['chat_title'] or '?')} | "
                f"User <code>{r['telegram_user_id']}</code>\n"
                f"   {ton:.4f} TON ≈${r['refund_usd']:.2f} → <code>{w_s}</code>\n"
                f"   {r['status']} | {(r['resolved_at'] or r['created_at'] or '')[:16]}\n"
            )
        text = "\n".join(lines)

    kb = []
    # Cancel buttons for pending rows (only show on first page to keep keyboard sane)
    if page == 0:
        for r in rows:
            if r["status"] in ("pending_wallet", "pending_send"):
                kb.append([InlineKeyboardButton(
                    f"❌ Cancel refund #{r['id']}",
                    callback_data=f"sa_refund_cancel_{r['id']}"
                )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sa_refunds_{page - 1}"))
    if (offset + LIMIT) < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"sa_refunds_{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")])

    markup = InlineKeyboardMarkup(kb)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

async def pending_refunds_cmd(update: Update, context):
    await show_refunds_page(update, context, page=0)

# ── Audit log viewer ──────────────────────────────────────────────────────────

async def show_audit_page(update: Update, context, page: int):
    """Paginated platform-wide audit log — read-only."""
    from renewise.db.connection import _db as _conn
    LIMIT = 8
    offset = page * LIMIT

    async with _conn() as db:
        rows = await db.fetch(
            "SELECT * FROM admin_audit_log ORDER BY created_at DESC LIMIT $1 OFFSET $2",
            LIMIT, offset,
        )
        total = int(await db.fetchval("SELECT COUNT(*) FROM admin_audit_log") or 0)

    total_pages = max(1, -(-total // LIMIT))
    if not rows:
        text = "📋 <b>Audit Log</b>\n\nNo entries."
    else:
        lines = [f"📋 <b>Audit Log</b> ({page + 1}/{total_pages})\n"]
        for r in rows:
            gid  = f"g{r['group_id']}" if r["group_id"] else "—"
            det  = (r["details"] or "")[:60]
            ts   = (r["created_at"] or "")[:16]
            lines.append(
                f"• <b>{html.escape(r['action'])}</b>\n"
                f"  actor:<code>{r['actor_telegram_id']}</code> | {gid} | {ts}\n"
                f"  {html.escape(det)}\n"
            )
        text = "\n".join(lines)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sa_audit_{page - 1}"))
    if (offset + LIMIT) < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"sa_audit_{page + 1}"))

    kb = []
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")])
    markup = InlineKeyboardMarkup(kb)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

async def auditlog_cmd(update: Update, context):
    await show_audit_page(update, context, page=0)

# ── Pending wallet changes ────────────────────────────────────────────────────

async def show_pending_wallet_changes(update: Update, context):
    """All pending wallet changes across all groups — superadmin read + cancel view."""
    from renewise.db.connection import _db as _conn

    async with _conn() as db:
        rows = await db.fetch(
            "SELECT w.*, g.chat_title, g.admin_telegram_id "
            "FROM pending_wallet_changes w "
            "JOIN groups g ON g.id = w.group_id "
            "WHERE w.status = 'pending' "
            "ORDER BY w.activates_at ASC"
        )

    if not rows:
        text = "⏳ <b>Pending Wallet Changes</b>\n\nNo pending changes."
    else:
        lines = [f"⏳ <b>Pending Wallet Changes ({len(rows)})</b>\n"]
        for r in rows:
            nw  = r["new_wallet_address"] or "?"
            nw_s = f"{nw[:8]}…{nw[-5:]}" if len(nw) > 16 else nw
            lines.append(
                f"• <b>Change #{r['id']}</b> | Group {r['group_id']} "
                f"<i>{html.escape(r['chat_title'] or '?')}</i>\n"
                f"  Admin: <code>{r['admin_telegram_id']}</code>\n"
                f"  → <code>{nw_s}</code>\n"
                f"  Activates: {(r['activates_at'] or '')[:16]}\n"
            )
        text = "\n".join(lines)

    kb = [[InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")]]
    markup = InlineKeyboardMarkup(kb)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

# ── Settings menu ─────────────────────────────────────────────────────────────

async def show_settings_menu(update: Update, context):
    from renewise.db.queries import get_global_fees
    buyer_bps, admin_bps = await get_global_fees()
    text = (
        "⚙️ <b>Platform Settings</b>\n\n"
        f"Global default fees:\n"
        f"  Buyer fee: <b>{buyer_bps} bps ({buyer_bps / 100:.2f}%)</b>\n"
        f"  Admin fee: <b>{admin_bps} bps ({admin_bps / 100:.2f}%)</b>\n\n"
        "<i>Per-group overrides take precedence.</i>"
    )
    kb = [
        [InlineKeyboardButton("💰 Adjust Global Fee",  callback_data="sa_settings_globalfee")],
        [
            InlineKeyboardButton("🚫 Ban Admin",   callback_data="sa_settings_ban_prompt"),
            InlineKeyboardButton("✅ Unban Admin", callback_data="sa_settings_unban_prompt"),
        ],
        [InlineKeyboardButton("📡 Rate Feed Status", callback_data="sa_settings_ratefeed")],
        [InlineKeyboardButton("◀️ Main Menu",         callback_data="sa_home_main")],
    ]
    q = update.callback_query
    if q:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_rate_feed_status(update: Update, context):
    import time
    from renewise.utils.coingecko import _cache, STALE_THRESHOLD
    now        = time.time()
    last_fetch = _cache["timestamp"]
    price      = _cache["price_usd"]
    if last_fetch == 0:
        age_str  = "Never fetched"
        is_stale = True
    else:
        age_s    = now - last_fetch
        age_str  = f"{age_s / 3600:.2f} hours ago"
        is_stale = age_s > STALE_THRESHOLD
    indicator = "🔴 STALE" if is_stale else "🟢 FRESH"
    text = (
        f"📡 <b>Rate Feed Status</b>\n\n"
        f"<b>Status:</b>       {indicator}\n"
        f"<b>Cached Rate:</b>  ${price:.4f} USD\n"
        f"<b>Last Fetch:</b>   {age_str}\n\n"
        f"<i>Threshold: {STALE_THRESHOLD / 3600:.1f} h. Payments use last known rate if stale.</i>"
    )
    kb = [[InlineKeyboardButton("◀️ Back to Settings", callback_data="sa_settings_menu")]]
    await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_global_fee_prompt(update: Update, context):
    from renewise.db.queries import get_global_fees
    buyer_bps, admin_bps = await get_global_fees()
    context.user_data["global_fee_pending"] = True
    kb = [
        [InlineKeyboardButton("🔄 Reset to .env Default", callback_data="sa_settings_globalfee_reset_prompt")],
        [InlineKeyboardButton("❌ Cancel", callback_data="sa_settings_menu")],
    ]
    await update.callback_query.edit_message_text(
        f"💰 <b>Adjust Global Default Fee</b>\n\n"
        f"Current: buyer <b>{buyer_bps} bps ({buyer_bps / 100:.2f}%)</b> | "
        f"admin <b>{admin_bps} bps ({admin_bps / 100:.2f}%)</b>\n\n"
        "Send new values as: <code>buyer_bps admin_bps</code>\n"
        "Example: <code>200 330</code>  (2.00% buyer, 3.30% admin)\n"
        "Range 0–2000 each.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def show_ban_admin_page(update: Update, context, page: int):
    LIMIT = 5
    offset = page * LIMIT
    admins = await get_active_admins_page(limit=LIMIT, offset=offset)
    total  = await get_total_active_admins_count()
    if not admins and page == 0:
        await update.callback_query.edit_message_text(
            "No active admins found.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="sa_settings_menu")]]),
        )
        return
    text = f"🚫 <b>Select Admin to Ban</b> (Page {page + 1})\n\n"
    kb = [[InlineKeyboardButton(f"Admin {a['admin_telegram_id']}", callback_data=f"sa_ban_select_{a['admin_telegram_id']}")] for a in admins]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sa_ban_page_{page - 1}"))
    if offset + LIMIT < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"sa_ban_page_{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("◀️ Back to Settings", callback_data="sa_settings_menu")])
    await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_unban_admin_page(update: Update, context, page: int):
    LIMIT = 5
    offset = page * LIMIT
    admins = await get_banned_admins_page(limit=LIMIT, offset=offset)
    total  = await get_total_banned_admins_count()
    if not admins and page == 0:
        await update.callback_query.edit_message_text(
            "No banned admins.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="sa_settings_menu")]]),
        )
        return
    text = f"✅ <b>Select Admin to Unban</b> (Page {page + 1})\n\n"
    kb = [[InlineKeyboardButton(f"Admin {a['telegram_id']} ({a['reason']})", callback_data=f"sa_unban_select_{a['telegram_id']}")] for a in admins]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sa_unban_page_{page - 1}"))
    if offset + LIMIT < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"sa_unban_page_{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("◀️ Back to Settings", callback_data="sa_settings_menu")])
    await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_ban_reason_prompt(update: Update, context, target_id: int):
    text = f"🚫 <b>Ban Reason for Admin <code>{target_id}</code></b>\n\nSelect a reason:"
    kb = [
        [InlineKeyboardButton("Spamming",      callback_data=f"sa_settings_ban_confirm_{target_id}_spam")],
        [InlineKeyboardButton("Fraud/Scam",    callback_data=f"sa_settings_ban_confirm_{target_id}_scam")],
        [InlineKeyboardButton("TOS Violation", callback_data=f"sa_settings_ban_confirm_{target_id}_tos")],
        [InlineKeyboardButton("Other",         callback_data=f"sa_settings_ban_confirm_{target_id}_other")],
        [InlineKeyboardButton("◀️ Back", callback_data="sa_ban_page_0"), InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")],
    ]
    await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

# ── Lookup (TX hash or user ID) ───────────────────────────────────────────────

async def perform_lookup(update: Update, context, term: str) -> None:
    is_cb = bool(update.callback_query)

    async def send(text, markup):
        if is_cb:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")]])

    if term.isdigit():
        uid = int(term)
        from renewise.db.queries import get_user_by_telegram_id
        admin_groups = await get_groups_by_admin(uid)
        subs         = await search_by_user_id(uid)
        user_db      = await get_user_by_telegram_id(uid)

        name  = html.escape(user_db.get("first_name") or "Unknown") if user_db else "Unknown"
        uname = (f" (@{html.escape(user_db['username'])})" if user_db and user_db.get("username") else "")

        if not subs and not admin_groups:
            await send(f"No data found for user <code>{uid}</code> ({name}).", back_kb)
            return

        text = f"👤 <b>{name}</b>{uname} | <code>{uid}</code>\n\n"
        kb   = []

        if admin_groups:
            text += f"👑 <b>Admin of {len(admin_groups)} group(s):</b>\n"
            for g in admin_groups:
                dot   = "🟢" if g["status"] == "active" else "🔴"
                price = (g.get("price_usd_cents") or int((g.get("price") or 0) * 100))
                title = html.escape(g.get("chat_title") or str(g["telegram_chat_id"]))
                text += (
                    f"{dot} #{g['id']} <i>{title}</i> | "
                    f"${price / 100:.2f} | {g['active_subs']} subs\n"
                )
                kb.append([InlineKeyboardButton(f"Manage #{g['id']}", callback_data=f"sa_group_{g['id']}")])

        if subs:
            text += f"\n📋 <b>Subscriptions ({len(subs)}):</b>\n"
            for s in subs:
                raw = s.get("price_locked_in") or 0
                # price_locked_in is USD dollars (e.g. 9.99 = $9.99)
                price_str = f"${raw:.2f}" if raw < 1000 else f"{raw / 1e9:.4f} TON"
                text += (
                    f"• Chat <code>{s['telegram_chat_id']}</code> | "
                    f"{s['status'].upper()} | {price_str}\n"
                    f"  Renews: {s.get('next_renewal_date') or 'N/A'}\n"
                    f"  TX: <code>{(s.get('last_payment_tx_hash') or 'None')}</code>\n"
                )
                if s.get("last_payment_tx_hash"):
                    kb.append([InlineKeyboardButton(
                        f"🔄 Recheck TX {s['last_payment_tx_hash'][:8]}…",
                        callback_data=f"sa_r_{s['last_payment_tx_hash']}",
                    )])

        kb.append([InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")])
        await send(text.strip(), InlineKeyboardMarkup(kb))

    else:
        res = await search_by_tx_hash(term)
        if not res:
            await send(f"TX hash <code>{html.escape(term)}</code> not found.", back_kb)
            return

        if res["type"] == "processed":
            text = (
                f"🔗 <b>TX {html.escape(term)}</b>\n\n"
                f"<b>Status:</b> PROCESSED\n"
                f"<b>At:</b> {res['processed_at']}\n"
            )
            if res["subscription"]:
                s = res["subscription"]
                text += (
                    f"<b>Sub ID:</b>   {s['id']}\n"
                    f"<b>User ID:</b>  {s['user_id']}\n"
                    f"<b>Group ID:</b> {s['group_id']}\n"
                )
            else:
                text += "<b>Sub:</b> None (failed verification or missing sub_id)\n"
        else:
            s = res["subscription"]
            text = (
                f"🔗 <b>TX {html.escape(term)}</b>\n\n"
                f"<b>Status:</b> UNPROCESSED (in subscription records)\n"
                f"<b>Sub ID:</b> {s['id']}\n"
            )

        kb = [
            [InlineKeyboardButton("🔄 Manual Recheck", callback_data=f"sa_r_{term}")],
            [InlineKeyboardButton("◀️ Main Menu",       callback_data="sa_home_main")],
        ]
        await send(text, InlineKeyboardMarkup(kb))

async def lookup_cmd(update: Update, context):
    if not context.args:
        await update.message.reply_text("Usage: /lookup &lt;tx_hash|telegram_user_id&gt;", parse_mode="HTML")
        return
    await perform_lookup(update, context, context.args[0])

async def lookup_text_handler(update: Update, context):
    if not context.user_data.pop("lookup_pending", False):
        return
    await perform_lookup(update, context, update.message.text.strip())

# ── Message admin ─────────────────────────────────────────────────────────────

async def msgadmin_text_handler(update: Update, context):
    admin_tg_id = context.user_data.pop("msg_admin_target", None)
    if admin_tg_id is None:
        return
    text    = update.message.text.strip()
    actor   = update.effective_user.id
    sent    = False
    if BOT_TOKEN:
        from telegram import Bot
        try:
            async with Bot(BOT_TOKEN) as bot:
                await bot.send_message(
                    chat_id=admin_tg_id,
                    text=f"📢 <b>Message from renewise platform:</b>\n\n{html.escape(text)}",
                    parse_mode="HTML",
                )
            sent = True
        except Exception as exc:
            log.warning("SA: failed to DM admin %d: %s", admin_tg_id, exc)
    from renewise.db.queries import audit
    await audit(None, "admin_messaged", actor,
                {"target_admin_telegram_id": admin_tg_id, "sent": sent, "preview": text[:80]})
    await update.message.reply_text("✅ Sent." if sent else "⚠️ Could not deliver.")

async def cancelmsg_cmd(update: Update, context):
    context.user_data.pop("msg_admin_target", None)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            "Message cancelled.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")]]),
        )
    else:
        await update.message.reply_text("Message cancelled.")

async def msgadmin_cmd(update: Update, context):
    """Direct: /msgadmin <user_id> <text>"""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /msgadmin &lt;telegram_user_id&gt; &lt;message&gt;", parse_mode="HTML")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("First argument must be a numeric Telegram user ID.")
        return
    msg_text = " ".join(context.args[1:])
    actor    = update.effective_user.id
    sent     = False
    if BOT_TOKEN:
        from telegram import Bot
        try:
            async with Bot(BOT_TOKEN) as bot:
                await bot.send_message(
                    chat_id=target,
                    text=f"📢 <b>Message from renewise platform:</b>\n\n{html.escape(msg_text)}",
                    parse_mode="HTML",
                )
            sent = True
        except Exception as exc:
            log.warning("SA: /msgadmin failed for %d: %s", target, exc)
    from renewise.db.queries import audit
    await audit(None, "admin_messaged", actor,
                {"target_admin_telegram_id": target, "sent": sent, "preview": msg_text[:80]})
    await update.message.reply_text("✅ Sent." if sent else "⚠️ Could not deliver.")

# ── Fee override text handler ─────────────────────────────────────────────────

async def fee_text_handler(update: Update, context):
    group_id = context.user_data.pop("fee_group_id", None)
    if group_id is None:
        return
    parts = update.message.text.strip().split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await update.message.reply_text(
            "❌ Send exactly two integers: <code>buyer_bps admin_bps</code>\nExample: <code>150 280</code>",
            parse_mode="HTML",
        )
        context.user_data["fee_group_id"] = group_id
        return
    buyer_bps, admin_bps = int(parts[0]), int(parts[1])
    if not (0 <= buyer_bps <= 2000 and 0 <= admin_bps <= 2000):
        await update.message.reply_text("❌ Values must be 0–2000 bps each.")
        context.user_data["fee_group_id"] = group_id
        return
    await update.message.reply_text(
        f"⚠️ <b>Confirm fee override for Group {group_id}</b>\n\n"
        f"Buyer: <b>{buyer_bps} bps ({buyer_bps / 100:.2f}%)</b>\n"
        f"Admin: <b>{admin_bps} bps ({admin_bps / 100:.2f}%)</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"sa_fees_confirm_{group_id}_{buyer_bps}_{admin_bps}"),
            InlineKeyboardButton("❌ Cancel",  callback_data=f"sa_group_{group_id}"),
        ]]),
    )

async def cancelfees_cmd(update: Update, context):
    context.user_data.pop("fee_group_id", None)
    await update.message.reply_text("Fee override cancelled.")

# ── Update price text handler ─────────────────────────────────────────────────

async def sa_price_text_handler(update: Update, context):
    group_id = context.user_data.get("sa_price_group_id")
    if group_id is None:
        return
    raw = update.message.text.strip().lstrip("$")
    try:
        usd = float(raw)
        if usd <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Send a positive USD amount, e.g. <code>9.99</code>.", parse_mode="HTML",
        )
        return
    cents = round(usd * 100)
    context.user_data.pop("sa_price_group_id", None)
    await update.message.reply_text(
        f"⚠️ <b>Confirm price update for Group {group_id}</b>\n\n"
        f"New price: <b>${cents / 100:.2f} USD</b> per billing cycle\n"
        "<i>Existing subscribers keep locked-in price until renewal.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"sa_price_confirm_{group_id}_{cents}"),
            InlineKeyboardButton("❌ Cancel",  callback_data=f"sa_group_{group_id}"),
        ]]),
    )

async def cancelprice_cmd(update: Update, context):
    context.user_data.pop("sa_price_group_id", None)
    context.user_data.pop("sa_price_pending_cents", None)
    await update.message.reply_text("Price update cancelled.")

# ── Global fee text handler ───────────────────────────────────────────────────

async def global_fee_text_handler(update: Update, context):
    context.user_data.pop("global_fee_pending", None)
    parts = update.message.text.strip().split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await update.message.reply_text(
            "❌ Send exactly two integers: <code>buyer_bps admin_bps</code>", parse_mode="HTML",
        )
        context.user_data["global_fee_pending"] = True
        return
    buyer_bps, admin_bps = int(parts[0]), int(parts[1])
    if not (0 <= buyer_bps <= 2000 and 0 <= admin_bps <= 2000):
        await update.message.reply_text("❌ Values must be 0–2000 bps each.")
        context.user_data["global_fee_pending"] = True
        return
    await update.message.reply_text(
        f"⚠️ <b>Confirm global default fee change</b>\n\n"
        f"Buyer: <b>{buyer_bps} bps ({buyer_bps / 100:.2f}%)</b>\n"
        f"Admin: <b>{admin_bps} bps ({admin_bps / 100:.2f}%)</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"sa_settings_globalfee_confirm_{buyer_bps}_{admin_bps}"),
            InlineKeyboardButton("❌ Cancel",  callback_data="sa_settings_menu"),
        ]]),
    )

async def cancelglobalfee_cmd(update: Update, context):
    context.user_data.pop("global_fee_pending", None)
    await update.message.reply_text("Global fee adjustment cancelled.")

# ── Multiplexed text handler ──────────────────────────────────────────────────

async def _reply_text_handler(update: Update, context):
    """Handles superadmin's typed reply to a support ticket."""
    target_id = context.user_data.pop("reply_target", None)
    if target_id is None:
        return
    reply_text = update.message.text.strip()
    actor      = update.effective_user.id
    sent       = False
    if BOT_TOKEN:
        from telegram import Bot
        try:
            async with Bot(BOT_TOKEN) as bot:
                await bot.send_message(
                    chat_id=target_id,
                    text=(
                        f"💬 <b>Support Reply</b>\n\n"
                        f"{html.escape(reply_text)}"
                    ),
                    parse_mode="HTML",
                )
            sent = True
        except Exception as exc:
            log.warning("SA: failed to send support reply to user %d: %s", target_id, exc)
    from renewise.db.queries import audit
    await audit(None, "support_reply_sent", actor,
                {"target_user_telegram_id": target_id, "sent": sent, "preview": reply_text[:80]})
    await update.message.reply_text(
        f"✅ Reply sent to user <code>{target_id}</code>." if sent
        else f"⚠️ Could not deliver reply to user <code>{target_id}</code>.",
        parse_mode="HTML",
    )


async def _multiplex_text_handler(update: Update, context):
    """Single TEXT handler — delegates to whichever multi-step flow is active."""
    if "fee_group_id" in context.user_data:
        await fee_text_handler(update, context)
    elif "sa_price_group_id" in context.user_data:
        await sa_price_text_handler(update, context)
    elif "msg_admin_target" in context.user_data:
        await msgadmin_text_handler(update, context)
    elif "reply_target" in context.user_data:
        await _reply_text_handler(update, context)
    elif "global_fee_pending" in context.user_data:
        await global_fee_text_handler(update, context)
    elif "lookup_pending" in context.user_data:
        await lookup_text_handler(update, context)
    # else: no active flow — ignore

# ── Master callback handler ───────────────────────────────────────────────────

async def sa_callback_handler(update: Update, context):
    query   = update.callback_query
    data    = query.data
    user_id = update.effective_user.id

    # ── navigation ────────────────────────────────────────────────────────────
    if data == "sa_home_main":
        await show_main_menu(update, context)
        await query.answer()

    elif data == "sa_home_stats":
        await show_overview(update, context)
        await query.answer()

    elif data == "sa_ks_status":
        await show_killswitch_status(update, context)
        await query.answer()

    elif data in ("sa_trigger_status", "sa_trigger_refresh"):
        await show_trigger_status(update, context)
        await query.answer("Fetching live balance…" if data == "sa_trigger_refresh" else "")

    elif data == "sa_wc_pending":
        await show_pending_wallet_changes(update, context)
        await query.answer()

    elif data == "sa_lookup_prompt":
        context.user_data["lookup_pending"] = True
        kb = [[InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")]]
        await query.edit_message_text(
            "🔍 <b>Lookup</b>\n\nSend a TX hash or numeric Telegram user ID.",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb),
        )
        await query.answer()

    elif data == "sa_cancelmsg":
        context.user_data.pop("msg_admin_target", None)
        await query.edit_message_text(
            "Message cancelled.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")]]),
        )
        await query.answer()

    elif data == "sa_cancelreply":
        context.user_data.pop("reply_target", None)
        await query.edit_message_text(
            "Reply cancelled.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")]]),
        )
        await query.answer()

    # ── groups paging ─────────────────────────────────────────────────────────
    elif data.startswith("sa_page_"):
        await show_groups_page(update, context, int(data.split("_")[2]))
        await query.answer()

    elif data.startswith("sa_group_"):
        gid = int(data.split("_")[2])
        if not await _guard_group_exists(update, gid):
            return
        await show_group_details(update, context, gid)
        await query.answer()

    elif data.startswith("sa_pmthist_"):
        parts = data.split("_")
        gid = int(parts[2])
        if not await _guard_group_exists(update, gid):
            return
        await show_sa_payment_history(update, context, gid, int(parts[3]) if len(parts) > 3 else 0)
        await query.answer()

    # ── suspend / unsuspend ───────────────────────────────────────────────────
    elif data.startswith("sa_suspend_"):
        gid = int(data.split("_")[2])
        if not await _guard_group_exists(update, gid):
            return
        await set_group_status(gid, "suspended", user_id)
        await show_group_details(update, context, gid)
        await query.answer("Group suspended.", show_alert=True)

    elif data.startswith("sa_unsuspend_"):
        gid = int(data.split("_")[2])
        if not await _guard_group_exists(update, gid):
            return
        await set_group_status(gid, "active", user_id)
        await show_group_details(update, context, gid)
        await query.answer("Group unsuspended.", show_alert=True)

    # ── users paging ──────────────────────────────────────────────────────────
    elif data.startswith("sa_upage_"):
        await show_users_page(update, context, int(data.split("_")[2]))
        await query.answer()

    elif data.startswith("sa_u_"):
        await perform_lookup(update, context, data.split("_")[2])
        await query.answer()

    # ── refunds ───────────────────────────────────────────────────────────────
    elif data.startswith("sa_refunds_"):
        await show_refunds_page(update, context, int(data.split("_")[2]))
        await query.answer()

    elif data.startswith("sa_refund_cancel_"):
        refund_id = int(data.split("_")[3])
        if not await _guard_refund_exists(update, refund_id):
            return
        from renewise.db.queries import mark_refund_cancelled, audit
        await mark_refund_cancelled(refund_id)
        await audit(None, "refund_cancelled_by_superadmin", user_id, {"refund_id": refund_id})
        await query.answer("Refund cancelled.", show_alert=True)
        await show_refunds_page(update, context, 0)

    # ── audit log ─────────────────────────────────────────────────────────────
    elif data.startswith("sa_audit_"):
        await show_audit_page(update, context, int(data.split("_")[2]))
        await query.answer()

    # ── manual TX recheck ─────────────────────────────────────────────────────
    elif data.startswith("sa_r_"):
        tx_hash = data.split("_", 2)[2]
        await query.answer("Starting manual recheck…")
        await audit_manual_recheck(user_id, f"tx_hash={tx_hash}")
        vault_address = await get_vault_by_tx_hash(tx_hash)
        if not vault_address:
            await query.message.reply_text(f"❌ No vault address on record for <code>{html.escape(tx_hash)}</code>.", parse_mode="HTML")
            return
        tx = await fetch_single_transaction(tx_hash, vault_address)
        if not tx:
            await query.message.reply_text(f"❌ TX <code>{html.escape(tx_hash[:16])}…</code>: <b>not found</b> on chain.", parse_mode="HTML")
            return
        if await is_tx_processed(tx_hash):
            await query.message.reply_text(f"✅ TX <code>{html.escape(tx_hash[:16])}…</code>: <b>already processed</b>.", parse_mode="HTML")
            return
        amount_nano = extract_in_msg_value(tx)
        from renewise.watcher.inprocess_watcher import _process_payment_inprocess
        await _process_payment_inprocess(context.application, vault_address, tx_hash, amount_nano)
        status = await get_tx_processed_status(tx_hash)
        if status and status.get("sub_id"):
            await query.message.reply_text(f"✅ Recheck <code>{html.escape(tx_hash[:16])}…</code>: <b>verified</b> — sub activated.", parse_mode="HTML")
        elif status:
            await query.message.reply_text(f"❌ Recheck <code>{html.escape(tx_hash[:16])}…</code>: <b>failed</b> (insufficient amount?).", parse_mode="HTML")
        else:
            await query.message.reply_text(f"❌ Recheck <code>{html.escape(tx_hash[:16])}…</code>: <b>error</b> during processing.", parse_mode="HTML")

    # ── reply to support ticket (from support ticket button) ─────────────────
    elif data.startswith("sa_reply_"):
        target_user_id = int(data.split("_")[2])
        context.user_data["reply_target"] = target_user_id
        await query.edit_message_text(
            f"💬 <b>Reply to User <code>{target_user_id}</code></b>\n\n"
            "Type your reply below. It will be delivered to the user via the main bot.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="sa_cancelreply"),
                InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main"),
            ]]),
        )
        await query.answer()

    # ── message admin ─────────────────────────────────────────────────────────
    elif data.startswith("sa_msgadmin_"):
        admin_tg_id = int(data.split("_")[2])
        context.user_data["msg_admin_target"] = admin_tg_id
        await query.edit_message_text(
            f"✉️ <b>Message Admin {admin_tg_id}</b>\n\nType your message. Delivered via main bot.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="sa_cancelmsg"),
                InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main"),
            ]]),
        )
        await query.answer()

    # ── cancel fee / price ────────────────────────────────────────────────────
    elif data.startswith("sa_cancelfees_"):
        context.user_data.pop("fee_group_id", None)
        await show_group_details(update, context, int(data.split("_")[2]))
        await query.answer("Cancelled.")

    elif data.startswith("sa_cancelprice_"):
        context.user_data.pop("sa_price_group_id", None)
        context.user_data.pop("sa_price_pending_cents", None)
        await show_group_details(update, context, int(data.split("_")[2]))
        await query.answer("Cancelled.")

    # ── kill switch ───────────────────────────────────────────────────────────
    elif data.startswith("sa_ks_"):
        from renewise.db.queries import set_payments_paused, audit
        if data == "sa_ks_pause_prompt":
            await query.edit_message_text(
                "⚠️ <b>Confirm Pause</b>\n\nStops ALL new payment processing platform-wide.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Yes, pause",  callback_data="sa_ks_pause_confirm")],
                    [InlineKeyboardButton("◀️ Cancel",   callback_data="sa_ks_cancel"),
                     InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")],
                ]),
            )
            await query.answer()
        elif data == "sa_ks_resume_prompt":
            await query.edit_message_text(
                "⚠️ <b>Confirm Resume</b>\n\nResume normal payment processing platform-wide.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Yes, resume", callback_data="sa_ks_resume_confirm")],
                    [InlineKeyboardButton("◀️ Cancel",   callback_data="sa_ks_cancel"),
                     InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")],
                ]),
            )
            await query.answer()
        elif data == "sa_ks_pause_confirm":
            await set_payments_paused(True, user_id)
            await audit(None, "pause_payments", user_id)
            await query.answer("Payments Paused", show_alert=True)
            await show_killswitch_status(update, context)
        elif data == "sa_ks_resume_confirm":
            await set_payments_paused(False, user_id)
            await audit(None, "resume_payments", user_id)
            await query.answer("Payments Resumed", show_alert=True)
            await show_killswitch_status(update, context)
        elif data == "sa_ks_cancel":
            await show_killswitch_status(update, context)
            await query.answer()
        else:
            await query.answer()

    # ── settings ──────────────────────────────────────────────────────────────
    elif data.startswith("sa_settings_") or data.startswith("sa_ban_") or data.startswith("sa_unban_"):
        if data == "sa_settings_menu":
            await show_settings_menu(update, context)
            await query.answer()

        elif data == "sa_settings_globalfee":
            await show_global_fee_prompt(update, context)
            await query.answer()

        elif data == "sa_settings_ratefeed":
            await show_rate_feed_status(update, context)
            await query.answer()

        elif data == "sa_settings_ban_prompt":
            await show_ban_admin_page(update, context, 0)
            await query.answer()

        elif data.startswith("sa_ban_page_"):
            await show_ban_admin_page(update, context, int(data.split("_")[3]))
            await query.answer()

        elif data.startswith("sa_ban_select_"):
            await show_ban_reason_prompt(update, context, int(data.split("_")[3]))
            await query.answer()

        elif data == "sa_settings_unban_prompt":
            await show_unban_admin_page(update, context, 0)
            await query.answer()

        elif data.startswith("sa_unban_page_"):
            await show_unban_admin_page(update, context, int(data.split("_")[3]))
            await query.answer()

        elif data.startswith("sa_unban_select_"):
            target = int(data.split("_")[3])
            await query.edit_message_text(
                f"⚠️ <b>Confirm Unban Admin <code>{target}</code>?</b>\n"
                "Groups stay suspended until manually unsuspended.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Confirm", callback_data=f"sa_settings_unban_confirm_{target}"),
                        InlineKeyboardButton("❌ Cancel",  callback_data="sa_unban_page_0"),
                    ],
                    [InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")],
                ]),
            )
            await query.answer()

        elif data.startswith("sa_settings_ban_confirm_"):
            parts      = data.split("_", 5)
            target     = int(parts[4])
            code       = parts[5] if len(parts) > 5 else "other"
            reason_map = {"spam": "Spamming", "scam": "Fraud/Scam", "tos": "TOS Violation", "other": "Other"}
            reason     = reason_map.get(code, "Other")
            from renewise.db.queries import ban_admin, audit
            await ban_admin(target, reason, user_id)
            await audit(None, "ban_admin", user_id, {"target_telegram_id": target, "reason": reason})
            await query.answer("Admin banned.", show_alert=True)
            await show_settings_menu(update, context)

        elif data.startswith("sa_settings_unban_confirm_"):
            target = int(data.split("_")[4])
            from renewise.db.queries import unban_admin, audit
            await unban_admin(target)
            await audit(None, "unban_admin", user_id, {"target_telegram_id": target})
            await query.answer("Admin unbanned.", show_alert=True)
            await show_settings_menu(update, context)

        elif data == "sa_settings_globalfee_reset_prompt":
            await query.edit_message_text(
                f"⚠️ Reset global fees to .env defaults?\n"
                f"Buyer: <b>{BUYER_FEE_BPS} bps</b> | Admin: <b>{ADMIN_FEE_BPS} bps</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Confirm", callback_data="sa_settings_globalfee_reset_confirm"),
                        InlineKeyboardButton("❌ Cancel",  callback_data="sa_settings_menu"),
                    ],
                    [InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")],
                ]),
            )
            await query.answer()

        elif data == "sa_settings_globalfee_reset_confirm":
            from renewise.db.queries import set_global_fees, audit
            await set_global_fees(BUYER_FEE_BPS, ADMIN_FEE_BPS, user_id)
            await audit(None, "reset_global_fee_to_default", user_id,
                        {"buyer_fee_bps": BUYER_FEE_BPS, "admin_fee_bps": ADMIN_FEE_BPS})
            await query.answer("Fees reset to default.", show_alert=True)
            await show_settings_menu(update, context)

        elif data.startswith("sa_settings_globalfee_confirm_"):
            parts     = data.split("_")
            buyer_bps = int(parts[4])
            admin_bps = int(parts[5])
            from renewise.db.queries import set_global_fees, audit
            await set_global_fees(buyer_bps, admin_bps, user_id)
            await audit(None, "update_global_fee", user_id,
                        {"buyer_fee_bps": buyer_bps, "admin_fee_bps": admin_bps})
            await query.answer("Global fees updated.", show_alert=True)
            await show_settings_menu(update, context)

        else:
            await query.answer()

    # ── fee override ──────────────────────────────────────────────────────────
    elif data.startswith("sa_fees_"):
        parts = data.split("_")
        gid = int(parts[2]) if parts[2].isdigit() else 0
        if gid and not await _guard_group_exists(update, gid):
            return
        if parts[2] == "confirm":
            gid       = int(parts[3])
            buyer_bps = int(parts[4])
            admin_bps = int(parts[5])
            await set_group_fees(gid, buyer_bps, admin_bps, user_id)
            await query.answer("Fees updated.", show_alert=True)
            await show_group_details(update, context, gid)
        else:
            gid  = int(parts[2])
            fees = await get_group_fee_config(gid)
            cur_buyer = fees["buyer_fee_bps"] if fees and fees["buyer_fee_bps"] is not None else BUYER_FEE_BPS
            cur_admin = fees["admin_fee_bps"] if fees and fees["admin_fee_bps"] is not None else ADMIN_FEE_BPS
            context.user_data["fee_group_id"] = gid
            await query.edit_message_text(
                f"⚙️ <b>Fee Override Group {gid}</b>\n\n"
                f"Current: buyer <b>{cur_buyer} bps</b> | admin <b>{cur_admin} bps</b>\n\n"
                "Reply with two integers: <code>buyer_bps admin_bps</code>  e.g. <code>150 280</code>"
                "\nRange: 0–2000 each.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data=f"sa_cancelfees_{gid}"),
                     InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")],
                ]),
            )
        await query.answer()

    # ── update price ──────────────────────────────────────────────────────────
    elif data.startswith("sa_price_"):
        parts = data.split("_")
        if len(parts) >= 3 and parts[2].isdigit():
            gid = int(parts[2])
            if not await _guard_group_exists(update, gid):
                return
        if len(parts) == 5 and parts[2] == "confirm":
            gid       = int(parts[3])
            new_cents = int(parts[4])
            from renewise.db.queries import update_group_price_usd_cents, audit
            await update_group_price_usd_cents(gid, new_cents)
            await audit(gid, "update_price", user_id,
                        {"new_price_usd_cents": new_cents, "acting_as": "superadmin"})
            await query.answer("Price updated.", show_alert=True)
            await show_group_details(update, context, gid)
        else:
            gid = int(parts[2])
            g   = await get_group_details(gid)
            cur = g["price_usd_cents"] if g else 0
            context.user_data["sa_price_group_id"] = gid
            await query.edit_message_text(
                f"💰 <b>Update Price — Group {gid}</b>\n\n"
                f"Current: <b>${(cur or 0) / 100:.2f} USD</b>\n\n"
                "Send new USD price, e.g. <code>9.99</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data=f"sa_cancelprice_{gid}"),
                     InlineKeyboardButton("◀️ Main Menu", callback_data="sa_home_main")],
                ]),
            )
        await query.answer()

# ── App builder + entry point ─────────────────────────────────────────────────

def build_app():
    """Build and return the configured superadmin Application."""
    if not SUPERADMIN_BOT_TOKEN:
        raise RuntimeError("SUPERADMIN_BOT_TOKEN is not set.")

    application = Application.builder().token(SUPERADMIN_BOT_TOKEN).build()

    # Access-control middleware runs before all handlers (group -1)
    application.add_handler(TypeHandler(Update, access_control), group=-1)

    # Commands
    application.add_handler(CommandHandler("start",          start_cmd))
    application.add_handler(CommandHandler("help",           help_cmd))
    application.add_handler(CommandHandler("overview",       overview_cmd))
    application.add_handler(CommandHandler("groups",         groups_cmd))
    application.add_handler(CommandHandler("killswitch",     killswitch_cmd))
    application.add_handler(CommandHandler("lookup",         lookup_cmd))
    application.add_handler(CommandHandler("msgadmin",       msgadmin_cmd))
    application.add_handler(CommandHandler("pendingrefunds", pending_refunds_cmd))
    application.add_handler(CommandHandler("auditlog",       auditlog_cmd))

    # All inline keyboard callbacks routed through single handler (pattern ^sa_)
    application.add_handler(CallbackQueryHandler(sa_callback_handler, pattern="^sa_"))

    # Multiplexed text handler for multi-step flows (fee override, price, msg, lookup)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _multiplex_text_handler))

    return application


def main():
    if not SUPERADMIN_BOT_TOKEN:
        log.error("SUPERADMIN_BOT_TOKEN is not set.")
        return
    app = build_app()
    log.info("Starting superadmin bot…")
    app.run_polling()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()

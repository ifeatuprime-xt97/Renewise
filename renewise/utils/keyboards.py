from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from renewise.config import SUPPORT_USERNAME


def _support_url() -> str:
    """Return a t.me deep-link to the support account, or empty string if not configured."""
    return f"https://t.me/{SUPPORT_USERNAME}" if SUPPORT_USERNAME else ""

def start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠️ Set Up a Paywall", callback_data="start:create_paywall")],
        [InlineKeyboardButton("📋 My Networks", callback_data="start:my_groups")],
        [InlineKeyboardButton("❓ How This Works", callback_data="start:how_it_works")],
    ])

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Stats", callback_data="menu:stats"),
            InlineKeyboardButton("💰 Update Price", callback_data="menu:update_price"),
        ],
        [
            InlineKeyboardButton("👛 Update Wallet", callback_data="menu:update_wallet"),
            InlineKeyboardButton("⏸ Pause/Resume", callback_data="menu:pause"),
        ],
        [
            InlineKeyboardButton("🎁 Comp a Member", callback_data="menu:comp"),
            InlineKeyboardButton("👥 View Members", callback_data="menu:members"),
        ],
        [
            InlineKeyboardButton("💳 Payment History", callback_data="menu:payment_history"),
        ],
        [
            InlineKeyboardButton("📞 Contact Support", callback_data="menu:support"),
        ],
        [
            InlineKeyboardButton("◀️ Back to Bot Menu", callback_data="start:back")
        ],
    ])


def confirm_paywall_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Go Live", callback_data="wizard:confirm")],
        [InlineKeyboardButton("✏️ Edit", callback_data="wizard:edit")],
        [InlineKeyboardButton("❌ Cancel", callback_data="wizard:cancel")],
    ])


def post_activate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 View Stats", callback_data="menu:stats"),
            InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings"),
        ],
    ])


def billing_interval_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Weekly (7d)", callback_data="interval:7"),
            InlineKeyboardButton("Monthly (30d)", callback_data="interval:30"),
        ],
        [InlineKeyboardButton("Custom…", callback_data="interval:custom")],
    ])


def pay_now_kb() -> InlineKeyboardMarkup:
    """Initial 'Pay Now' button shown in the welcome message."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Pay Now", callback_data="join:pay_now")],
    ])


def payment_details_kb(payment_url: str, support_url: str = "") -> InlineKeyboardMarkup:
    """
    Payment keyboard shown alongside the QR code.

    Four wallet options arranged compactly, then I've Paid / Contact Support / Cancel.

    All wallet buttons use the same ton:// link — the OS routes it to whichever
    GRAM wallet the user has installed as their default handler for the ton:// scheme.
    We label them individually so users recognise their wallet name, but the URL
    is identical for all four (ton:// is the universal deep-link standard).

    Row 1: Telegram Wallet | Tonkeeper
    Row 2: MyTonWallet     | TonHub
    Row 3: ✅ I've Paid
    Row 4: 💬 Contact Support  (only shown when support_url is set)
    Row 5: ❌ Cancel
    """
    rows = [
        [
            InlineKeyboardButton("📲 Telegram Wallet", url=payment_url),
            InlineKeyboardButton("💎 Tonkeeper",        url=payment_url),
        ],
        [
            InlineKeyboardButton("🔷 MyTonWallet",     url=payment_url),
            InlineKeyboardButton("🔹 TonHub",           url=payment_url),
        ],
        [InlineKeyboardButton("✅ I've Paid",           callback_data="join:paid")],
    ]
    if support_url:
        rows.append([InlineKeyboardButton("💬 Contact Support", url=support_url)])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="join:cancel")])
    return InlineKeyboardMarkup(rows)


def join_flow_kb(payment_url: str) -> InlineKeyboardMarkup:
    """Legacy two-button keyboard (kept for backward compatibility)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Pay & Join", url=payment_url)],
        [InlineKeyboardButton("✅ I've Paid", callback_data="join:paid")],
    ])


def member_action_kb(user_id: int, group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎁 Comp", callback_data=f"member:comp:{user_id}:{group_id}"),
            InlineKeyboardButton("🚫 Kick", callback_data=f"member:kick:{user_id}:{group_id}"),
        ],
        [InlineKeyboardButton("◀️ Back", callback_data=f"grpsel:{group_id}:menu:members")],
    ])


def members_nav_kb(group_id: int, page: int, has_next: bool) -> InlineKeyboardMarkup:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"members:page:{group_id}:{page - 1}"))
    if has_next:
        row.append(InlineKeyboardButton("Next ▶️", callback_data=f"members:page:{group_id}:{page + 1}"))
    buttons = [row] if row else []
    buttons.append([InlineKeyboardButton("◀️ Back to Menu", callback_data=f"grpsel:{group_id}:menu:home")])
    return InlineKeyboardMarkup(buttons)


def payment_history_nav_kb(group_id: int, page: int, has_next: bool) -> InlineKeyboardMarkup:
    """Navigation keyboard for the payment history paginator."""
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"pmthist:page:{group_id}:{page - 1}"))
    if has_next:
        row.append(InlineKeyboardButton("Next ▶️", callback_data=f"pmthist:page:{group_id}:{page + 1}"))
    buttons = [row] if row else []
    buttons.append([InlineKeyboardButton("◀️ Back to Menu", callback_data=f"grpsel:{group_id}:menu:home")])
    return InlineKeyboardMarkup(buttons)


def confirm_price_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Apply", callback_data="price:confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="price:cancel"),
        ],
    ])


def network_select_kb(groups: list, action: str = "menu:home") -> InlineKeyboardMarkup:
    """One button per network the admin owns.

    groups: list of dicts with keys 'id', 'title'.
    action: the callback_data fragment dispatched after the admin picks a network
            (e.g. 'menu:home' to show the network detail screen).
    """
    buttons = [
        [InlineKeyboardButton(
            f"📌 {g['title']}",
            callback_data=f"grpsel:{g['id']}:{action}",
        )]
        for g in groups
    ]
    buttons.append([InlineKeyboardButton("📞 Contact Support", callback_data="menu:support")])
    buttons.append([InlineKeyboardButton("◀️ Back to Start",   callback_data="start:back")])
    return InlineKeyboardMarkup(buttons)


# Backward-compat alias — existing callers of group_select_kb still work.
group_select_kb = network_select_kb


def network_detail_kb(group_id: int) -> InlineKeyboardMarkup:
    """Full management keyboard scoped to a single network.

    All callback_data values use the grpsel:<id>:menu:<action> pattern so
    existing action handlers require no changes.
    """
    gid = group_id
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Stats",         callback_data=f"grpsel:{gid}:menu:stats"),
            InlineKeyboardButton("💰 Update Price",  callback_data=f"grpsel:{gid}:menu:update_price"),
        ],
        [
            InlineKeyboardButton("👛 Update Wallet", callback_data=f"grpsel:{gid}:menu:update_wallet"),
            InlineKeyboardButton("🔑 Passkey",       callback_data=f"grpsel:{gid}:menu:set_passkey"),
        ],
        [
            InlineKeyboardButton("⏸ Pause/Resume",  callback_data=f"grpsel:{gid}:menu:pause"),
            InlineKeyboardButton("🎁 Comp a Member", callback_data=f"grpsel:{gid}:menu:comp"),
        ],
        [
            InlineKeyboardButton("👥 View Members",  callback_data=f"grpsel:{gid}:menu:members"),
            InlineKeyboardButton("💳 Payment History", callback_data=f"grpsel:{gid}:menu:payment_history"),
        ],
        [
            InlineKeyboardButton("📜 Wallet History", callback_data=f"grpsel:{gid}:menu:wallet_history"),
        ],
        [
            InlineKeyboardButton("📞 Contact Support", callback_data="menu:support"),
        ],
        [
            InlineKeyboardButton("🗑️ Delete This Network", callback_data=f"grpsel:{gid}:menu:delete"),
        ],
        [
            InlineKeyboardButton("◀️ My Networks", callback_data="menu:back"),
        ],
    ])


def cancel_input_kb() -> InlineKeyboardMarkup:
    """A generic cancel button for when the bot is waiting for text input."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="input:cancel")]
    ])


def wallet_change_alert_kb(change_id: int) -> InlineKeyboardMarkup:
    """
    Sent to the admin immediately when a wallet change is submitted.
    The only action is cancellation no passkey or extra confirmation needed.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🛑 Cancel This Change",
            callback_data=f"wallet_change:cancel:{change_id}",
        )],
    ])


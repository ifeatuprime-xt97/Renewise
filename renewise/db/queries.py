from __future__ import annotations
from typing import Any
import bcrypt
from renewise.db.connection import _db, Row


# ── passkey helpers (shared by group and platform wallet protection) ──────────

def _hash_passcode(passcode: str) -> str:
    return bcrypt.hashpw(passcode.encode(), bcrypt.gensalt()).decode()

def _verify_passcode(passcode: str, hashed: str) -> bool:
    return bcrypt.checkpw(passcode.encode(), hashed.encode())


# ── groups ────────────────────────────────────────────────────────────────────

async def upsert_group(
    telegram_chat_id: int,
    admin_telegram_id: int,
    chat_type: str | None = None,
) -> int:
    async with _db() as db:
        if chat_type:
            row = await db.fetchrow(
                "INSERT INTO groups (telegram_chat_id, admin_telegram_id, chat_type) "
                "VALUES ($1,$2,$3) "
                "ON CONFLICT(telegram_chat_id) DO UPDATE SET "
                "admin_telegram_id=EXCLUDED.admin_telegram_id, "
                "chat_type=EXCLUDED.chat_type "
                "RETURNING id",
                telegram_chat_id, admin_telegram_id, chat_type,
            )
        else:
            row = await db.fetchrow(
                "INSERT INTO groups (telegram_chat_id, admin_telegram_id) VALUES ($1,$2) "
                "ON CONFLICT(telegram_chat_id) DO UPDATE SET "
                "admin_telegram_id=EXCLUDED.admin_telegram_id "
                "RETURNING id",
                telegram_chat_id, admin_telegram_id,
            )
        return row["id"]


async def get_group_by_id(group_id: int) -> Row | None:
    async with _db() as db:
        return await db.fetchrow("SELECT * FROM groups WHERE id=$1", group_id)


async def get_group_by_chat_id(telegram_chat_id: int) -> Row | None:
    async with _db() as db:
        return await db.fetchrow(
            "SELECT * FROM groups WHERE telegram_chat_id=$1", telegram_chat_id
        )


async def get_groups_for_admin(admin_telegram_id: int) -> list[Row]:
    async with _db() as db:
        return await db.fetch(
            "SELECT * FROM groups WHERE admin_telegram_id=$1 AND status != 'frozen'",
            admin_telegram_id,
        )


async def activate_paywall(
    group_id: int,
    billing_interval_days: int,
    payout_wallet_address: str,
    chat_title: str | None = None,
    invite_link: str | None = None,
    chat_type: str | None = None,
) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE groups SET billing_interval_days=$1, "
            "payout_wallet_address=$2, status='active', "
            "chat_title=COALESCE($3, chat_title), "
            "invite_link=COALESCE($4, invite_link), "
            "chat_type=COALESCE($5, chat_type) "
            "WHERE id=$6",
            billing_interval_days, payout_wallet_address,
            chat_title, invite_link, chat_type, group_id,
        )


async def update_group_price(group_id: int, price: float) -> None:
    async with _db() as db:
        await db.execute("UPDATE groups SET price=$1 WHERE id=$2", price, group_id)


async def update_group_price_usd_cents(group_id: int, price_usd_cents: int) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE groups SET price_usd_cents=$1 WHERE id=$2", price_usd_cents, group_id
        )


async def update_group_wallet(group_id: int, wallet: str) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE groups SET payout_wallet_address=$1 WHERE id=$2", wallet, group_id
        )


async def set_group_passcode(
    group_id: int,
    new_passcode: str,
    actor_id: int,
    current_passcode: str | None = None,
) -> bool:
    """
    Set or change the 4-digit wallet passkey for a group.
    - First time: no current_passcode required.
    - Changing: current_passcode must match the stored bcrypt hash.
    Returns True on success, False if current_passcode is wrong.
    Raises ValueError if new_passcode is not exactly 4 digits.
    """
    if not (new_passcode.isdigit() and len(new_passcode) == 4):
        raise ValueError("Passkey must be exactly 4 digits")

    async with _db() as db:
        row = await db.fetchrow(
            "SELECT wallet_passcode_hash FROM groups WHERE id=$1", group_id
        )
        if not row:
            return False

        existing_hash = row["wallet_passcode_hash"]
        if existing_hash:
            if not current_passcode or not _verify_passcode(current_passcode, existing_hash):
                await audit(group_id, "group_passcode_change_failed", actor_id)
                return False

        new_hash = _hash_passcode(new_passcode)
        await db.execute(
            "UPDATE groups SET wallet_passcode_hash=$1 WHERE id=$2", new_hash, group_id
        )
        action = "group_passcode_changed" if existing_hash else "group_passcode_set"
        await audit(group_id, action, actor_id)
        return True


async def check_group_wallet_passcode(group_id: int, passcode: str | None) -> bool:
    """
    Return True if:
      - No passkey is set on the group (first-time wallet setup), OR
      - A passkey is set and passcode matches.
    Returns False if a passkey is set but passcode is wrong/missing.
    """
    async with _db() as db:
        row = await db.fetchrow(
            "SELECT wallet_passcode_hash FROM groups WHERE id=$1", group_id
        )
        if not row:
            return False
        existing_hash = row["wallet_passcode_hash"]
        if not existing_hash:
            return True   # no passkey set — first wallet change is always allowed
        return bool(passcode and _verify_passcode(passcode, existing_hash))


async def update_group_invite_link(group_id: int, invite_link: str) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE groups SET invite_link=$1 WHERE id=$2", invite_link, group_id
        )


async def get_admin_prior_wallet(admin_telegram_id: int) -> str | None:
    async with _db() as db:
        val = await db.fetchval(
            "SELECT payout_wallet_address FROM groups "
            "WHERE admin_telegram_id=$1 AND payout_wallet_address IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            admin_telegram_id,
        )
        return val


async def set_group_status(group_id: int, status: str) -> None:
    async with _db() as db:
        await db.execute("UPDATE groups SET status=$1 WHERE id=$2", status, group_id)


async def freeze_group(telegram_chat_id: int) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE groups SET status='frozen' WHERE telegram_chat_id=$1",
            telegram_chat_id,
        )
        await db.execute(
            "UPDATE subscriptions SET frozen_at=NOW() "
            "WHERE group_id=(SELECT id FROM groups WHERE telegram_chat_id=$1) "
            "AND status='active'",
            telegram_chat_id,
        )


# ── recent_admin_grants ───────────────────────────────────────────────────────

async def record_admin_grant(
    telegram_chat_id: int,
    chat_title: str,
    chat_type: str,
    from_user_id: int,
    can_invite_users: bool,
    can_manage_chat: bool,
    can_post_messages: bool,
) -> None:
    async with _db() as db:
        await db.execute(
            "INSERT INTO recent_admin_grants "
            "(telegram_chat_id, chat_title, chat_type, from_user_id, "
            " can_invite_users, can_manage_chat, can_post_messages) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            telegram_chat_id, chat_title, chat_type, from_user_id,
            int(can_invite_users), int(can_manage_chat), int(can_post_messages),
        )


async def get_recent_admin_grants(from_user_id: int, minutes: int = 15) -> list[Row]:
    from renewise.config import USE_POSTGRES
    async with _db() as db:
        if USE_POSTGRES:
            return await db.fetch(
                "SELECT * FROM recent_admin_grants "
                "WHERE from_user_id=$1 "
                "AND granted_at >= NOW() - ($2 || ' minutes')::INTERVAL "
                "ORDER BY granted_at DESC",
                from_user_id, str(minutes),
            )
        else:
            return await db.fetch(
                "SELECT * FROM recent_admin_grants "
                "WHERE from_user_id=$1 "
                "AND granted_at >= datetime('now', '-' || $2 || ' minutes') "
                "ORDER BY granted_at DESC",
                from_user_id, str(minutes),
            )


# ── users ─────────────────────────────────────────────────────────────────────

async def upsert_user(
    telegram_user_id: int,
    first_name: str | None = None,
    username: str | None = None,
) -> int:
    async with _db() as db:
        row = await db.fetchrow(
            "INSERT INTO users (telegram_user_id, first_name, username) VALUES ($1,$2,$3) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "first_name=COALESCE(EXCLUDED.first_name, users.first_name), "
            "username=COALESCE(EXCLUDED.username, users.username) "
            "RETURNING id",
            telegram_user_id, first_name, username,
        )
        return row["id"]


async def get_user_by_telegram_id(telegram_user_id: int) -> Row | None:
    async with _db() as db:
        return await db.fetchrow(
            "SELECT * FROM users WHERE telegram_user_id=$1", telegram_user_id
        )


# ── subscriptions ─────────────────────────────────────────────────────────────

async def get_payment_history(
    group_id: int, offset: int = 0, limit: int = 5
) -> tuple[list[Row], int]:
    async with _db() as db:
        rows = await db.fetch(
            "SELECT s.id, s.status, s.price_locked_in, s.start_date, "
            "s.next_renewal_date, s.last_payment_tx_hash, "
            "u.telegram_user_id, u.first_name, u.username "
            "FROM subscriptions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.group_id = $1 "
            "AND (s.status != 'pending' OR s.last_payment_tx_hash IS NOT NULL) "
            "ORDER BY COALESCE(s.start_date, s.created_at) DESC "
            "LIMIT $2 OFFSET $3",
            group_id, limit, offset,
        )
        total = await db.fetchval(
            "SELECT COUNT(*) FROM subscriptions s "
            "WHERE s.group_id = $1 "
            "AND (s.status != 'pending' OR s.last_payment_tx_hash IS NOT NULL)",
            group_id,
        ) or 0
    return rows, int(total)


async def get_payment_detail(sub_id: int) -> Row | None:
    async with _db() as db:
        return await db.fetchrow(
            "SELECT s.id, s.status, s.price_locked_in, s.start_date, "
            "s.next_renewal_date, s.last_payment_tx_hash, "
            "s.vault_address, s.required_nano_amount, s.amount_paid_so_far, "
            "s.created_at, s.updated_at, "
            "u.telegram_user_id, u.first_name, u.username, "
            "g.chat_title, g.billing_interval_days "
            "FROM subscriptions s "
            "JOIN users u ON u.id = s.user_id "
            "JOIN groups g ON g.id = s.group_id "
            "WHERE s.id = $1",
            sub_id,
        )


async def create_subscription(
    user_id: int,
    group_id: int,
    price_locked_in: float,
    vault_address: str | None = None,
) -> int:
    async with _db() as db:
        row = await db.fetchrow(
            "INSERT INTO subscriptions (user_id, group_id, status, price_locked_in, vault_address) "
            "VALUES ($1,$2,'pending',$3,$4) "
            "ON CONFLICT(user_id, group_id) DO UPDATE SET "
            "status='pending', "
            "vault_address=EXCLUDED.vault_address, "
            "price_locked_in=EXCLUDED.price_locked_in, "
            "last_payment_tx_hash=NULL, "
            "start_date=NULL, "
            "next_renewal_date=NULL, "
            "required_nano_amount=NULL, "
            "amount_paid_so_far=0, "
            "updated_at=NOW() "
            "RETURNING id",
            user_id, group_id, price_locked_in, vault_address,
        )
        if row:
            return row["id"]
        row2 = await db.fetchrow(
            "SELECT id FROM subscriptions WHERE user_id=$1 AND group_id=$2",
            user_id, group_id,
        )
        return row2["id"]


async def activate_subscription(
    user_id: int, group_id: int, tx_hash: str | None = None
) -> str:
    from renewise.config import USE_POSTGRES
    async with _db() as db:
        if USE_POSTGRES:
            await db.execute(
                "UPDATE subscriptions SET status='active', "
                "start_date=COALESCE(start_date, NOW()), "
                "next_renewal_date=COALESCE(next_renewal_date, NOW()) "
                "  + (SELECT billing_interval_days || ' days' FROM groups WHERE id=group_id)::INTERVAL, "
                "last_payment_tx_hash=$1, "
                "updated_at=NOW() "
                "WHERE user_id=$2 AND group_id=$3",
                tx_hash, user_id, group_id,
            )
        else:
            # SQLite: use datetime() string functions
            await db.execute(
                "UPDATE subscriptions SET status='active', "
                "start_date=COALESCE(start_date, CURRENT_TIMESTAMP), "
                "next_renewal_date=datetime("
                "  COALESCE(next_renewal_date, CURRENT_TIMESTAMP), "
                "  '+'||(SELECT billing_interval_days FROM groups WHERE id=group_id)||' days'), "
                "last_payment_tx_hash=$1, "
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE user_id=$2 AND group_id=$3",
                tx_hash, user_id, group_id,
            )
        row = await db.fetchrow(
            "SELECT next_renewal_date FROM subscriptions WHERE user_id=$1 AND group_id=$2",
            user_id, group_id,
        )
        return str(row["next_renewal_date"]) if row else ""


async def comp_subscription(user_id: int, group_id: int) -> None:
    async with _db() as db:
        await db.execute(
            "INSERT INTO subscriptions (user_id, group_id, status, price_locked_in, start_date) "
            "VALUES ($1,$2,'comped',0,NOW()) "
            "ON CONFLICT(user_id, group_id) DO UPDATE SET "
            "status='comped', price_locked_in=0, start_date=NOW()",
            user_id, group_id,
        )


async def set_subscription_required_amount(
    subscription_id: int, amount_nano: int
) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE subscriptions SET required_nano_amount=$1 WHERE id=$2",
            amount_nano, subscription_id,
        )


async def set_subscription_vault_address(
    subscription_id: int, vault_address: str
) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE subscriptions SET vault_address=$1 WHERE id=$2",
            vault_address, subscription_id,
        )


async def update_amount_paid_so_far(
    subscription_id: int, amount_nano: int
) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE subscriptions SET amount_paid_so_far=$1, updated_at=NOW() WHERE id=$2",
            amount_nano, subscription_id,
        )


async def get_subscription(user_id: int, group_id: int) -> Row | None:
    async with _db() as db:
        return await db.fetchrow(
            "SELECT * FROM subscriptions WHERE user_id=$1 AND group_id=$2",
            user_id, group_id,
        )


async def get_user_payment_history(
    telegram_user_id: int, offset: int = 0, limit: int = 20
) -> tuple[list[Row], int]:
    async with _db() as db:
        rows = await db.fetch(
            "SELECT s.id AS subscription_id, s.status AS subscription_status, "
            "s.price_locked_in, s.start_date, s.next_renewal_date, "
            "s.last_payment_tx_hash, s.required_nano_amount, "
            "g.id AS group_id, g.chat_title, g.price_usd_cents, g.billing_interval_days "
            "FROM subscriptions s "
            "JOIN users u ON u.id = s.user_id "
            "JOIN groups g ON g.id = s.group_id "
            "WHERE u.telegram_user_id = $1 "
            "AND (s.status != 'pending' OR s.last_payment_tx_hash IS NOT NULL) "
            "ORDER BY COALESCE(s.start_date, s.created_at) DESC "
            "LIMIT $2 OFFSET $3",
            telegram_user_id, limit, offset,
        )
        total = await db.fetchval(
            "SELECT COUNT(*) FROM subscriptions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE u.telegram_user_id = $1 "
            "AND (s.status != 'pending' OR s.last_payment_tx_hash IS NOT NULL)",
            telegram_user_id,
        ) or 0
    return rows, int(total)


async def get_user_subscriptions(telegram_user_id: int) -> list[Row]:
    async with _db() as db:
        return await db.fetch(
            "SELECT s.id AS subscription_id, s.status AS subscription_status, "
            "s.price_locked_in, s.start_date, s.next_renewal_date, "
            "g.id AS group_id, g.chat_title, "
            "g.price_usd_cents, g.billing_interval_days "
            "FROM subscriptions s "
            "JOIN users u ON u.id = s.user_id "
            "JOIN groups g ON g.id = s.group_id "
            "WHERE u.telegram_user_id = $1 "
            "ORDER BY s.next_renewal_date ASC",
            telegram_user_id,
        )


async def get_members_page(group_id: int, offset: int, limit: int) -> list[Row]:
    async with _db() as db:
        return await db.fetch(
            "SELECT s.*, u.telegram_user_id, u.first_name, u.username "
            "FROM subscriptions s "
            "JOIN users u ON u.id=s.user_id "
            "WHERE s.group_id=$1 AND s.status IN ('active','comped') "
            "ORDER BY s.created_at DESC LIMIT $2 OFFSET $3",
            group_id, limit, offset,
        )


async def count_members(group_id: int) -> int:
    async with _db() as db:
        val = await db.fetchval(
            "SELECT COUNT(*) FROM subscriptions "
            "WHERE group_id=$1 AND status IN ('active','comped')",
            group_id,
        )
        return int(val or 0)


async def delete_stale_pending_subscriptions(older_than_hours: int = 12) -> int:
    from renewise.config import USE_POSTGRES
    async with _db() as db:
        if USE_POSTGRES:
            rows = await db.fetch(
                "SELECT id FROM subscriptions "
                "WHERE status='pending' "
                "AND last_payment_tx_hash IS NULL "
                "AND created_at <= NOW() - ($1 || ' hours')::INTERVAL",
                str(older_than_hours),
            )
        else:
            rows = await db.fetch(
                "SELECT id FROM subscriptions "
                "WHERE status='pending' "
                "AND last_payment_tx_hash IS NULL "
                "AND created_at <= datetime('now', '-' || $1 || ' hours')",
                str(older_than_hours),
            )
        stale_ids = [r["id"] for r in rows]
        if not stale_ids:
            return 0
        ph = ",".join(f"${i+1}" for i in range(len(stale_ids)))
        await db.execute(f"DELETE FROM vault_registry WHERE subscription_id IN ({ph})", *stale_ids)
        await db.execute(f"DELETE FROM reminder_log WHERE subscription_id IN ({ph})", *stale_ids)
        await db.execute(f"DELETE FROM overpayment_refunds WHERE subscription_id IN ({ph})", *stale_ids)
        await db.execute(f"DELETE FROM subscriptions WHERE id IN ({ph})", *stale_ids)
        return len(stale_ids)


async def cancel_subscription(user_id: int, group_id: int) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE subscriptions SET status='cancelled' WHERE user_id=$1 AND group_id=$2",
            user_id, group_id,
        )


async def get_upcoming_renewals(group_id: int, limit: int = 5) -> list[Row]:
    async with _db() as db:
        return await db.fetch(
            "SELECT s.*, u.telegram_user_id FROM subscriptions s "
            "JOIN users u ON u.id=s.user_id "
            "WHERE s.group_id=$1 AND s.status='active' "
            "ORDER BY s.next_renewal_date ASC LIMIT $2",
            group_id, limit,
        )


async def get_subscription_by_vault(vault_address: str) -> Row | None:
    async with _db() as db:
        return await db.fetchrow(
            "SELECT s.*, g.billing_interval_days, u.telegram_user_id "
            "FROM subscriptions s "
            "JOIN groups g ON s.group_id = g.id "
            "JOIN users u ON s.user_id = u.id "
            "WHERE s.vault_address = $1",
            vault_address,
        )


async def get_vaults_to_watch() -> list[str]:
    async with _db() as db:
        rows = await db.fetch(
            "SELECT DISTINCT vr.vault_address "
            "FROM vault_registry vr "
            "JOIN subscriptions s ON s.id = vr.subscription_id "
            "WHERE s.status IN ('pending', 'active') "
            "UNION "
            "SELECT DISTINCT vault_address "
            "FROM subscriptions "
            "WHERE vault_address IS NOT NULL "
            "AND status IN ('pending', 'active') "
            "UNION "
            "SELECT DISTINCT vault_address "
            "FROM platform_charges "
            "WHERE vault_address IS NOT NULL "
            "AND status = 'pending'"
        )
        return [r["vault_address"] for r in rows]

async def get_platform_charge_by_vault(vault_address: str) -> Row | None:
    """
    Look up platform charge by vault address. Tries all friendly-address
    variants (EQ/UQ/kQ/0Q/raw) so the watcher can pass any canonical form.
    """
    variants = [vault_address]
    try:
        from pytoniq_core import Address as _Addr
        a = _Addr(vault_address)
        variants = list({
            vault_address,
            a.to_str(is_bounceable=True,  is_url_safe=True, is_test_only=False),
            a.to_str(is_bounceable=True,  is_url_safe=True, is_test_only=True),
            a.to_str(is_bounceable=False, is_url_safe=True, is_test_only=False),
            a.to_str(is_bounceable=False, is_url_safe=True, is_test_only=True),
            f"0:{a.hash_part.hex()}",
        })
    except Exception:
        pass

    async with _db() as db:
        for v in variants:
            charge = await db.fetchrow(
                "SELECT * FROM platform_charges WHERE vault_address = $1 AND status = 'pending'",
                v,
            )
            if charge:
                return charge
    return None


# ── processed_tx_hashes ───────────────────────────────────────────────────────

async def is_tx_processed(tx_hash: str) -> bool:
    """Return True only if the tx was *fully* processed (not a partial-payment record)."""
    async with _db() as db:
        val = await db.fetchval(
            "SELECT 1 FROM processed_tx_hashes WHERE tx_hash=$1 AND is_partial=0", tx_hash
        )
        return val is not None


# ── audit log ─────────────────────────────────────────────────────────────────

async def audit(
    group_id: int | None, action: str, actor_id: int, details: Any = None
) -> None:
    async with _db() as db:
        await db.execute(
            "INSERT INTO admin_audit_log (group_id, action, actor_telegram_id, details) "
            "VALUES ($1,$2,$3,$4)",
            group_id, action, actor_id, str(details) if details else None,
        )


# ── superadmin ────────────────────────────────────────────────────────────────

async def is_admin_suspended(admin_telegram_id: int) -> bool:
    async with _db() as db:
        val = await db.fetchval(
            "SELECT 1 FROM admin_suspensions WHERE admin_telegram_id=$1",
            admin_telegram_id,
        )
        return val is not None


async def suspend_admin(admin_telegram_id: int) -> None:
    async with _db() as db:
        await db.execute(
            "INSERT INTO admin_suspensions (admin_telegram_id) VALUES ($1) "
            "ON CONFLICT DO NOTHING",
            admin_telegram_id,
        )


async def unsuspend_admin(admin_telegram_id: int) -> None:
    async with _db() as db:
        await db.execute(
            "DELETE FROM admin_suspensions WHERE admin_telegram_id=$1",
            admin_telegram_id,
        )


# ── platform_config ───────────────────────────────────────────────────────────

async def is_payments_paused() -> bool:
    async with _db() as db:
        val = await db.fetchval(
            "SELECT payments_paused FROM platform_config WHERE id=1"
        )
        return bool(val)


async def set_payments_paused(paused: bool, admin_telegram_id: int) -> None:
    async with _db() as db:
        if paused:
            await db.execute(
                "UPDATE platform_config SET payments_paused=TRUE, "
                "paused_at=NOW(), paused_by=$1 WHERE id=1",
                admin_telegram_id,
            )
        else:
            await db.execute(
                "UPDATE platform_config SET payments_paused=FALSE, "
                "paused_at=NULL, paused_by=NULL WHERE id=1"
            )


# ── Global default fees ───────────────────────────────────────────────────────

async def get_global_fees() -> tuple[int, int]:
    import os
    env_buyer = int(os.getenv("BUYER_FEE_BPS", "200"))
    env_admin = int(os.getenv("ADMIN_FEE_BPS", "330"))
    async with _db() as db:
        row = await db.fetchrow(
            "SELECT global_buyer_fee_bps, global_admin_fee_bps "
            "FROM platform_config WHERE id=1"
        )
        if not row:
            return env_buyer, env_admin
        buyer = row["global_buyer_fee_bps"] if row["global_buyer_fee_bps"] is not None else env_buyer
        admin = row["global_admin_fee_bps"] if row["global_admin_fee_bps"] is not None else env_admin
        return int(buyer), int(admin)


async def set_global_fees(
    buyer_fee_bps: int, admin_fee_bps: int, actor_id: int
) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE platform_config "
            "SET global_buyer_fee_bps=$1, global_admin_fee_bps=$2 "
            "WHERE id=1",
            buyer_fee_bps, admin_fee_bps,
        )


# ── banned admins ─────────────────────────────────────────────────────────────

async def get_admin_ban_reason(telegram_id: int) -> str | None:
    async with _db() as db:
        return await db.fetchval(
            "SELECT reason FROM banned_admins WHERE telegram_id=$1", telegram_id
        )


async def ban_admin(telegram_id: int, reason: str, actor_id: int) -> None:
    async with _db() as db:
        await db.execute(
            "INSERT INTO banned_admins (telegram_id, banned_by, reason) VALUES ($1,$2,$3) "
            "ON CONFLICT DO NOTHING",
            telegram_id, actor_id, reason,
        )
        await db.execute(
            "UPDATE groups SET status='suspended' WHERE admin_telegram_id=$1",
            telegram_id,
        )


async def unban_admin(telegram_id: int) -> None:
    async with _db() as db:
        await db.execute(
            "DELETE FROM banned_admins WHERE telegram_id=$1", telegram_id
        )


# ── overpayment_refunds ───────────────────────────────────────────────────────

async def create_overpayment_refund(
    subscription_id: int,
    user_id: int,
    group_id: int,
    tx_hash: str,
    overpaid_nano: int,
    refund_nano: int,
    refund_usd: float,
) -> int:
    async with _db() as db:
        row = await db.fetchrow(
            "INSERT INTO overpayment_refunds "
            "(subscription_id, user_id, group_id, tx_hash, overpaid_nano, "
            " refund_nano, refund_usd, status) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,'pending_wallet') RETURNING id",
            subscription_id, user_id, group_id, tx_hash,
            overpaid_nano, refund_nano, refund_usd,
        )
        return row["id"]


async def get_pending_refund_for_user(telegram_user_id: int) -> Row | None:
    async with _db() as db:
        return await db.fetchrow(
            "SELECT r.*, u.telegram_user_id "
            "FROM overpayment_refunds r "
            "JOIN users u ON u.id = r.user_id "
            "WHERE u.telegram_user_id = $1 "
            "AND r.status = 'pending_wallet' "
            "ORDER BY r.created_at DESC LIMIT 1",
            telegram_user_id,
        )


async def set_refund_wallet(refund_id: int, wallet: str) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE overpayment_refunds "
            "SET refund_wallet=$1, status='pending_send' WHERE id=$2",
            wallet, refund_id,
        )


async def get_pending_sends(limit: int = 50) -> list[Row]:
    async with _db() as db:
        return await db.fetch(
            "SELECT r.*, u.telegram_user_id, g.chat_title "
            "FROM overpayment_refunds r "
            "JOIN users u ON u.id = r.user_id "
            "JOIN groups g ON g.id = r.group_id "
            "WHERE r.status = 'pending_send' "
            "ORDER BY r.created_at ASC LIMIT $1",
            limit,
        )


async def mark_refund_sent(refund_id: int) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE overpayment_refunds "
            "SET status='sent', resolved_at=NOW() WHERE id=$1",
            refund_id,
        )


async def mark_refund_cancelled(refund_id: int) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE overpayment_refunds "
            "SET status='cancelled', resolved_at=NOW() WHERE id=$1",
            refund_id,
        )


# ── terms of service ─────────────────────────────────────────────────────────

async def has_accepted_terms(telegram_user_id: int) -> bool:
    async with _db() as db:
        val = await db.fetchval(
            "SELECT terms_accepted_at FROM users WHERE telegram_user_id=$1",
            telegram_user_id,
        )
        return val is not None


async def accept_terms(telegram_user_id: int) -> None:
    async with _db() as db:
        await db.execute(
            "INSERT INTO users (telegram_user_id, terms_accepted_at) "
            "VALUES ($1, NOW()) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "terms_accepted_at = COALESCE(users.terms_accepted_at, NOW())",
            telegram_user_id,
        )


# ── pending_wallet_changes ────────────────────────────────────────────────────

async def create_pending_wallet_change(
    group_id: int,
    old_wallet: str | None,
    new_wallet: str,
    requested_by: int,
    activates_at: str,
) -> int:
    async with _db() as db:
        await db.execute(
            "UPDATE pending_wallet_changes "
            "SET status='cancelled', cancelled_at=NOW(), cancelled_by=$1 "
            "WHERE group_id=$2 AND status='pending'",
            requested_by, group_id,
        )
        row = await db.fetchrow(
            "INSERT INTO pending_wallet_changes "
            "(group_id, old_wallet_address, new_wallet_address, requested_by, activates_at) "
            "VALUES ($1,$2,$3,$4,$5) RETURNING id",
            group_id, old_wallet, new_wallet, requested_by, activates_at,
        )
        return row["id"]


async def get_pending_wallet_change(change_id: int) -> Row | None:
    async with _db() as db:
        return await db.fetchrow(
            "SELECT * FROM pending_wallet_changes WHERE id=$1", change_id
        )


async def get_superseded_pending_wallet_change(
    group_id: int, cancelled_by: int
) -> Row | None:
    async with _db() as db:
        return await db.fetchrow(
            "SELECT * FROM pending_wallet_changes "
            "WHERE group_id=$1 AND status='cancelled' AND cancelled_by=$2 "
            "ORDER BY cancelled_at DESC LIMIT 1",
            group_id, cancelled_by,
        )


async def cancel_pending_wallet_change(change_id: int, cancelled_by: int) -> bool:
    async with _db() as db:
        row = await db.fetchrow(
            "UPDATE pending_wallet_changes "
            "SET status='cancelled', cancelled_at=NOW(), cancelled_by=$1 "
            "WHERE id=$2 AND status='pending' "
            "RETURNING id",
            cancelled_by, change_id,
        )
        return row is not None


async def get_due_wallet_changes() -> list[Row]:
    async with _db() as db:
        return await db.fetch(
            "SELECT pwc.*, g.status AS group_status, g.admin_telegram_id, "
            "g.chat_title, g.payout_wallet_address AS current_wallet "
            "FROM pending_wallet_changes pwc "
            "JOIN groups g ON g.id = pwc.group_id "
            "WHERE pwc.status='pending' AND pwc.activates_at <= NOW()"
        )


async def apply_pending_wallet_change(
    change_id: int, new_wallet: str, group_id: int
) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE groups SET payout_wallet_address=$1 WHERE id=$2",
            new_wallet, group_id,
        )
        await db.execute(
            "UPDATE pending_wallet_changes SET status='applied' WHERE id=$1",
            change_id,
        )


async def get_wallet_change_history(group_id: int) -> list[Row]:
    async with _db() as db:
        return await db.fetch(
            "SELECT * FROM pending_wallet_changes "
            "WHERE group_id=$1 "
            "ORDER BY requested_at DESC, id DESC",
            group_id,
        )


async def get_active_subscribers(group_id: int) -> list[Row]:
    async with _db() as db:
        return await db.fetch(
            "SELECT u.telegram_user_id FROM subscriptions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.group_id=$1 AND s.status IN ('active','comped','pending')",
            group_id,
        )


async def delete_group(
    group_id: int,
    actor_id: int,
    group_title: str,
    telegram_chat_id: int,
) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE pending_wallet_changes "
            "SET status='cancelled', cancelled_at=NOW(), cancelled_by=$1 "
            "WHERE group_id=$2 AND status='pending'",
            actor_id, group_id,
        )
        await db.execute(
            "INSERT INTO admin_audit_log (group_id, action, actor_telegram_id, details) "
            "VALUES ($1,$2,$3,$4)",
            group_id, "group_deleted", actor_id,
            str({"group_title": group_title, "telegram_chat_id": telegram_chat_id}),
        )
        rows = await db.fetch(
            "SELECT id FROM subscriptions WHERE group_id=$1", group_id
        )
        sub_ids = [r["id"] for r in rows]
        if sub_ids:
            ph = ",".join(f"${i+1}" for i in range(len(sub_ids)))
            await db.execute(f"DELETE FROM vault_registry WHERE subscription_id IN ({ph})", *sub_ids)
            await db.execute(f"DELETE FROM reminder_log WHERE subscription_id IN ({ph})", *sub_ids)
            await db.execute(f"DELETE FROM overpayment_refunds WHERE subscription_id IN ({ph})", *sub_ids)
        await db.execute("DELETE FROM vault_registry WHERE group_id=$1", group_id)
        await db.execute("DELETE FROM overpayment_refunds WHERE group_id=$1", group_id)
        await db.execute("DELETE FROM subscriptions WHERE group_id=$1", group_id)
        await db.execute(
            "UPDATE admin_audit_log SET group_id=NULL WHERE group_id=$1", group_id
        )
        await db.execute("DELETE FROM groups WHERE id=$1", group_id)



# ── superadmin queries (inline — mirrors superadmin/queries.py simple ones) ───

async def get_groups_page(limit: int, offset: int) -> list[Row]:
    async with _db() as db:
        return await db.fetch(
            "SELECT * FROM groups ORDER BY id ASC LIMIT $1 OFFSET $2",
            limit, offset,
        )


async def get_total_groups_count() -> int:
    async with _db() as db:
        return int(await db.fetchval("SELECT COUNT(*) FROM groups") or 0)

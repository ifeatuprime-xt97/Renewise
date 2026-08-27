from __future__ import annotations
from renewise.db.connection import _db, Row


async def get_platform_overview() -> dict:
    from renewise.config import USE_POSTGRES
    async with _db() as db:
        active_admins = await db.fetchval(
            "SELECT COUNT(DISTINCT admin_telegram_id) FROM groups "
            "WHERE status = 'active' "
            "AND admin_telegram_id NOT IN (SELECT telegram_id FROM banned_admins) "
            "AND admin_telegram_id NOT IN (SELECT admin_telegram_id FROM admin_suspensions)"
        ) or 0
        active_subs = await db.fetchval(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'active'"
        ) or 0
        if USE_POSTGRES:
            monthly_gmv = await db.fetchval(
                "SELECT COALESCE(SUM(s.price_locked_in), 0) "
                "FROM processed_tx_hashes p "
                "JOIN subscriptions s ON s.id = p.sub_id "
                "WHERE DATE_TRUNC('month', p.processed_at) = DATE_TRUNC('month', NOW())"
            ) or 0.0
        else:
            monthly_gmv = await db.fetchval(
                "SELECT COALESCE(SUM(s.price_locked_in), 0) "
                "FROM processed_tx_hashes p "
                "JOIN subscriptions s ON s.id = p.sub_id "
                "WHERE strftime('%Y-%m', p.processed_at) = strftime('%Y-%m', 'now')"
            ) or 0.0
    return {
        "active_admins": int(active_admins),
        "active_subs":   int(active_subs),
        "monthly_gmv":   float(monthly_gmv),
    }


async def get_groups_page(limit: int, offset: int) -> list[dict]:
    async with _db() as db:
        rows = await db.fetch(
            "SELECT * FROM groups ORDER BY id ASC LIMIT $1 OFFSET $2", limit, offset
        )
        return [dict(r) for r in rows]


async def get_total_groups_count() -> int:
    async with _db() as db:
        return int(await db.fetchval("SELECT COUNT(*) FROM groups") or 0)


async def get_users_page(limit: int, offset: int) -> list[dict]:
    async with _db() as db:
        rows = await db.fetch(
            "SELECT * FROM users ORDER BY id DESC LIMIT $1 OFFSET $2", limit, offset
        )
        return [dict(r) for r in rows]


async def get_total_users_count() -> int:
    async with _db() as db:
        return int(await db.fetchval("SELECT COUNT(*) FROM users") or 0)


async def get_group_details(group_id: int) -> dict | None:
    async with _db() as db:
        row = await db.fetchrow("SELECT * FROM groups WHERE id=$1", group_id)
        if not row:
            return None
        group = dict(row)
        group["active_subs"] = int(await db.fetchval(
            "SELECT COUNT(*) FROM subscriptions WHERE group_id=$1 AND status='active'",
            group_id,
        ) or 0)
        group["total_revenue"] = float(await db.fetchval(
            "SELECT COALESCE(SUM(s.price_locked_in), 0) "
            "FROM processed_tx_hashes p "
            "JOIN subscriptions s ON s.id = p.sub_id "
            "WHERE s.group_id=$1",
            group_id,
        ) or 0.0)
        return group


async def set_group_status(group_id: int, status: str, admin_tg_id: int) -> None:
    async with _db() as db:
        await db.execute("UPDATE groups SET status=$1 WHERE id=$2", status, group_id)
        action = "suspend_group" if status == "suspended" else "unsuspend_group"
        await db.execute(
            "INSERT INTO admin_audit_log (group_id, action, actor_telegram_id) VALUES ($1,$2,$3)",
            group_id, action, admin_tg_id,
        )


async def search_by_tx_hash(tx_hash: str) -> dict | None:
    async with _db() as db:
        p_row = await db.fetchrow(
            "SELECT * FROM processed_tx_hashes WHERE tx_hash=$1", tx_hash
        )
        if p_row:
            sub_id = p_row["sub_id"]
            s_row = await db.fetchrow("SELECT * FROM subscriptions WHERE id=$1", sub_id) if sub_id else None
            return {
                "type": "processed",
                "processed_at": p_row["processed_at"],
                "subscription": dict(s_row) if s_row else None,
            }
        s_row = await db.fetchrow(
            "SELECT * FROM subscriptions WHERE last_payment_tx_hash=$1", tx_hash
        )
        if s_row:
            return {"type": "unprocessed", "subscription": dict(s_row)}
        return None


async def search_by_user_id(telegram_user_id: int) -> list[dict]:
    async with _db() as db:
        rows = await db.fetch(
            "SELECT s.*, g.telegram_chat_id "
            "FROM subscriptions s "
            "JOIN users u ON u.id = s.user_id "
            "JOIN groups g ON g.id = s.group_id "
            "WHERE u.telegram_user_id=$1",
            telegram_user_id,
        )
        return [dict(r) for r in rows]


async def get_groups_by_admin(admin_telegram_id: int) -> list[dict]:
    async with _db() as db:
        rows = await db.fetch(
            "SELECT g.*, "
            "(SELECT COUNT(*) FROM subscriptions s "
            " WHERE s.group_id = g.id AND s.status = 'active') AS active_subs "
            "FROM groups g WHERE g.admin_telegram_id = $1",
            admin_telegram_id,
        )
        return [dict(r) for r in rows]


async def get_vault_by_tx_hash(tx_hash: str) -> str | None:
    async with _db() as db:
        p_row = await db.fetchrow(
            "SELECT sub_id FROM processed_tx_hashes WHERE tx_hash=$1", tx_hash
        )
        if p_row and p_row["sub_id"]:
            s_row = await db.fetchrow(
                "SELECT vault_address FROM subscriptions WHERE id=$1", p_row["sub_id"]
            )
            if s_row:
                return s_row["vault_address"]
        s_row = await db.fetchrow(
            "SELECT vault_address FROM subscriptions WHERE last_payment_tx_hash=$1", tx_hash
        )
        return s_row["vault_address"] if s_row else None


async def get_tx_processed_status(tx_hash: str) -> dict | None:
    async with _db() as db:
        row = await db.fetchrow(
            "SELECT * FROM processed_tx_hashes WHERE tx_hash=$1", tx_hash
        )
        return dict(row) if row else None


async def audit_manual_recheck(admin_tg_id: int, details: str) -> None:
    async with _db() as db:
        await db.execute(
            "INSERT INTO admin_audit_log (action, actor_telegram_id, details) VALUES ($1,$2,$3)",
            "manual_recheck", admin_tg_id, details,
        )


# ── fee override ───────────────────────────────────────────────────────────────

async def set_group_fees(
    group_id: int, buyer_fee_bps: int, admin_fee_bps: int, actor_tg_id: int
) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE groups SET buyer_fee_bps=$1, admin_fee_bps=$2 WHERE id=$3",
            buyer_fee_bps, admin_fee_bps, group_id,
        )
        await db.execute(
            "INSERT INTO admin_audit_log (group_id, action, actor_telegram_id, details) "
            "VALUES ($1,$2,$3,$4)",
            group_id, "fee_override", actor_tg_id,
            f"buyer_fee_bps={buyer_fee_bps} admin_fee_bps={admin_fee_bps}",
        )


async def get_group_fee_config(group_id: int) -> dict | None:
    async with _db() as db:
        row = await db.fetchrow(
            "SELECT buyer_fee_bps, admin_fee_bps FROM groups WHERE id=$1", group_id
        )
        return dict(row) if row else None


# ── banned / active admins ────────────────────────────────────────────────────

async def get_active_admins_page(limit: int, offset: int) -> list[dict]:
    async with _db() as db:
        rows = await db.fetch(
            "SELECT DISTINCT admin_telegram_id FROM groups "
            "WHERE admin_telegram_id NOT IN (SELECT telegram_id FROM banned_admins) "
            "ORDER BY admin_telegram_id ASC LIMIT $1 OFFSET $2",
            limit, offset,
        )
        return [dict(r) for r in rows]


async def get_total_active_admins_count() -> int:
    async with _db() as db:
        return int(await db.fetchval(
            "SELECT COUNT(DISTINCT admin_telegram_id) FROM groups "
            "WHERE admin_telegram_id NOT IN (SELECT telegram_id FROM banned_admins)"
        ) or 0)


async def get_banned_admins_page(limit: int, offset: int) -> list[dict]:
    async with _db() as db:
        rows = await db.fetch(
            "SELECT telegram_id, reason, banned_at FROM banned_admins "
            "ORDER BY banned_at DESC LIMIT $1 OFFSET $2",
            limit, offset,
        )
        return [dict(r) for r in rows]


async def get_total_banned_admins_count() -> int:
    async with _db() as db:
        return int(await db.fetchval("SELECT COUNT(*) FROM banned_admins") or 0)


async def get_pending_send_refund_count() -> int:
    """Count refunds stuck in pending_send (trigger wallet needed but not sent yet)."""
    async with _db() as db:
        return int(await db.fetchval(
            "SELECT COUNT(*) FROM overpayment_refunds WHERE status='pending_send'"
        ) or 0)


async def get_trigger_wallet_balance(
    address: str,
    api_key: str = "",
    testnet: bool = False,
) -> float | None:
    """
    Fetch the live TON balance of the trigger wallet via TonCenter v2 API.

    Returns balance in TON (float), or None on any error / unconfigured state.
    Does NOT raise — designed to fail gracefully in a UI context.
    """
    if not address:
        return None
    import aiohttp
    base = "https://testnet.toncenter.com/api/v2" if testnet else "https://toncenter.com/api/v2"
    headers = {"X-API-Key": api_key} if api_key else {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{base}/getAddressBalance",
                params={"address": address},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                # result is nanotons (string)
                nano = int(data.get("result", 0) or 0)
                return nano / 1_000_000_000
    except Exception:
        return None


async def get_admin_detail(admin_telegram_id: int) -> dict:
    async with _db() as db:
        rows = await db.fetch(
            "SELECT g.*, "
            "(SELECT COUNT(*) FROM subscriptions s "
            " WHERE s.group_id = g.id AND s.status IN ('active','comped')) AS sub_count, "
            "(SELECT COALESCE(SUM(s.price_locked_in), 0) "
            " FROM subscriptions s "
            " WHERE s.group_id = g.id AND s.status IN ('active','comped')) AS revenue_usd "
            "FROM groups g WHERE g.admin_telegram_id = $1",
            admin_telegram_id,
        )
        groups = [dict(r) for r in rows]
        for g in groups:
            g["revenue_usd_cents"] = int(round(float(g.pop("revenue_usd", 0) or 0) * 100))
        total_subs    = sum(g["sub_count"]         for g in groups)
        total_revenue = sum(g["revenue_usd_cents"] for g in groups)
        return {
            "admin_telegram_id": admin_telegram_id,
            "groups":            groups,
            "total_subs":        total_subs,
            "total_revenue":     total_revenue,
        }

# ── platforms ─────────────────────────────────────────────────────────────────

async def get_platforms_page(limit: int, offset: int) -> list[dict]:
    async with _db() as db:
        rows = await db.fetch(
            "SELECT * FROM platforms ORDER BY created_at DESC LIMIT $1 OFFSET $2",
            limit, offset
        )
        return [dict(r) for r in rows]

async def get_total_platforms_count() -> int:
    async with _db() as db:
        return int(await db.fetchval("SELECT COUNT(*) FROM platforms") or 0)

async def get_platform_details(platform_id: int) -> dict | None:
    async with _db() as db:
        row = await db.fetchrow("SELECT * FROM platforms WHERE id = $1", platform_id)
        if not row:
            return None
        platform = dict(row)
        platform["total_charges"] = int(await db.fetchval(
            "SELECT COUNT(*) FROM platform_charges WHERE platform_id = $1", platform_id
        ) or 0)
        platform["revenue_usd"] = float(await db.fetchval(
            "SELECT COALESCE(SUM(amount_usd_cents), 0) FROM platform_charges "
            "WHERE platform_id = $1 AND status = 'completed'", platform_id
        ) or 0) / 100.0
        return platform

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


async def set_platform_fees(
    platform_id: int, buyer_fee_bps: int, admin_fee_bps: int, actor_tg_id: int
) -> None:
    """Set per-platform fee overrides. NULL means fall back to global defaults."""
    async with _db() as db:
        await db.execute(
            "UPDATE platforms SET buyer_fee_bps=$1, admin_fee_bps=$2 WHERE id=$3",
            buyer_fee_bps, admin_fee_bps, platform_id,
        )
        await db.execute(
            "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id, details) "
            "VALUES ($1,$2,$3,$4)",
            platform_id, "fee_override", actor_tg_id,
            f"buyer_fee_bps={buyer_fee_bps} admin_fee_bps={admin_fee_bps}",
        )


async def get_platform_fee_config(platform_id: int) -> dict | None:
    async with _db() as db:
        row = await db.fetchrow(
            "SELECT buyer_fee_bps, admin_fee_bps FROM platforms WHERE id=$1", platform_id
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
    Fetch the live GRAM balance of the trigger wallet via TonCenter v2 API.

    Returns balance in GRAM (float), or None on any error / unconfigured state.
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

async def get_user_detail(telegram_user_id: int) -> dict | None:
    """Full profile for a single user: base info + subscription summary."""
    async with _db() as db:
        user = await db.fetchrow(
            "SELECT * FROM users WHERE telegram_user_id=$1", telegram_user_id
        )
        if not user:
            return None
        user = dict(user)

        # All subscriptions with group title
        subs = await db.fetch(
            "SELECT s.id, s.status, s.price_locked_in, s.start_date, "
            "s.next_renewal_date, s.last_payment_tx_hash, "
            "g.id AS group_id, g.chat_title, g.telegram_chat_id "
            "FROM subscriptions s "
            "JOIN groups g ON g.id = s.group_id "
            "WHERE s.user_id = $1 "
            "ORDER BY s.created_at DESC",
            user["id"],
        )
        user["subscriptions"] = [dict(s) for s in subs]
        user["active_sub_count"]  = sum(1 for s in user["subscriptions"] if s["status"] == "active")
        user["total_sub_count"]   = len(user["subscriptions"])
        user["total_spent_usd"]   = sum(
            float(s["price_locked_in"] or 0)
            for s in user["subscriptions"]
            if s["status"] in ("active", "comped", "expired", "cancelled")
        )
        return user


async def get_all_user_ids() -> list[int]:
    """Return telegram_user_id for every user — used for broadcast."""
    async with _db() as db:
        rows = await db.fetch("SELECT telegram_user_id FROM users ORDER BY id ASC")
        return [r["telegram_user_id"] for r in rows]


# ── platform-wide TX feed ─────────────────────────────────────────────────────

async def get_platform_tx_feed(
    limit: int,
    offset: int,
    status_filter: str = "all",   # "all" | "active" | "expired" | "cancelled" | "pending"
) -> list[dict]:
    """
    Platform-wide transaction feed ordered by most recent first.

    UNIONs two sources:
      1. processed_tx_hashes → subscriptions → users → groups  (paywall payments)
      2. platform_charges → platforms → users (LEFT JOIN)        (developer API payments)

    Developer charge statuses are mapped to subscription equivalents so the
    existing filter tabs work without changes:
        completed → active   |  pending → pending
        expired   → expired  |  failed  → cancelled

    Columns returned (same shape for both legs):
      tx_hash, processed_at, sub_id, sub_status, price_locked_in,
      start_date, next_renewal_date, telegram_user_id, first_name,
      username, group_id, chat_title, admin_telegram_id
    """
    from renewise.config import USE_POSTGRES

    # SECURITY: status_filter is validated against an explicit allowlist — never
    # interpolated into SQL.  Anything outside the set silently falls back to "all".
    _ALLOWED = {"all", "active", "expired", "cancelled", "pending", "comped"}
    if status_filter not in _ALLOWED:
        status_filter = "all"

    # Map the subscription-side status filter to the equivalent platform_charges status.
    # "comped" has no developer equivalent — exclude developer charges when that tab is active.
    _DEV_STATUS_MAP = {
        "active":    "completed",
        "expired":   "expired",
        "cancelled": "failed",
        "pending":   "pending",
    }

    # ── subscription leg ──────────────────────────────────────────────────────
    # Identical SELECT list in both legs so UNION works regardless of DB.
    _sub_select = (
        "SELECT "
        "  p.tx_hash, p.processed_at, "
        "  s.id       AS sub_id, "
        "  s.status   AS sub_status, "
        "  s.price_locked_in, "
        "  s.start_date, "
        "  s.next_renewal_date, "
        "  u.telegram_user_id, "
        "  u.first_name, "
        "  u.username, "
        "  g.id       AS group_id, "
        "  g.chat_title, "
        "  g.admin_telegram_id "
        "FROM processed_tx_hashes p "
        "JOIN subscriptions s ON s.id = p.sub_id "
        "JOIN users u ON u.id = s.user_id "
        "JOIN groups g ON g.id = s.group_id"
    )

    # ── developer charges leg ─────────────────────────────────────────────────
    # Normalise platform_charges columns to match the subscription leg.
    # • price_locked_in  = amount_usd_cents / 100
    # • sub_status       = mapped charge status (completed→active etc.)
    # • chat_title       = '🔌 ' + platform_name  (prefix distinguishes dev charges)
    # • group_id         = platform_id  (reused for display; no real group)
    # • sub_id / start_date / next_renewal_date / admin_telegram_id = NULL
    # • first_name / username come from users table via LEFT JOIN (may be NULL)

    # CASE expression is identical for both SQLite and Postgres.
    _dev_status_expr = (
        "CASE pc.status "
        "  WHEN 'completed' THEN 'active' "
        "  WHEN 'expired'   THEN 'expired' "
        "  WHEN 'failed'    THEN 'cancelled' "
        "  ELSE pc.status "
        "END"
    )

    # SQLite uses REAL; Postgres uses DOUBLE PRECISION — both accept plain division.
    _price_cast = "CAST(pc.amount_usd_cents AS REAL)" if not USE_POSTGRES else "CAST(pc.amount_usd_cents AS DOUBLE PRECISION)"

    # Postgres requires subquery aliases; SQLite does too in UNION contexts.
    # SQLite does NOT support NULLS LAST — omit it (completed_at is always set
    # when tx_hash IS NOT NULL, so NULL ordering is not an issue in practice).
    _order_by = "ORDER BY processed_at DESC NULLS LAST" if USE_POSTGRES else "ORDER BY processed_at DESC"

    _dev_select = (
        "SELECT "
        "  pc.tx_hash, "
        "  pc.completed_at AS processed_at, "
        "  NULL            AS sub_id, "
        f" ({_dev_status_expr}) AS sub_status, "
        f" {_price_cast} / 100.0 AS price_locked_in, "
        "  NULL AS start_date, "
        "  NULL AS next_renewal_date, "
        "  pl.owner_telegram_id AS telegram_user_id, "
        "  u.first_name, "
        "  u.username, "
        "  pc.platform_id  AS group_id, "
        "  ('🔌 ' || pl.platform_name) AS chat_title, "
        "  NULL            AS admin_telegram_id "
        "FROM platform_charges pc "
        "JOIN platforms pl ON pl.id = pc.platform_id "
        "LEFT JOIN users u ON u.telegram_user_id = pl.owner_telegram_id "
        "WHERE pc.tx_hash IS NOT NULL"   # only charges that have reached on-chain
    )

    async with _db() as db:
        if status_filter == "all":
            rows = await db.fetch(
                f"SELECT * FROM ({_sub_select} UNION ALL {_dev_select}) AS combined "
                f"{_order_by} "
                "LIMIT $1 OFFSET $2",
                limit, offset,
            )
        elif status_filter == "comped":
            # "comped" is subscription-only — no developer charge equivalent
            rows = await db.fetch(
                f"{_sub_select} WHERE s.status = $1 "
                "ORDER BY p.processed_at DESC "
                "LIMIT $2 OFFSET $3",
                status_filter, limit, offset,
            )
        else:
            dev_status = _DEV_STATUS_MAP[status_filter]
            rows = await db.fetch(
                f"SELECT * FROM ("
                f"  {_sub_select} WHERE s.status = $1 "
                f"  UNION ALL "
                f"  {_dev_select} AND ({_dev_status_expr}) = $2"
                f") AS combined "
                f"{_order_by} "
                "LIMIT $3 OFFSET $4",
                status_filter, dev_status, limit, offset,
            )
        return [dict(r) for r in rows]


async def get_platform_tx_count(status_filter: str = "all") -> int:
    """Count all transactions across both paywall subscriptions and developer charges."""
    # SECURITY: same allowlist validation as get_platform_tx_feed above.
    _ALLOWED = {"all", "active", "expired", "cancelled", "pending", "comped"}
    if status_filter not in _ALLOWED:
        status_filter = "all"

    _DEV_STATUS_MAP = {
        "active":    "completed",
        "expired":   "expired",
        "cancelled": "failed",
        "pending":   "pending",
    }

    _dev_status_expr = (
        "CASE pc.status "
        "  WHEN 'completed' THEN 'active' "
        "  WHEN 'expired'   THEN 'expired' "
        "  WHEN 'failed'    THEN 'cancelled' "
        "  ELSE pc.status "
        "END"
    )

    async with _db() as db:
        if status_filter == "all":
            sub_count = int(await db.fetchval(
                "SELECT COUNT(*) FROM processed_tx_hashes p "
                "JOIN subscriptions s ON s.id = p.sub_id"
            ) or 0)
            dev_count = int(await db.fetchval(
                "SELECT COUNT(*) FROM platform_charges WHERE tx_hash IS NOT NULL"
            ) or 0)
            return sub_count + dev_count

        elif status_filter == "comped":
            # comped is subscription-only
            return int(await db.fetchval(
                "SELECT COUNT(*) FROM processed_tx_hashes p "
                "JOIN subscriptions s ON s.id = p.sub_id "
                "WHERE s.status = $1",
                status_filter,
            ) or 0)

        else:
            dev_status = _DEV_STATUS_MAP[status_filter]
            sub_count = int(await db.fetchval(
                "SELECT COUNT(*) FROM processed_tx_hashes p "
                "JOIN subscriptions s ON s.id = p.sub_id "
                "WHERE s.status = $1",
                status_filter,
            ) or 0)
            dev_count = int(await db.fetchval(
                f"SELECT COUNT(*) FROM platform_charges "
                f"WHERE tx_hash IS NOT NULL AND ({_dev_status_expr}) = $1",
                dev_status,
            ) or 0)
            return sub_count + dev_count


async def get_platform_revenue_breakdown() -> dict:
    """
    Per-status revenue + counts for the revenue breakdown screen.
    Also returns all-time totals and a simple month-over-month comparison.
    """
    from renewise.config import USE_POSTGRES
    async with _db() as db:
        # All-time per-status revenue
        rows = await db.fetch(
            "SELECT s.status, COUNT(*) AS cnt, "
            "COALESCE(SUM(s.price_locked_in), 0) AS revenue "
            "FROM subscriptions s "
            "WHERE s.status != 'pending' "
            "GROUP BY s.status"
        )
        by_status = {r["status"]: {"count": int(r["cnt"]), "revenue": float(r["revenue"])} for r in rows}

        # Monthly GMV — current vs previous
        if USE_POSTGRES:
            cur_gmv = float(await db.fetchval(
                "SELECT COALESCE(SUM(s.price_locked_in), 0) "
                "FROM processed_tx_hashes p JOIN subscriptions s ON s.id=p.sub_id "
                "WHERE DATE_TRUNC('month', p.processed_at) = DATE_TRUNC('month', NOW())"
            ) or 0)
            prev_gmv = float(await db.fetchval(
                "SELECT COALESCE(SUM(s.price_locked_in), 0) "
                "FROM processed_tx_hashes p JOIN subscriptions s ON s.id=p.sub_id "
                "WHERE DATE_TRUNC('month', p.processed_at) = "
                "DATE_TRUNC('month', NOW() - INTERVAL '1 month')"
            ) or 0)
        else:
            cur_gmv = float(await db.fetchval(
                "SELECT COALESCE(SUM(s.price_locked_in), 0) "
                "FROM processed_tx_hashes p JOIN subscriptions s ON s.id=p.sub_id "
                "WHERE strftime('%Y-%m', p.processed_at) = strftime('%Y-%m', 'now')"
            ) or 0)
            prev_gmv = float(await db.fetchval(
                "SELECT COALESCE(SUM(s.price_locked_in), 0) "
                "FROM processed_tx_hashes p JOIN subscriptions s ON s.id=p.sub_id "
                "WHERE strftime('%Y-%m', p.processed_at) = "
                "strftime('%Y-%m', date('now','-1 month'))"
            ) or 0)

        total_tx = int(await db.fetchval("SELECT COUNT(*) FROM processed_tx_hashes") or 0)
        total_users = int(await db.fetchval("SELECT COUNT(*) FROM users") or 0)
        total_groups = int(await db.fetchval("SELECT COUNT(*) FROM groups") or 0)
        active_groups = int(await db.fetchval(
            "SELECT COUNT(*) FROM groups WHERE status='active'"
        ) or 0)

    all_time_rev = sum(v["revenue"] for v in by_status.values())
    return {
        "by_status":    by_status,
        "all_time_rev": all_time_rev,
        "cur_gmv":      cur_gmv,
        "prev_gmv":     prev_gmv,
        "total_tx":     total_tx,
        "total_users":  total_users,
        "total_groups": total_groups,
        "active_groups": active_groups,
    }


async def get_admin_groups_detail(admin_telegram_id: int) -> dict:
    """
    Full picture of a single admin: all their groups with per-group
    active subs, total revenue, status, and the admin's ban/suspension state.
    """
    async with _db() as db:
        groups_rows = await db.fetch(
            "SELECT g.id, g.telegram_chat_id, g.chat_title, g.status, "
            "g.price_usd_cents, g.billing_interval_days, g.payout_wallet_address, "
            "g.created_at, "
            "(SELECT COUNT(*) FROM subscriptions s "
            " WHERE s.group_id=g.id AND s.status='active') AS active_subs, "
            "(SELECT COUNT(*) FROM subscriptions s "
            " WHERE s.group_id=g.id AND s.status='comped') AS comped_subs, "
            "(SELECT COALESCE(SUM(s.price_locked_in), 0) "
            " FROM processed_tx_hashes p "
            " JOIN subscriptions s ON s.id=p.sub_id "
            " WHERE s.group_id=g.id) AS total_revenue "
            "FROM groups g WHERE g.admin_telegram_id=$1 "
            "ORDER BY g.created_at DESC",
            admin_telegram_id,
        )
        groups = [dict(r) for r in groups_rows]

        is_banned = bool(await db.fetchval(
            "SELECT 1 FROM banned_admins WHERE telegram_id=$1", admin_telegram_id
        ))
        ban_reason = await db.fetchval(
            "SELECT reason FROM banned_admins WHERE telegram_id=$1", admin_telegram_id
        )
        is_suspended = bool(await db.fetchval(
            "SELECT 1 FROM admin_suspensions WHERE admin_telegram_id=$1", admin_telegram_id
        ))
        user_row = await db.fetchrow(
            "SELECT first_name, username FROM users WHERE telegram_user_id=$1",
            admin_telegram_id,
        )

    total_rev   = sum(float(g.get("total_revenue") or 0) for g in groups)
    total_active = sum(int(g.get("active_subs") or 0) for g in groups)
    return {
        "admin_telegram_id": admin_telegram_id,
        "first_name":   user_row["first_name"] if user_row else None,
        "username":     user_row["username"]   if user_row else None,
        "groups":       groups,
        "total_groups": len(groups),
        "total_active_subs": total_active,
        "total_revenue": total_rev,
        "is_banned":    is_banned,
        "ban_reason":   ban_reason,
        "is_suspended": is_suspended,
    }


async def force_cancel_subscription(sub_id: int, actor_tg_id: int) -> bool:
    """Cancel a subscription by its DB id. Returns True if a row was updated."""
    async with _db() as db:
        row = await db.fetchrow(
            "UPDATE subscriptions SET status='cancelled', updated_at=NOW() "
            "WHERE id=$1 AND status NOT IN ('cancelled','pending') "
            "RETURNING id, group_id",
            sub_id,
        )
        if not row:
            return False
        await db.execute(
            "INSERT INTO admin_audit_log (group_id, action, actor_telegram_id, details) "
            "VALUES ($1,$2,$3,$4)",
            row["group_id"], "force_cancel_subscription", actor_tg_id,
            f"sub_id={sub_id}",
        )
        return True


async def force_expire_subscription(sub_id: int, actor_tg_id: int) -> bool:
    """Expire a subscription immediately. Returns True if a row was updated."""
    async with _db() as db:
        row = await db.fetchrow(
            "UPDATE subscriptions SET status='expired', updated_at=NOW() "
            "WHERE id=$1 AND status='active' "
            "RETURNING id, group_id",
            sub_id,
        )
        if not row:
            return False
        await db.execute(
            "INSERT INTO admin_audit_log (group_id, action, actor_telegram_id, details) "
            "VALUES ($1,$2,$3,$4)",
            row["group_id"], "force_expire_subscription", actor_tg_id,
            f"sub_id={sub_id}",
        )
        return True


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


async def delete_platform(platform_id: int, actor_tg_id: int) -> bool:
    """
    Hard-delete a platform and all its related data:
    webhook_deliveries → webhook_endpoints → platform_charges → platform_audit_log → platforms.
    Returns True if a platform row was actually deleted.
    """
    async with _db() as db:
        exists = await db.fetchval(
            "SELECT 1 FROM platforms WHERE id = $1", platform_id
        )
        if not exists:
            return False

        # Cascade order: child tables first
        await db.execute(
            "DELETE FROM webhook_deliveries "
            "WHERE webhook_endpoint_id IN "
            "(SELECT id FROM webhook_endpoints WHERE platform_id = $1)",
            platform_id,
        )
        await db.execute(
            "DELETE FROM webhook_endpoints WHERE platform_id = $1", platform_id
        )
        await db.execute(
            "DELETE FROM platform_charges WHERE platform_id = $1", platform_id
        )
        await db.execute(
            "DELETE FROM platform_audit_log WHERE platform_id = $1", platform_id
        )
        await db.execute(
            "DELETE FROM platforms WHERE id = $1", platform_id
        )
        # Global audit log entry so the action is traceable
        await db.execute(
            "INSERT INTO admin_audit_log (action, actor_telegram_id, details) "
            "VALUES ($1, $2, $3)",
            "platform_deleted",
            actor_tg_id,
            f"platform_id={platform_id}",
        )
    return True

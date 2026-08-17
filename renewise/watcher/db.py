"""
renewise/watcher/db.py

Watcher-specific DB tables and query helpers.
Uses the unified _db() adapter so it works with both SQLite and PostgreSQL (Neon).

Tables
──────
vault_registry      — vault_address → (subscription_id, user_id, group_id)
processed_tx_hashes — idempotency set for payment processing
reminder_log        — tracks which (subscription_id, cycle) had a reminder sent
"""
from __future__ import annotations
from renewise.config import DATABASE_PATH, USE_POSTGRES
from renewise.db.connection import _db, Row

# ── DDL (SQLite — used only when DATABASE_URL is not set) ────────────────────

CREATE_VAULT_REGISTRY = """
CREATE TABLE IF NOT EXISTS vault_registry (
    vault_address   TEXT PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    user_id         INTEGER NOT NULL REFERENCES users(id),
    group_id        INTEGER NOT NULL REFERENCES groups(id),
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_PROCESSED_TX = """
CREATE TABLE IF NOT EXISTS processed_tx_hashes (
    tx_hash      TEXT PRIMARY KEY,
    sub_id       INTEGER,
    processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_REMINDER_LOG = """
CREATE TABLE IF NOT EXISTS reminder_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    renewal_cycle   TEXT NOT NULL,
    sent_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
    message_id      INTEGER,
    UNIQUE(subscription_id, renewal_cycle)
)
"""

CREATE_IDX_VAULT = """
CREATE INDEX IF NOT EXISTS idx_vault_registry_address
    ON vault_registry(vault_address)
"""

CREATE_IDX_SUB_RENEWAL = """
CREATE INDEX IF NOT EXISTS idx_sub_renewal
    ON subscriptions(next_renewal_date, status)
"""


async def migrate() -> None:
    """
    Idempotent startup migration.

    PostgreSQL path: tables are created by schema.py init_db() on first run.
                     We only run column-backfill ALTER TABLEs here.
    SQLite path:     creates tables if missing, then backfills new columns.
    """
    if USE_POSTGRES:
        # PostgreSQL: schema.py handles CREATE TABLE.
        # Just ensure new columns exist (safe to call even if they already do).
        async with _db() as db:
            for stmt in (
                "ALTER TABLE processed_tx_hashes ADD COLUMN IF NOT EXISTS sub_id INTEGER",
                "ALTER TABLE reminder_log ADD COLUMN IF NOT EXISTS message_id INTEGER",
            ):
                try:
                    await db.execute(stmt)
                except Exception:
                    pass  # column already exists — safe
        return

    # SQLite path
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        for stmt in (
            CREATE_VAULT_REGISTRY,
            CREATE_PROCESSED_TX,
            CREATE_REMINDER_LOG,
            CREATE_IDX_VAULT,
            CREATE_IDX_SUB_RENEWAL,
        ):
            await db.execute(stmt)

        cur = await db.execute("PRAGMA table_info(processed_tx_hashes)")
        cols = {row[1] for row in await cur.fetchall()}
        if "sub_id" not in cols:
            await db.execute("ALTER TABLE processed_tx_hashes ADD COLUMN sub_id INTEGER")

        cur = await db.execute("PRAGMA table_info(reminder_log)")
        rl_cols = {row[1] for row in await cur.fetchall()}
        if "message_id" not in rl_cols:
            await db.execute("ALTER TABLE reminder_log ADD COLUMN message_id INTEGER")

        await db.commit()


# ── Query helpers ─────────────────────────────────────────────────────────────

async def register_vault(
    vault_address: str,
    subscription_id: int,
    user_id: int,
    group_id: int,
) -> None:
    async with _db() as db:
        await db.execute(
            "INSERT INTO vault_registry "
            "(vault_address, subscription_id, user_id, group_id) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT DO NOTHING",
            vault_address, subscription_id, user_id, group_id,
        )


async def get_vault_registration(vault_address: str) -> Row | None:
    """
    Look up vault by address. Tries the given address plus bounceable/
    non-bounceable variants so UQ and EQ forms both resolve correctly.
    """
    variants = [vault_address]
    try:
        from pytoniq_core import Address as _Addr
        a = _Addr(vault_address)
        variants = list({
            vault_address,
            a.to_str(is_bounceable=True,  is_url_safe=True),
            a.to_str(is_bounceable=False, is_url_safe=True),
        })
    except Exception:
        pass

    async with _db() as db:
        for v in variants:
            row = await db.fetchrow(
                "SELECT * FROM vault_registry WHERE vault_address=$1", v
            )
            if row:
                return row
    return None


async def is_tx_processed(tx_hash: str) -> bool:
    async with _db() as db:
        val = await db.fetchval(
            "SELECT 1 FROM processed_tx_hashes WHERE tx_hash=$1", tx_hash
        )
        return val is not None


async def mark_tx_processed(tx_hash: str, sub_id: int | None = None) -> bool:
    """
    Atomically insert tx hash. Returns True on first insert, False if duplicate.
    PostgreSQL: uses INSERT … ON CONFLICT DO NOTHING + checking affected rows.
    SQLite:     uses INSERT OR IGNORE + rowcount.
    """
    if USE_POSTGRES:
        async with _db() as db:
            # asyncpg doesn't expose rowcount easily; use a CTE to detect insert
            row = await db.fetchrow(
                "WITH ins AS ("
                "  INSERT INTO processed_tx_hashes (tx_hash, sub_id) VALUES ($1,$2) "
                "  ON CONFLICT DO NOTHING RETURNING tx_hash"
                ") SELECT COUNT(*) AS n FROM ins",
                tx_hash, sub_id,
            )
            return bool(row and row["n"])
    else:
        import aiosqlite
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO processed_tx_hashes (tx_hash, sub_id) VALUES (?,?)",
                (tx_hash, sub_id),
            )
            await db.commit()
            return cur.rowcount == 1


async def has_reminder_been_sent(subscription_id: int, renewal_cycle: str) -> bool:
    async with _db() as db:
        val = await db.fetchval(
            "SELECT 1 FROM reminder_log WHERE subscription_id=$1 AND renewal_cycle=$2",
            subscription_id, renewal_cycle,
        )
        return val is not None


async def get_reminder_message_id(subscription_id: int, renewal_cycle: str) -> int | None:
    async with _db() as db:
        return await db.fetchval(
            "SELECT message_id FROM reminder_log "
            "WHERE subscription_id=$1 AND renewal_cycle=$2",
            subscription_id, renewal_cycle,
        )


async def record_reminder_sent(
    subscription_id: int, renewal_cycle: str, message_id: int | None = None
) -> None:
    async with _db() as db:
        if message_id is not None:
            await db.execute(
                "INSERT INTO reminder_log (subscription_id, renewal_cycle, message_id) "
                "VALUES ($1,$2,$3) "
                "ON CONFLICT(subscription_id, renewal_cycle) "
                "DO UPDATE SET message_id=EXCLUDED.message_id "
                "  WHERE EXCLUDED.message_id IS NOT NULL",
                subscription_id, renewal_cycle, message_id,
            )
        else:
            await db.execute(
                "INSERT INTO reminder_log (subscription_id, renewal_cycle) VALUES ($1,$2) "
                "ON CONFLICT DO NOTHING",
                subscription_id, renewal_cycle,
            )


async def get_subscriptions_due_reminder(window_days: int) -> list[Row]:
    async with _db() as db:
        if USE_POSTGRES:
            return await db.fetch(
                "SELECT s.id, s.user_id, s.group_id, s.next_renewal_date, "
                "u.telegram_user_id, g.telegram_chat_id, g.price, g.billing_interval_days "
                "FROM subscriptions s "
                "JOIN users  u ON u.id = s.user_id "
                "JOIN groups g ON g.id = s.group_id "
                "WHERE s.status = 'active' "
                "AND s.next_renewal_date IS NOT NULL "
                "AND s.next_renewal_date <= NOW() + ($1 || ' days')::INTERVAL "
                "AND s.next_renewal_date >  NOW() "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM reminder_log r "
                "  WHERE r.subscription_id = s.id "
                "  AND r.renewal_cycle = s.next_renewal_date::TEXT"
                ")",
                str(window_days),
            )
        else:
            return await db.fetch(
                "SELECT s.id, s.user_id, s.group_id, s.next_renewal_date, "
                "u.telegram_user_id, g.telegram_chat_id, g.price, g.billing_interval_days "
                "FROM subscriptions s "
                "JOIN users  u ON u.id = s.user_id "
                "JOIN groups g ON g.id = s.group_id "
                "WHERE s.status = 'active' "
                "AND s.next_renewal_date IS NOT NULL "
                "AND datetime(s.next_renewal_date) <= datetime('now', $1 || ' days') "
                "AND datetime(s.next_renewal_date) >  datetime('now') "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM reminder_log r "
                "  WHERE r.subscription_id = s.id "
                "  AND r.renewal_cycle = s.next_renewal_date"
                ")",
                str(window_days),
            )


async def get_subscriptions_past_grace(grace_days: int) -> list[Row]:
    async with _db() as db:
        if USE_POSTGRES:
            return await db.fetch(
                "SELECT s.id, s.user_id, s.group_id, "
                "u.telegram_user_id, g.telegram_chat_id, g.admin_telegram_id "
                "FROM subscriptions s "
                "JOIN users  u ON u.id = s.user_id "
                "JOIN groups g ON g.id = s.group_id "
                "WHERE s.status = 'active' "
                "AND s.next_renewal_date IS NOT NULL "
                "AND s.next_renewal_date + ($1 || ' days')::INTERVAL < NOW() "
                "AND u.telegram_user_id != g.admin_telegram_id",
                str(grace_days),
            )
        else:
            return await db.fetch(
                "SELECT s.id, s.user_id, s.group_id, "
                "u.telegram_user_id, g.telegram_chat_id, g.admin_telegram_id "
                "FROM subscriptions s "
                "JOIN users  u ON u.id = s.user_id "
                "JOIN groups g ON g.id = s.group_id "
                "WHERE s.status = 'active' "
                "AND s.next_renewal_date IS NOT NULL "
                "AND datetime(s.next_renewal_date, $1 || ' days') < datetime('now') "
                "AND u.telegram_user_id != g.admin_telegram_id",
                str(grace_days),
            )

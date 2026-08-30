"""
renewise/db/connection.py
─────────────────────────
Unified database connection layer.

When DATABASE_URL is set the app uses asyncpg (Neon / PostgreSQL).
When DATABASE_URL is blank the app falls back to aiosqlite (SQLite, local dev).

Public API — used throughout the codebase:

    from renewise.db.connection import _db, Row

    async with _db() as db:
        rows = await db.fetch("SELECT * FROM groups WHERE id = $1", group_id)
        row  = await db.fetchrow("SELECT * FROM users WHERE telegram_user_id = $1", uid)
        await db.execute("INSERT INTO ...", val1, val2)

The connection object returned by _db() always exposes:
    db.execute(sql, *args)          → None
    db.fetchrow(sql, *args)         → Row | None
    db.fetch(sql, *args)            → list[Row]
    db.fetchval(sql, *args)         → scalar | None

Row wraps both asyncpg.Record and aiosqlite.Row with uniform dict-style access:
    row["column_name"]   ✓ on both backends
    row[0]               ✓ on both backends
    dict(row)            ✓ on both backends

SQL placeholder convention
──────────────────────────
All SQL in queries.py and schema.py uses $1, $2, … placeholders.
On the SQLite backend these are transparently rewritten to ? before execution.
"""
from __future__ import annotations

import re
import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Sequence

from renewise.config import DATABASE_URL, DATABASE_PATH, USE_POSTGRES


# ── Row wrapper ───────────────────────────────────────────────────────────────

class Row:
    """
    Uniform dict-style row wrapper for both asyncpg.Record and aiosqlite.Row.

    asyncpg.Record already supports row["col"] but not dict(row) cleanly.
    aiosqlite.Row supports row["col"] and row[0] but not all dict methods.
    This wrapper normalises both to a consistent interface.
    """
    __slots__ = ("_data",)

    def __init__(self, raw: Any) -> None:
        # Convert to plain dict once so all access is O(1)
        if hasattr(raw, "_mapping"):          # asyncpg.Record
            self._data: dict = dict(raw)
        elif hasattr(raw, "keys"):            # aiosqlite.Row (sqlite3.Row)
            self._data = dict(zip(raw.keys(), tuple(raw)))
        elif isinstance(raw, dict):
            self._data = raw
        else:
            # Fallback: treat as sequence with no column names
            self._data = {i: v for i, v in enumerate(raw)}

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return list(self._data.values())[key]
        return self._data[key]

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"Row({self._data!r})"

    # Allow dict(row)
    def __class_getitem__(cls, item):
        return cls


def _wrap(raw: Any) -> Row | None:
    return Row(raw) if raw is not None else None

def _wrap_list(rows: Sequence[Any]) -> list[Row]:
    return [Row(r) for r in rows]


# ── SQL rewriting for SQLite ──────────────────────────────────────────────────

_PH = re.compile(r"\$(\d+)")
_NOW = re.compile(r"\bNOW\(\)", re.IGNORECASE)
_INTERVAL = re.compile(r"\|\|\s*'(\s*days?)'\s*\)::INTERVAL", re.IGNORECASE)
_INTERVAL2 = re.compile(r"\(\s*\$(\d+)\s*\|\|\s*'[^']*'\s*\)::INTERVAL", re.IGNORECASE)
_INTERVAL3 = re.compile(r"::INTERVAL", re.IGNORECASE)
_TRUE  = re.compile(r"\bTRUE\b",  re.IGNORECASE)
_FALSE = re.compile(r"\bFALSE\b", re.IGNORECASE)
_EXCLUDED = re.compile(r"\bEXCLUDED\b", re.IGNORECASE)

def _to_sqlite(sql: str) -> str:
    """Rewrite PostgreSQL idioms to SQLite equivalents."""
    sql = _PH.sub("?", sql)
    sql = _NOW.sub("CURRENT_TIMESTAMP", sql)
    # ($N || ' days')::INTERVAL → just the string value; SQLite datetime() handles it inline
    # The actual interval arithmetic is handled differently — see activate_subscription
    sql = _INTERVAL3.sub("", sql)
    sql = _TRUE.sub("1", sql)
    sql = _FALSE.sub("0", sql)
    sql = _EXCLUDED.sub("excluded", sql)
    # ON CONFLICT DO NOTHING
    sql = re.sub(r"ON CONFLICT DO NOTHING", "ON CONFLICT DO NOTHING", sql, flags=re.IGNORECASE)
    return sql


# ── PostgreSQL connection wrapper ─────────────────────────────────────────────

class _PgConn:
    """Thin wrapper around an asyncpg Connection exposing the unified API."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def execute(self, sql: str, *args: Any) -> None:
        await self._conn.execute(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> Row | None:
        return _wrap(await self._conn.fetchrow(sql, *args))

    async def fetch(self, sql: str, *args: Any) -> list[Row]:
        return _wrap_list(await self._conn.fetch(sql, *args))

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self._conn.fetchval(sql, *args)

    # Context manager passthrough (used by _db below)
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


# ── SQLite connection wrapper ─────────────────────────────────────────────────

class _SqConn:
    """Thin wrapper around an aiosqlite Connection exposing the unified API."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def execute(self, sql: str, *args: Any) -> None:
        await self._conn.execute(_to_sqlite(sql), args)
        await self._conn.commit()

    async def fetchrow(self, sql: str, *args: Any) -> Row | None:
        stripped = sql.strip().upper()
        cur = await self._conn.execute(_to_sqlite(sql), args)
        row = await cur.fetchone()
        # Commit after write statements (e.g. INSERT ... RETURNING id)
        if stripped.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            await self._conn.commit()
        return _wrap(row)

    async def fetch(self, sql: str, *args: Any) -> list[Row]:
        cur = await self._conn.execute(_to_sqlite(sql), args)
        rows = await cur.fetchall()
        return _wrap_list(rows)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        cur = await self._conn.execute(_to_sqlite(sql), args)
        row = await cur.fetchone()
        return row[0] if row else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


# ── asyncpg pool (module-level, initialised once on first use) ────────────────

_pg_pool: Any = None
_pg_lock = asyncio.Lock()


async def _get_pool() -> Any:
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    async with _pg_lock:
        if _pg_pool is None:
            import asyncpg
            _pg_pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=2,
                max_size=10,
                command_timeout=30,
                # Neon requires SSL; the connection string already includes
                # sslmode=require so no extra ssl= kwarg needed.
            )
    return _pg_pool


async def close_pool() -> None:
    """Call on app shutdown to cleanly drain the asyncpg pool."""
    global _pg_pool
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None


# ── Public context manager ────────────────────────────────────────────────────

@asynccontextmanager
async def _db() -> AsyncIterator[_PgConn | _SqConn]:
    """
    Yield a unified DB connection for one logical operation.

    PostgreSQL path: acquires a connection from the asyncpg pool,
    wraps it in a transaction, commits on clean exit, rolls back on error.

    SQLite path: opens a fresh aiosqlite connection with WAL + FK support.
    Each execute() commits immediately (matches existing behaviour).

    Usage (unchanged from existing codebase):

        async with _db() as db:
            row = await db.fetchrow("SELECT * FROM groups WHERE id = $1", gid)
    """
    if USE_POSTGRES:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                yield _PgConn(conn)
    else:
        import aiosqlite
        import os
        os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            yield _SqConn(conn)

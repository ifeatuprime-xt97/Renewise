"""
Mini App API tests for wallet change endpoints.

Tests:
  PUT  /api/groups/{id}/wallet          — schedules a change, not immediate
  POST /api/groups/{id}/wallet-change/{cid}/cancel — cancels it
  GET  /api/groups/{id}/wallet-change-history      — returns history

Uses httpx.AsyncClient with FastAPI's ASGI transport — no real server needed.
Auth is bypassed by overriding the get_telegram_user dependency.
DB is an in-memory SQLite instance shared per test via a lifespan override.
"""
from __future__ import annotations

import os
import pytest
import pytest_asyncio
import aiosqlite
import httpx
from contextlib import asynccontextmanager

os.environ.setdefault("BOT_TOKEN", "0:test")
os.environ.setdefault("DATABASE_PATH", ":memory:")

import renewise.config as _cfg
_cfg.DATABASE_PATH = ":memory:"

from renewise.db import queries
from renewise.db.schema import (
    CREATE_GROUPS, CREATE_USERS, CREATE_SUBSCRIPTIONS, CREATE_AUDIT_LOG,
    CREATE_PROCESSED_TX, CREATE_ADMIN_SUSPENSIONS, CREATE_PLATFORM_CONFIG,
    CREATE_RECENT_ADMIN_GRANTS, CREATE_BANNED_ADMINS, CREATE_OVERPAYMENT_REFUNDS,
    CREATE_PENDING_WALLET_CHANGES, CREATE_VAULT_REGISTRY, CREATE_REMINDER_LOG,
    CREATE_IDX_VAULT, CREATE_IDX_SUB_RENEWAL, CREATE_IDX_PENDING_WALLET,
)


# ── shared in-memory connection + query patch ─────────────────────────────────

@pytest_asyncio.fixture
async def conn():
    async with aiosqlite.connect(":memory:") as c:
        c.row_factory = aiosqlite.Row
        await c.execute("PRAGMA foreign_keys=ON")
        for stmt in (
            CREATE_GROUPS, CREATE_USERS, CREATE_SUBSCRIPTIONS, CREATE_AUDIT_LOG,
            CREATE_PROCESSED_TX, CREATE_ADMIN_SUSPENSIONS, CREATE_PLATFORM_CONFIG,
            CREATE_RECENT_ADMIN_GRANTS, CREATE_BANNED_ADMINS, CREATE_OVERPAYMENT_REFUNDS,
            CREATE_PENDING_WALLET_CHANGES, CREATE_VAULT_REGISTRY, CREATE_REMINDER_LOG,
            CREATE_IDX_VAULT, CREATE_IDX_SUB_RENEWAL, CREATE_IDX_PENDING_WALLET,
        ):
            await c.execute(stmt)
        await c.execute("INSERT INTO platform_config (id, payments_paused) VALUES (1, 0)")
        await c.commit()
        yield c


@pytest_asyncio.fixture
async def client(conn, monkeypatch):
    """
    httpx AsyncClient wired to the FastAPI app.

    - Patches queries._db to use the shared in-memory connection wrapped in _SqConn.
    - Overrides get_telegram_user to return a fixed admin user (id=999).
    - Skips the lifespan (init_db / cleanup tasks) — DB is already set up.
    """
    from renewise.db.connection import _SqConn

    # Wrap the raw aiosqlite connection in _SqConn so .fetchrow(), .fetch(),
    # .execute(), and .fetchval() all exist on the object the app receives.
    @asynccontextmanager
    async def _fake_db():
        yield _SqConn(conn)
    monkeypatch.setattr(queries, "_db", _fake_db)

    # Seed a group owned by admin 999
    cur = await conn.execute(
        "INSERT INTO groups (telegram_chat_id, admin_telegram_id, status, chat_title) "
        "VALUES (100, 999, 'active', 'Test Group') RETURNING id"
    )
    row = await cur.fetchone()
    await conn.commit()
    group_id = row[0]

    # Import app AFTER patching so the patched _db is used
    from renewise.miniapp.server import app, get_telegram_user
    from fastapi import FastAPI

    # Override auth dependency
    async def _fake_user():
        return {"id": 999, "first_name": "Admin"}

    app.dependency_overrides[get_telegram_user] = _fake_user

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c, group_id

    app.dependency_overrides.clear()


# ── helpers ───────────────────────────────────────────────────────────────────

# Valid mainnet bounceable (EQ) addresses — CRC16-verified, 48-char base64url.
# tag=0x11 (bounceable mainnet), workchain=0x00, deterministic 32-byte payload.
VALID_WALLET  = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAd99"
VALID_WALLET2 = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAu8e"


# ═════════════════════════════════════════════════════════════════════════════
# PUT /api/groups/{id}/wallet
# ═════════════════════════════════════════════════════════════════════════════

class TestPutWallet:

    @pytest.mark.asyncio
    async def test_returns_scheduled_not_applied(self, client):
        c, gid = client
        resp = await c.put(
            f"/api/groups/{gid}/wallet",
            json={"wallet_address": VALID_WALLET},
            headers={"Authorization": "tma dummy"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["scheduled"] is True
        assert "change_id" in data
        assert "activates_at" in data
        assert "hours" in data["message"].lower() or "hours" in str(data["delay_hours"])

    @pytest.mark.asyncio
    async def test_wallet_not_immediately_updated(self, client, conn):
        c, gid = client
        await c.put(
            f"/api/groups/{gid}/wallet",
            json={"wallet_address": VALID_WALLET},
            headers={"Authorization": "tma dummy"},
        )
        # groups.payout_wallet_address must NOT have changed yet
        cur = await conn.execute(
            "SELECT payout_wallet_address FROM groups WHERE id=?", (gid,)
        )
        row = await cur.fetchone()
        assert row["payout_wallet_address"] != VALID_WALLET

    @pytest.mark.asyncio
    async def test_invalid_wallet_rejected(self, client):
        c, gid = client
        resp = await c.put(
            f"/api/groups/{gid}/wallet",
            json={"wallet_address": "not-a-wallet"},
            headers={"Authorization": "tma dummy"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_second_submission_supersedes_first(self, client, conn):
        c, gid = client
        r1 = await c.put(
            f"/api/groups/{gid}/wallet",
            json={"wallet_address": VALID_WALLET},
            headers={"Authorization": "tma dummy"},
        )
        first_change_id = r1.json()["change_id"]

        wallet2 = VALID_WALLET2
        r2 = await c.put(
            f"/api/groups/{gid}/wallet",
            json={"wallet_address": wallet2},
            headers={"Authorization": "tma dummy"},
        )
        assert r2.status_code == 200

        # First change must now be cancelled
        cur = await conn.execute(
            "SELECT status FROM pending_wallet_changes WHERE id=?", (first_change_id,)
        )
        row = await cur.fetchone()
        assert row["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_unauthorized_group_rejected(self, client):
        c, _ = client
        resp = await c.put(
            "/api/groups/99999/wallet",
            json={"wallet_address": VALID_WALLET},
            headers={"Authorization": "tma dummy"},
        )
        assert resp.status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
# POST /api/groups/{id}/wallet-change/{cid}/cancel
# ═════════════════════════════════════════════════════════════════════════════

class TestCancelWalletChange:

    @pytest.mark.asyncio
    async def test_cancel_pending_succeeds(self, client, conn):
        c, gid = client
        r = await c.put(
            f"/api/groups/{gid}/wallet",
            json={"wallet_address": VALID_WALLET},
            headers={"Authorization": "tma dummy"},
        )
        change_id = r.json()["change_id"]

        resp = await c.post(
            f"/api/groups/{gid}/wallet-change/{change_id}/cancel",
            headers={"Authorization": "tma dummy"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "cancelled"

        cur = await conn.execute(
            "SELECT status FROM pending_wallet_changes WHERE id=?", (change_id,)
        )
        row = await cur.fetchone()
        assert row["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_already_cancelled_returns_409(self, client):
        c, gid = client
        r = await c.put(
            f"/api/groups/{gid}/wallet",
            json={"wallet_address": VALID_WALLET},
            headers={"Authorization": "tma dummy"},
        )
        change_id = r.json()["change_id"]

        await c.post(
            f"/api/groups/{gid}/wallet-change/{change_id}/cancel",
            headers={"Authorization": "tma dummy"},
        )
        resp = await c.post(
            f"/api/groups/{gid}/wallet-change/{change_id}/cancel",
            headers={"Authorization": "tma dummy"},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_404(self, client):
        c, gid = client
        resp = await c.post(
            f"/api/groups/{gid}/wallet-change/99999/cancel",
            headers={"Authorization": "tma dummy"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_cancel_wrong_group_returns_403(self, client, conn):
        """change_id belongs to gid but request uses a different group_id."""
        c, gid = client
        r = await c.put(
            f"/api/groups/{gid}/wallet",
            json={"wallet_address": VALID_WALLET},
            headers={"Authorization": "tma dummy"},
        )
        change_id = r.json()["change_id"]

        # Create a second group also owned by admin 999
        cur = await conn.execute(
            "INSERT INTO groups (telegram_chat_id, admin_telegram_id, status, chat_title) "
            "VALUES (200, 999, 'active', 'Other Group') RETURNING id"
        )
        row = await cur.fetchone()
        await conn.commit()
        other_gid = row[0]

        resp = await c.post(
            f"/api/groups/{other_gid}/wallet-change/{change_id}/cancel",
            headers={"Authorization": "tma dummy"},
        )
        assert resp.status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
# GET /api/groups/{id}/wallet-change-history
# ═════════════════════════════════════════════════════════════════════════════

class TestWalletChangeHistoryEndpoint:

    @pytest.mark.asyncio
    async def test_empty_history(self, client):
        c, gid = client
        resp = await c.get(
            f"/api/groups/{gid}/wallet-change-history",
            headers={"Authorization": "tma dummy"},
        )
        assert resp.status_code == 200
        assert resp.json()["wallet_change_history"] == []

    @pytest.mark.asyncio
    async def test_history_after_submit(self, client):
        c, gid = client
        await c.put(
            f"/api/groups/{gid}/wallet",
            json={"wallet_address": VALID_WALLET},
            headers={"Authorization": "tma dummy"},
        )
        resp = await c.get(
            f"/api/groups/{gid}/wallet-change-history",
            headers={"Authorization": "tma dummy"},
        )
        assert resp.status_code == 200
        history = resp.json()["wallet_change_history"]
        assert len(history) == 1
        assert history[0]["new_wallet_address"] == VALID_WALLET
        assert history[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_history_after_cancel(self, client):
        c, gid = client
        r = await c.put(
            f"/api/groups/{gid}/wallet",
            json={"wallet_address": VALID_WALLET},
            headers={"Authorization": "tma dummy"},
        )
        change_id = r.json()["change_id"]
        await c.post(
            f"/api/groups/{gid}/wallet-change/{change_id}/cancel",
            headers={"Authorization": "tma dummy"},
        )
        resp = await c.get(
            f"/api/groups/{gid}/wallet-change-history",
            headers={"Authorization": "tma dummy"},
        )
        history = resp.json()["wallet_change_history"]
        assert history[0]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_unauthorized_group_rejected(self, client):
        c, _ = client
        resp = await c.get(
            "/api/groups/99999/wallet-change-history",
            headers={"Authorization": "tma dummy"},
        )
        assert resp.status_code == 403

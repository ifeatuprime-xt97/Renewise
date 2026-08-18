"""
tests/test_miniapp_onboarding.py

Real pytest tests for:
  POST /api/groups/detect
  POST /api/groups/create

Each test proves BOTH:
  (a) correct behaviour for an authorized user
  (b) explicit rejection of an unauthorized user attempting the same action
      on a resource they don't own — attack-simulation parity with the
      original IDOR test standard.

Strategy
--------
Authentication: we generate real HMAC-signed initData using the test
BOT_TOKEN so get_telegram_user runs exactly as it does in production.

Database: each test gets a fresh per-test SQLite file via tmp_path.
schema.init_db() is called against it, and renewise.db.queries._db is
patched so all query functions use that file.

validate_ton_address: stubbed via monkeypatch to avoid network calls
on tests that need it.

Running
-------
    python -m pytest tests/test_miniapp_onboarding.py -v --asyncio-mode=auto
"""
from __future__ import annotations

import json
import os
import sys
import time
import hmac
import hashlib
import asyncio
from pathlib import Path
from urllib.parse import urlencode
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import pytest
import pytest_asyncio
import aiosqlite
from httpx import AsyncClient, ASGITransport

# ── ensure project root is importable ─────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set fake token BEFORE importing anything that reads it
FAKE_TOKEN = "123456:AAFakeTokenForTestingOnlyDoNotUse"
os.environ["BOT_TOKEN"] = FAKE_TOKEN

import renewise.config as _cfg
import renewise.db.connection as _conn
import renewise.db.schema as _schema
import renewise.db.queries as _queries
from renewise.db.schema import init_db

# Module-level TEST_DB_PATH — set per test by the autouse fixture
_TEST_DB_PATH: str = ""

# ── initData helpers ───────────────────────────────────────────────────────

def _make_init_data(user_id: int, token: str = FAKE_TOKEN) -> str:
    """
    Fabricate syntactically correct, HMAC-valid Telegram initData for user_id.
    verify_telegram_web_app_data will accept this because we sign with the
    same token the app uses.
    """
    user_json = json.dumps({"id": user_id, "first_name": "Test", "is_bot": False})
    auth_date = str(int(time.time()))
    payload = {"auth_date": auth_date, "user": user_json}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    sig = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    payload["hash"] = sig
    return urlencode(payload)


def _auth(user_id: int) -> dict:
    """Returns the Authorization header dict for user_id."""
    return {"authorization": f"tma {_make_init_data(user_id)}"}


# ── per-test DB fixture ────────────────────────────────────────────────────

@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(monkeypatch, tmp_path):
    """
    Gives every test an isolated SQLite DB with the full schema.

    schema.py and queries.py both bind DATABASE_PATH at import time via
    `from renewise.config import DATABASE_PATH`.  We must patch that local
    binding in EACH module, not just in renewise.config.
    """
    global _TEST_DB_PATH
    db_file = tmp_path / "renewise_test.db"
    _TEST_DB_PATH = str(db_file)

    # The project intentionally uses the configured DB path and mode.
    # Do not override the DB layer with a raw sqlite connection; that bypasses
    # the connection adapter and breaks calls that expect the unified _db() API.
    monkeypatch.setattr(_cfg,    "DATABASE_PATH", _TEST_DB_PATH)
    monkeypatch.setattr(_cfg,    "DATABASE_URL", "")
    monkeypatch.setattr(_cfg,    "USE_POSTGRES", False)
    monkeypatch.setattr(_conn,   "DATABASE_PATH", _TEST_DB_PATH)
    monkeypatch.setattr(_conn,   "DATABASE_URL", "")
    monkeypatch.setattr(_conn,   "USE_POSTGRES", False)
    monkeypatch.setattr(_schema, "DATABASE_PATH", _TEST_DB_PATH)
    monkeypatch.setattr(_conn,   "_pg_pool", None)

    # init_db reads the configured database settings and opens the SQLite DB
    # through the real connection wrapper, which is the behavior we want to test.
    await init_db()

    yield


@pytest.fixture
def patch_wallet_valid(monkeypatch):
    """Stub validate_ton_address → True (no network)."""
    async def _always_valid(addr: str) -> bool:
        return True
    monkeypatch.setattr("renewise.miniapp.server.validate_ton_address", _always_valid)


@pytest.fixture
def patch_wallet_invalid(monkeypatch):
    """Stub validate_ton_address → False."""
    async def _always_invalid(addr: str) -> bool:
        return False
    monkeypatch.setattr("renewise.miniapp.server.validate_ton_address", _always_invalid)


# ── single shared ASGI client ──────────────────────────────────────────────
# We don't use dependency_overrides; instead we generate correctly-signed
# initData so get_telegram_user runs exactly as it does in production.

@pytest_asyncio.fixture
async def http() -> AsyncGenerator[AsyncClient, None]:
    from renewise.miniapp.server import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── constants ──────────────────────────────────────────────────────────────

OWNER_ID    = 100001
ATTACKER_ID = 999999
CHAT_ID     = -1001234567890
VALID_WALLET = "EQD2NmD_lH5f5u1Kj3KfGyTvhZSX0Eg6qp2a5IQUKXxOG"

VALID_BODY = {
    "telegram_chat_id":      CHAT_ID,
    "price_usd_cents":       999,
    "billing_interval_days": 30,
    "wallet_address":        VALID_WALLET,
}


async def _seed_grant(
    user_id:   int  = OWNER_ID,
    chat_id:   int  = CHAT_ID,
    chat_type: str  = "group",
    title:     str  = "Test Group",
    can_invite: bool = True,
    can_manage: bool = True,
    can_post:   bool = True,
) -> None:
    await _queries.record_admin_grant(
        telegram_chat_id  = chat_id,
        chat_title        = title,
        chat_type         = chat_type,
        from_user_id      = user_id,
        can_invite_users  = can_invite,
        can_manage_chat   = can_manage,
        can_post_messages = can_post,
    )


@pytest.mark.asyncio
async def test_api_my_groups_refreshes_stale_group_title(monkeypatch, http):
    """If Telegram has a newer title than the DB cache, /api/my-groups should refresh it."""
    group_id = await _queries.upsert_group(CHAT_ID, OWNER_ID, "group")
    await _queries.activate_paywall(
        group_id=group_id,
        price=0,
        billing_interval_days=30,
        payout_wallet_address="EQD2NmD_lH5f5u1Kj3KfGyTvhZSX0Eg6qp2a5IQUKXxOG",
        chat_title="Old Group Title",
        invite_link=None,
        chat_type="group",
    )

    class FakeResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return {"ok": True, "result": {"title": "Fresh Group Title", "type": "supergroup"}}

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("aiohttp.ClientSession", FakeSession)

    resp = await http.get("/api/my-groups", headers=_auth(OWNER_ID))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"][0]["chat_title"] == "Fresh Group Title"
    assert body["groups"][0]["chat_type"] == "supergroup"


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/groups/detect
# ══════════════════════════════════════════════════════════════════════════════

class TestDetect:

    @pytest.mark.asyncio
    async def test_authorized_owner_sees_their_grant(self, http):
        """
        (a) Authorized: owner has a recent grant → detect returns it with
        permissions_ok=True.
        """
        await _seed_grant(OWNER_ID)

        resp = await http.post("/api/groups/detect", headers=_auth(OWNER_ID))

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["detected_chats"]) == 1
        chat = body["detected_chats"][0]
        assert chat["telegram_chat_id"] == CHAT_ID
        assert chat["chat_title"] == "Test Group"
        assert chat["permissions_ok"] is True
        assert chat["missing_permissions"] == []

    @pytest.mark.asyncio
    async def test_attacker_cannot_see_owners_grants(self, http):
        """
        (b) Attack simulation: OWNER has a grant; ATTACKER calls /detect.
        Must receive an empty list — cannot enumerate other users' chats.
        """
        await _seed_grant(OWNER_ID)   # owner's grant only

        resp = await http.post("/api/groups/detect", headers=_auth(ATTACKER_ID))

        assert resp.status_code == 200, resp.text
        assert resp.json()["detected_chats"] == []

    @pytest.mark.asyncio
    async def test_missing_permissions_flagged_for_channel(self, http):
        """
        Channel grant missing can_post_messages →
        permissions_ok=False, "Manage Messages" in missing list.
        """
        await _seed_grant(OWNER_ID, chat_type="channel", can_post=False)

        resp = await http.post("/api/groups/detect", headers=_auth(OWNER_ID))

        assert resp.status_code == 200
        chat = resp.json()["detected_chats"][0]
        assert chat["permissions_ok"] is False
        assert "Manage Messages" in chat["missing_permissions"]

    @pytest.mark.asyncio
    async def test_missing_can_manage_chat_flagged_for_group(self, http):
        """Group grant missing can_manage_chat → "Ban Users" in missing list."""
        await _seed_grant(OWNER_ID, chat_type="group", can_manage=False)

        resp = await http.post("/api/groups/detect", headers=_auth(OWNER_ID))

        assert resp.status_code == 200
        chat = resp.json()["detected_chats"][0]
        assert chat["permissions_ok"] is False
        assert "Ban Users" in chat["missing_permissions"]

    @pytest.mark.asyncio
    async def test_unauthenticated_request_rejected(self, http):
        """No Authorization header → 401 or 422."""
        resp = await http.post("/api/groups/detect")
        assert resp.status_code in (401, 422), resp.text

    @pytest.mark.asyncio
    async def test_banned_admin_rejected(self, http):
        """Banned admin gets 403 even if grant exists."""
        await _queries.ban_admin(OWNER_ID, "spam", actor_id=0)
        await _seed_grant(OWNER_ID)

        resp = await http.post("/api/groups/detect", headers=_auth(OWNER_ID))

        assert resp.status_code == 403
        assert "banned" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_suspended_admin_rejected(self, http):
        """Suspended admin gets 403."""
        await _queries.suspend_admin(OWNER_ID)
        await _seed_grant(OWNER_ID)

        resp = await http.post("/api/groups/detect", headers=_auth(OWNER_ID))

        assert resp.status_code == 403
        assert "suspended" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_multiple_grants_all_returned(self, http):
        """Owner made bot admin in two chats → both appear in detected_chats."""
        CHAT_ID_2 = -1009999999999
        await _seed_grant(OWNER_ID, chat_id=CHAT_ID,   title="Chat One")
        await _seed_grant(OWNER_ID, chat_id=CHAT_ID_2, title="Chat Two")

        resp = await http.post("/api/groups/detect", headers=_auth(OWNER_ID))

        assert resp.status_code == 200
        ids = {c["telegram_chat_id"] for c in resp.json()["detected_chats"]}
        assert CHAT_ID   in ids
        assert CHAT_ID_2 in ids


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/groups/create
# ══════════════════════════════════════════════════════════════════════════════

class TestCreate:

    @pytest.mark.asyncio
    async def test_authorized_owner_creates_group(self, http, patch_wallet_valid):
        """
        (a) Authorized: grant exists, wallet valid → group row created,
        audit log written, response has expected shape.
        """
        await _seed_grant(OWNER_ID)

        resp = await http.post(
            "/api/groups/create", json=VALID_BODY, headers=_auth(OWNER_ID)
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["group_id"] is not None
        assert body["chat_title"] == "Test Group"
        assert body["price_usd_cents"] == 999
        assert body["billing_interval_days"] == 30

        # DB state — real DB was written
        group = await _queries.get_group_by_id(body["group_id"])
        assert group is not None
        assert group["admin_telegram_id"] == OWNER_ID
        assert group["status"] == "active"
        assert group["price_usd_cents"] == 999
        assert group["billing_interval_days"] == 30
        assert group["payout_wallet_address"] == VALID_WALLET

        # Audit log was written with correct actor
        async with aiosqlite.connect(_TEST_DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM admin_audit_log WHERE action='paywall_created_via_miniapp'"
            )
            row = await cur.fetchone()
        assert row is not None
        assert row["actor_telegram_id"] == OWNER_ID
        assert row["group_id"] == body["group_id"]

    @pytest.mark.asyncio
    async def test_attacker_cannot_create_with_owners_grant(self, http, patch_wallet_valid):
        """
        (b) Attack simulation: OWNER has the admin grant; ATTACKER submits
        /create claiming the same chat_id. Must be 403 — no group written.
        """
        await _seed_grant(OWNER_ID)   # owner's grant, NOT attacker's

        resp = await http.post(
            "/api/groups/create", json=VALID_BODY, headers=_auth(ATTACKER_ID)
        )

        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"].lower()
        assert any(kw in detail for kw in ("grant", "admin", "not found", "no recent"))

        # Nothing written to DB
        assert await _queries.get_group_by_chat_id(CHAT_ID) is None

    @pytest.mark.asyncio
    async def test_invalid_wallet_rejected(self, http, patch_wallet_invalid):
        """
        Wallet validation failure → 422, no group created.
        Uses the same validate_ton_address path the bot uses.
        """
        await _seed_grant(OWNER_ID)

        resp = await http.post(
            "/api/groups/create",
            json={**VALID_BODY, "wallet_address": "not-valid"},
            headers=_auth(OWNER_ID),
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert isinstance(detail, str) and "wallet" in detail.lower()
        assert await _queries.get_group_by_chat_id(CHAT_ID) is None

    @pytest.mark.asyncio
    async def test_price_below_minimum_rejected(self, http):
        """Pydantic: price_usd_cents < 100 → 422 before any DB access."""
        resp = await http.post(
            "/api/groups/create",
            json={**VALID_BODY, "price_usd_cents": 50},
            headers=_auth(OWNER_ID),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_interval_rejected(self, http):
        """Pydantic: billing_interval_days not in (7, 30) → 422."""
        resp = await http.post(
            "/api/groups/create",
            json={**VALID_BODY, "billing_interval_days": 14},
            headers=_auth(OWNER_ID),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_insufficient_bot_permissions_rejected(self, http, patch_wallet_valid):
        """
        Grant exists but bot is missing can_manage_chat →
        422 with structured error body, nothing created.
        """
        await _seed_grant(OWNER_ID, can_manage=False)

        resp = await http.post(
            "/api/groups/create", json=VALID_BODY, headers=_auth(OWNER_ID)
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error"] == "insufficient_bot_permissions"
        assert "Ban Users" in detail["missing"]
        assert await _queries.get_group_by_chat_id(CHAT_ID) is None

    @pytest.mark.asyncio
    async def test_fee_preview_arithmetic_is_correct(self, http, patch_wallet_valid):
        """
        fee_preview numbers must be arithmetically consistent.
        (member_overpay) + (admin_underpay) = platform_keeps, within rounding.
        Mirrors the worked example from cb_confirm_chat.
        """
        await _seed_grant(OWNER_ID)

        resp = await http.post(
            "/api/groups/create",
            json={**VALID_BODY, "price_usd_cents": 2000},   # $20.00 worked example
            headers=_auth(OWNER_ID),
        )

        assert resp.status_code == 200
        fp = resp.json()["fee_preview"]

        price = fp["example_price_usd"]
        pays  = fp["member_pays_usd"]
        recv  = fp["admin_receives_usd"]
        keeps = fp["platform_keeps_usd"]

        assert pays > price,  "member should pay more than base price"
        assert recv < price,  "admin should receive less than base price"
        assert abs((pays - price) + (price - recv) - keeps) < 0.005, (
            f"fee arithmetic broken: pays={pays} recv={recv} keeps={keeps} price={price}"
        )

    @pytest.mark.asyncio
    async def test_unauthenticated_rejected(self, http):
        """No Authorization header → 401 or 422."""
        resp = await http.post("/api/groups/create", json=VALID_BODY)
        assert resp.status_code in (401, 422), resp.text

    @pytest.mark.asyncio
    async def test_no_grant_at_all_returns_403(self, http, patch_wallet_valid):
        """
        No grant row exists for this user at all →
        403, distinct from the 'permissions insufficient' 422.
        """
        # deliberately do NOT seed any grant
        resp = await http.post(
            "/api/groups/create", json=VALID_BODY, headers=_auth(OWNER_ID)
        )

        assert resp.status_code == 403
        assert await _queries.get_group_by_chat_id(CHAT_ID) is None

    @pytest.mark.asyncio
    async def test_upsert_is_idempotent(self, http, patch_wallet_valid):
        """
        Calling /create twice for the same chat_id re-activates, not duplicates.
        """
        await _seed_grant(OWNER_ID)
        r1 = await http.post(
            "/api/groups/create", json=VALID_BODY, headers=_auth(OWNER_ID)
        )
        assert r1.status_code == 200
        first_id = r1.json()["group_id"]

        # second grant for the second call
        await _seed_grant(OWNER_ID)
        r2 = await http.post(
            "/api/groups/create",
            json={**VALID_BODY, "price_usd_cents": 1500},
            headers=_auth(OWNER_ID),
        )
        assert r2.status_code == 200
        assert r2.json()["group_id"] == first_id   # same DB row, not a duplicate

        group = await _queries.get_group_by_id(first_id)
        assert group["price_usd_cents"] == 1500    # updated

    @pytest.mark.asyncio
    async def test_weekly_interval_accepted(self, http, patch_wallet_valid):
        """billing_interval_days=7 is valid and stored correctly."""
        await _seed_grant(OWNER_ID)

        resp = await http.post(
            "/api/groups/create",
            json={**VALID_BODY, "billing_interval_days": 7},
            headers=_auth(OWNER_ID),
        )

        assert resp.status_code == 200
        assert resp.json()["billing_interval_days"] == 7
        group = await _queries.get_group_by_chat_id(CHAT_ID)
        assert group["billing_interval_days"] == 7

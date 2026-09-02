"""
Renewise Mini App API server.

Authorization model
-------------------
Every request is authenticated via Telegram initData (HMAC-SHA256 over
WebAppData).  On top of that, every endpoint that touches group-scoped data
performs an ownership check — group["admin_telegram_id"] == requester's
telegram_id — before any read or write is allowed.  This matches the
_verify_admin_access pattern already established in admin_menu.py.

Rate limiting
-------------
Write endpoints are limited to 10 req/minute per user IP.
Read  endpoints are limited to 60 req/minute per user IP.
Both limits are enforced via slowapi (in-memory by default; swap the storage
backend to Redis once REDIS_URL is in the environment).

Out of scope
------------
No endpoint in this file checks ALLOWED_SUPERADMIN_IDS or exposes
cross-admin / platform-wide data.  Every endpoint is scoped to "data
belonging to the requesting user."
"""
# NOTE: do NOT add `from __future__ import annotations` here.
# slowapi wraps endpoint functions with functools.wraps, and FastAPI 0.100+
# resolves Annotated[..., Depends(...)] annotations at function-definition time.
# Postponed evaluation (PEP 563) turns those annotations into strings, which
# FastAPI cannot resolve back to the real Depends() object from within the
# wrapper's frame — causing it to treat `user` as a plain query parameter.

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from renewise.config import BOT_TOKEN
from renewise.db import queries
from renewise.miniapp.auth import verify_telegram_web_app_data
from renewise.services.wallet import validate_ton_address

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bot username cache — resolved once at startup via the Bot API.
# Used by the frontend to construct deep-links in the CONFIRMED correct format:
#   https://t.me/{bot_username}?start=renew_{group_id}
# ---------------------------------------------------------------------------
def _make_bot_cache() -> dict:
    return {"username": None}


_bot_cache = _make_bot_cache()
_STATIC_DIR = Path(__file__).resolve().parent / "static"


async def _resolve_bot_username() -> str | None:
    """Call getMe once and cache the bot's username for the lifetime of the process."""
    if _bot_cache["username"]:
        return _bot_cache["username"]
    try:
        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getMe",
                timeout=_aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    _bot_cache["username"] = data["result"]["username"]
                    return _bot_cache["username"]
    except OSError as exc:
        log.warning("Could not resolve bot username: %s", type(exc).__name__)
    return None


# ---------------------------------------------------------------------------
# Rate limiter — keyed on the originating IP address.
# Limits are applied per-decorator, so write endpoints get stricter caps.
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def _lifespan(application: FastAPI):
    """Run DB migrations and resolve bot username once at startup."""
    from renewise.db.schema import init_db
    from renewise.db.queries import delete_stale_pending_subscriptions
    await init_db()
    await _resolve_bot_username()
    # Run once immediately on startup, then every hour in the background.
    await delete_stale_pending_subscriptions(older_than_hours=12)
    cleanup_task = asyncio.create_task(_stale_pending_cleanup_loop())
    yield
    cleanup_task.cancel()


async def _stale_pending_cleanup_loop() -> None:
    """Hourly background task: delete pending subscriptions older than 12 hours."""
    while True:
        await asyncio.sleep(3600)
        try:
            from renewise.db.queries import delete_stale_pending_subscriptions
            deleted = await delete_stale_pending_subscriptions(older_than_hours=12)
            if deleted:
                log.info("miniapp: deleted %d stale pending subscription(s)", deleted)
        except Exception as exc:  # broad catch intentional — background loop must not crash
            log.warning("miniapp: stale pending cleanup error: %s", type(exc).__name__)


app = FastAPI(title="Renewise Mini App API", lifespan=_lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Global handler: any unhandled exception returns JSON {"detail": "..."} instead of
# Starlette's default plain-text "Internal Server Error" body. This prevents the
# frontend's `(await r.json()).detail` from throwing a JSON parse error on top of
# the real failure, making the actual error message visible to the user.
from fastapi import Request as _Request
from fastapi.responses import JSONResponse as _JSONResponse

@app.exception_handler(Exception)
async def _unhandled_exception_handler(_request: _Request, exc: Exception) -> _JSONResponse:
    log.exception("Unhandled exception in request to %s", _request.url.path)
    return _JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {type(exc).__name__}"},
    )

from renewise.api.platform import platform_router
app.include_router(platform_router, tags=["platform"])

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Static / config routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/checkout/{charge_id}")
async def checkout_ui(charge_id: int) -> FileResponse:
    return FileResponse(_STATIC_DIR / "checkout.html")


@app.get("/api/public/checkout/{charge_id}")
@limiter.limit("30/minute")
async def api_public_checkout(request: Request, charge_id: int) -> dict:
    """Public endpoint to get charge details for the hosted checkout page."""
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        row = await db.fetchrow(
            "SELECT * FROM platform_charges WHERE id = $1", charge_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="Charge not found")

        vault_address = row["vault_address"]
        required_nano = row["required_nano_amount"]
        # Use the stored payment_url which includes the full StateInit payload,
        # required for deploy-on-first-message. Fall back to a basic transfer
        # link only for legacy rows that predate this column.
        payment_url = row["payment_url"] or (
            f"ton://transfer/{vault_address}?amount={required_nano}"
            if vault_address else None
        )

        return {
            "id": row["id"],
            "external_reference": row["external_reference"],
            "amount_usd_cents": row["amount_usd_cents"],
            "status": row["status"],
            "vault_address": vault_address,
            "required_nano": required_nano,
            "payment_url": payment_url
        }


@app.get("/logo")
async def logo() -> FileResponse:
    return FileResponse(_STATIC_DIR / "public" / "logo.jpeg")


@app.get("/favicon.ico")
async def favicon() -> FileResponse:
    return FileResponse(_STATIC_DIR / "public" / "logo.jpeg")


@app.get("/healthz")
async def healthz() -> dict:
    """
    Health check endpoint — intentionally unauthenticated.

    Returns 200 {"status": "ok"} when the process is alive and the DB
    is reachable.  Returns 503 {"status": "error", "detail": ...} if the DB
    connectivity check fails, so a load-balancer or uptime monitor can detect
    a broken deployment without needing a valid Telegram initData token.

    Used by:
      - systemd / process supervisor: curl -sf http://localhost:8000/healthz
      - Uptime monitors (e.g. UptimeRobot, Better Uptime, Grafana)
    """
    from renewise.db.connection import _db as _conn
    try:
        async with _conn() as db:
            await db.fetchval("SELECT 1")
        return {"status": "ok"}
    except Exception as exc:
        log.error("Health check DB failure: %s", exc)
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": str(exc)},
        )


@app.get("/api/config")
async def api_config() -> dict:
    """
    Returns non-secret frontend configuration.
    The bot_username is used to construct the CONFIRMED correct deep-link format:
        https://t.me/{bot_username}?start=renew_{group_id}
    Intentionally unauthenticated — bot username is public.
    """
    username = _bot_cache["username"] or await _resolve_bot_username()
    return {"bot_username": username}


# ---------------------------------------------------------------------------
# Shared authentication dependency
# ---------------------------------------------------------------------------

async def get_telegram_user(
    authorization: Annotated[str, Header(description="Must be 'tma <initData>'")],
) -> dict:
    """
    Validates the initData Authorization header and returns the parsed Telegram
    user dict (contains at minimum {"id": <int>, ...}).

    Raises HTTP 401 on any failure so callers never see a partial auth state.
    """
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header scheme",
        )

    init_data = authorization[4:]  # strip "tma " prefix

    parsed_data = verify_telegram_web_app_data(init_data, BOT_TOKEN)
    if not parsed_data:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired Telegram initData signature",
        )

    try:
        user = json.loads(parsed_data.get("user", "{}"))
        if "id" not in user:
            raise HTTPException(status_code=401, detail="No user ID in initData")
        return user
    except json.JSONDecodeError:
        raise HTTPException(status_code=401, detail="Invalid user JSON payload")


# ---------------------------------------------------------------------------
# Shared authorization helper
# ---------------------------------------------------------------------------

async def verify_admin_or_403(telegram_user_id: int, group_id: int) -> dict:
    """
    Confirm `telegram_user_id` is the registered admin of `group_id`.

    This is the exact _verify_admin_access logic from admin_menu.py, promoted
    to a shared helper so every endpoint uses it identically — no drift.

    Returns the group row on success; raises HTTP 403 on failure.
    The group row is returned so callers do not need a second DB round-trip.
    """
    group = await queries.get_group_by_id(group_id)
    if not group or group["admin_telegram_id"] != telegram_user_id:
        raise HTTPException(status_code=403, detail="Unauthorized access to group data")
    return dict(group)


async def _refresh_group_meta_from_telegram(group_row: dict) -> dict:
    """Refresh stale chat_title/chat_type from Telegram for a single group."""
    telegram_chat_id = group_row.get("telegram_chat_id")
    if not telegram_chat_id:
        return group_row

    current_title = (group_row.get("chat_title") or "").strip()
    current_type = (group_row.get("chat_type") or "").strip()

    try:
        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getChat",
                params={"chat_id": telegram_chat_id},
                timeout=_aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    return group_row
                result = data.get("result") or {}
                new_title = result.get("title") or result.get("first_name") or str(telegram_chat_id)
                new_type = result.get("type") or "group"
                if current_title != new_title or current_type != new_type:
                    await queries.activate_paywall(
                        group_id=group_row["id"],
                        billing_interval_days=group_row.get("billing_interval_days") or 30,
                        payout_wallet_address=group_row.get("payout_wallet_address") or "",
                        chat_title=new_title,
                        invite_link=group_row.get("invite_link"),
                        chat_type=new_type,
                    )
                    group_row["chat_title"] = new_title
                    group_row["chat_type"] = new_type
                return group_row
    except OSError as exc:
        log.warning("Could not refresh metadata for group %s: %s", telegram_chat_id, type(exc).__name__)
        return group_row


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class GroupCreateRequest(BaseModel):
    """
    All fields needed to activate a paywall, mirrors the wizard state that
    cb_final_confirm collects before calling upsert_group / activate_paywall.
    """
    telegram_chat_id: int
    price_usd_cents: int        # e.g. 999 = $9.99
    billing_interval_days: int  # 7 or 30
    wallet_address: str

    @field_validator("price_usd_cents")
    @classmethod
    def price_must_be_positive(cls, v: int) -> int:
        if v < 100:  # minimum $1.00
            raise ValueError("price_usd_cents must be at least 100 ($1.00)")
        return v

    @field_validator("billing_interval_days")
    @classmethod
    def interval_must_be_valid(cls, v: int) -> int:
        if v not in (7, 30):
            raise ValueError("billing_interval_days must be 7 (weekly) or 30 (monthly)")
        return v


# ---------------------------------------------------------------------------
# Existing read endpoints
# ---------------------------------------------------------------------------

@app.get("/api/my-subscriptions")
@limiter.limit("60/minute")
async def api_my_subscriptions(
    request: Request,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    telegram_user_id = user["id"]
    subs = await queries.get_user_subscriptions(telegram_user_id)
    return {"subscriptions": [dict(s) for s in subs]}


@app.get("/api/my-payment-history")
@limiter.limit("60/minute")
async def api_my_payment_history(
    request: Request,
    user: Annotated[dict, Depends(get_telegram_user)],
    offset: int = 0,
    limit: int = 20,
) -> dict:
    """
    Paginated payment history for the authenticated member.
    Returns confirmed payments across all groups, newest first.
    No fee-split data — members only see what they paid.
    """
    limit = min(limit, 200)
    offset = max(offset, 0)
    telegram_user_id = user["id"]
    rows, total = await queries.get_user_payment_history(telegram_user_id, offset=offset, limit=limit)
    history = []
    for r in rows:
        r = dict(r)
        # Convert required_nano_amount → TON for display convenience
        nano = r.get("required_nano_amount")
        r["amount_ton"] = round(nano / 1e9, 9) if nano else None
        history.append(r)
    return {"history": history, "total": total, "offset": offset, "limit": limit}


@app.get("/api/my-groups")
@limiter.limit("60/minute")
async def api_my_groups(
    request: Request,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    telegram_user_id = user["id"]
    from renewise.superadmin.queries import get_admin_detail
    detail = await get_admin_detail(telegram_user_id)

    refreshed_groups = []
    for g in detail["groups"]:
        refreshed_groups.append(await _refresh_group_meta_from_telegram(dict(g)))
    detail["groups"] = refreshed_groups

    # ── Total GRAM (nanonano) — real stored on-chain required amount ──────────
    # Sum of required_nano_amount across all active/comped subscriptions owned
    # by this admin. For older subscriptions created before that column existed,
    # we fall back to extracting the exact GRAM amount from the audit log using
    # the last_payment_tx_hash. This ensures 100% accurate on-chain totals.
    import ast
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        # 1. Build tx_hash → amount_nano from audit log
        audit_rows = await db.fetch(
            "SELECT a.details "
            "FROM admin_audit_log a "
            "JOIN groups g ON g.id = a.group_id "
            "WHERE g.admin_telegram_id = $1 AND a.action = 'payment_confirmed'",
            telegram_user_id,
        )
        tx_amounts = {}
        for row in audit_rows:
            try:
                details = ast.literal_eval(row["details"])
                if "tx_hash" in details and "amount_nano" in details:
                    tx_amounts[details["tx_hash"]] = details["amount_nano"]
            except (ValueError, SyntaxError, TypeError):
                log.debug("Skipping unparseable audit log details row")

        # 2. Sum active/comped subscription required amounts
        sub_rows = await db.fetch(
            "SELECT s.required_nano_amount, s.last_payment_tx_hash "
            "FROM subscriptions s "
            "JOIN groups g ON g.id = s.group_id "
            "WHERE g.admin_telegram_id = $1 AND s.status IN ('active', 'comped')",
            telegram_user_id,
        )
        total_revenue_nano = 0
        for row in sub_rows:
            req_nano = row["required_nano_amount"]
            if req_nano is not None and req_nano > 0:
                total_revenue_nano += req_nano
            elif row["last_payment_tx_hash"]:
                total_revenue_nano += tx_amounts.get(row["last_payment_tx_hash"], 0)

    # ── Net revenue: subtract the platform's admin_fee from gross amounts ─────
    # The vault splits funds on-chain: admin receives price * (1 - admin_fee_bps/10000).
    # We show the admin what actually lands in their wallet, not the gross member payment.
    from renewise.db.queries import get_global_fees
    global_buyer_bps, global_admin_bps = await get_global_fees()
    # Use the first group's per-group override if set, otherwise fall back to global
    first_group_admin_bps = None
    if detail["groups"]:
        first_group_admin_bps = detail["groups"][0].get("admin_fee_bps")
    admin_fee_bps = first_group_admin_bps if first_group_admin_bps is not None else global_admin_bps
    admin_fee_pct = admin_fee_bps / 100.0   # e.g. 330 bps → 3.30%

    keep_ratio   = 1.0 - admin_fee_bps / 10000.0
    net_revenue  = int(round(detail["total_revenue"] * keep_ratio))  # USD cents admin keeps
    net_revenue_nano = int(round(total_revenue_nano * keep_ratio))   # nanoTON admin keeps

    return {
        "groups":            detail["groups"],
        "total_subs":        detail["total_subs"],
        "total_revenue":     detail["total_revenue"],     # gross USD cents (for reference)
        "total_revenue_nano": total_revenue_nano,          # gross nanoTON (for reference)
        "net_revenue":       net_revenue,                  # USD cents after platform fee
        "net_revenue_nano":  net_revenue_nano,             # nanoTON after platform fee
        "admin_fee_pct":     admin_fee_pct,                # e.g. 3.30
    }


@app.get("/api/groups/{group_id}/payment-history")
@limiter.limit("60/minute")
async def api_group_payment_history(
    request: Request,
    group_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
    offset: int = 0,
    limit: int = 5,
) -> dict:
    telegram_user_id = user["id"]
    limit = min(limit, 200)
    offset = max(offset, 0)
    await verify_admin_or_403(telegram_user_id, group_id)

    rows, total_count = await queries.get_payment_history(
        group_id, offset=offset, limit=limit
    )
    return {
        "payment_history": [dict(r) for r in rows],
        "total_count": total_count,
        "offset": offset,
        "limit": limit,
    }


# ── GET /api/groups/{group_id}/payments/{sub_id} ──────────────────────────────

@app.get("/api/groups/{group_id}/payments/{sub_id}")
@limiter.limit("60/minute")
async def api_payment_detail(
    request: Request,
    group_id: int,
    sub_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Full detail for a single payment / subscription row.

    Returns all fields the list view already has, plus:
      - vault_address       — the smart contract address for this payment
      - required_nano_amount — exact TON nanotons required
      - amount_paid_so_far  — running total for partial payments
      - telegram_user_id    — subscriber's Telegram ID
      - username            — subscriber's @handle (may be null)
      - chat_title          — group/channel name
      - billing_interval_days
      - tonviewer_url       — direct deep-link to the tx on TonViewer
    """
    telegram_user_id = user["id"]
    # Ownership: sub must belong to a group owned by this admin
    await verify_admin_or_403(telegram_user_id, group_id)

    row = await queries.get_payment_detail(sub_id)
    if not row:
        raise HTTPException(status_code=404, detail="Payment not found")

    row = dict(row)

    # Safety: confirm this sub actually belongs to the requested group
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        r = await db.fetchrow(
            "SELECT group_id FROM subscriptions WHERE id=$1", sub_id
        )
        if not r or r["group_id"] != group_id:
            raise HTTPException(status_code=404, detail="Payment not found in this group")

    tx = row.get("last_payment_tx_hash")
    row["tonviewer_url"] = f"https://tonviewer.com/{tx}" if tx else None

    # Convert nano to TON for convenience
    nano = row.get("required_nano_amount")
    row["required_ton"] = round(nano / 1e9, 9) if nano else None

    # ── Fee breakdown for payment details modal ───────────────────────────────
    # Show the admin exactly what they received after the platform's admin_fee cut.
    from renewise.db.queries import get_global_fees
    global_buyer_bps, global_admin_bps = await get_global_fees()
    group_row = await queries.get_group_by_id(group_id)
    admin_fee_bps = (
        group_row["admin_fee_bps"]
        if group_row and group_row["admin_fee_bps"] is not None
        else global_admin_bps
    )
    buyer_fee_bps = (
        group_row["buyer_fee_bps"]
        if group_row and group_row["buyer_fee_bps"] is not None
        else global_buyer_bps
    )
    row["admin_fee_pct"]  = admin_fee_bps / 100.0   # e.g. 3.30
    row["buyer_fee_pct"]  = buyer_fee_bps / 100.0   # e.g. 2.00
    row["platform_fee_pct"] = (admin_fee_bps + buyer_fee_bps) / 100.0  # total e.g. 5.30
    if nano:
        keep_ratio = 1.0 - admin_fee_bps / 10000.0
        row["admin_payout_ton"] = round((nano / 1e9) * keep_ratio, 9)
    else:
        row["admin_payout_ton"] = None

    return row
# POST /api/groups/detect
# ---------------------------------------------------------------------------

@app.post("/api/groups/detect")
@limiter.limit("10/minute")
async def api_groups_detect(
    request: Request,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Mirrors STEP_DETECT in create_paywall.py / cb_check_now.

    Queries recent_admin_grants for the authenticated user (last 15 minutes)
    and returns whichever chats the bot has been made admin of.

    No group_id is accepted from the body — the only identifier is the
    verified Telegram user from initData, so there is nothing to IDOR against.

    Also checks for admin bans/suspensions exactly as cmd_create_paywall does,
    so the Mini App and the chat flow have identical guards.
    """
    telegram_user_id: int = user["id"]

    # Mirror the ban/suspension checks from cmd_create_paywall
    ban_reason = await queries.get_admin_ban_reason(telegram_user_id)
    if ban_reason:
        raise HTTPException(
            status_code=403,
            detail=f"Account banned: {ban_reason}",
        )

    if await queries.is_admin_suspended(telegram_user_id):
        raise HTTPException(status_code=403, detail="Account suspended")

    grants = await queries.get_recent_admin_grants(telegram_user_id, minutes=15)

    result = []
    for g in grants:
        g = dict(g)
        # Compute missing permissions using the same logic as _grant_has_perms
        # in create_paywall.py
        chat_type = g["chat_type"]
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
        missing = [label for col, label in checks if not g[col]]

        result.append({
            "grant_id":            g["id"],
            "telegram_chat_id":    g["telegram_chat_id"],
            "chat_title":          g["chat_title"],
            "chat_type":           chat_type,
            "granted_at":          g["granted_at"],
            "permissions_ok":      len(missing) == 0,
            "missing_permissions": missing,
        })

    return {"detected_chats": result}


# ---------------------------------------------------------------------------
# NEW — Group/Channel Onboarding: create/activate step
# POST /api/groups/create
# ---------------------------------------------------------------------------

@app.post("/api/groups/create")
@limiter.limit("10/minute")
async def api_groups_create(
    request: Request,
    body: GroupCreateRequest,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Mirrors STEP_CONFIRM_CHAT → STEP_FINAL_CONFIRM → cb_final_confirm in
    create_paywall.py.

    Accepts the four wizard-collected values, validates the wallet address,
    then calls the SAME upsert_group / activate_paywall /
    update_group_price_usd_cents functions the bot uses — no reimplementation.

    Authorization
    -------------
    The telegram_chat_id in the body must correspond to a recent_admin_grant
    row that belongs to the authenticated user.  This is the Mini App
    equivalent of the bot's grant_id-embedded callback that prevents one
    user from activating a paywall on a chat they didn't make the bot admin
    of.  We also confirm can_invite_users + can_manage_chat (+ can_post_messages
    for channels) are present — same as _grant_has_perms.

    Fee transparency
    ----------------
    Returns the same fee-transparency numbers the bot shows in STEP_FEE_INFO
    so the frontend can display the worked example in the final summary step.

    Note on invite link
    -------------------
    The Mini App cannot call ctx.bot.create_chat_invite_link directly.  We
    create the group row and let the bot scheduler generate the link on its
    next pass.  The response includes invite_link: null with a note.
    """
    telegram_user_id: int = user["id"]

    # ── Ban / suspension guard ────────────────────────────────────────────────
    ban_reason = await queries.get_admin_ban_reason(telegram_user_id)
    if ban_reason:
        raise HTTPException(status_code=403, detail=f"Account banned: {ban_reason}")

    if await queries.is_admin_suspended(telegram_user_id):
        raise HTTPException(status_code=403, detail="Account suspended")

    # ── Authorization: verify this user made the bot admin of this chat ───────
    # Mirrors cb_grant_confirm: grant["from_user_id"] == update.effective_user.id
    recent_grants = await queries.get_recent_admin_grants(telegram_user_id, minutes=15)
    matching_grant = next(
        (g for g in recent_grants if g["telegram_chat_id"] == body.telegram_chat_id),
        None,
    )

    if matching_grant is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "No recent admin-grant found for this chat. "
                "Add the bot as admin in the group/channel first, "
                "then call /api/groups/detect within 15 minutes."
            ),
        )

    # ── Permission check (mirrors _grant_has_perms) ───────────────────────────
    g = dict(matching_grant)
    chat_type = g["chat_type"]
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
    missing = [label for col, label in checks if not g[col]]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "insufficient_bot_permissions",
                "missing": missing,
            },
        )

    # ── Wallet validation (same validate_ton_address the bot uses) ────────────
    if not await validate_ton_address(body.wallet_address):
        raise HTTPException(
            status_code=422,
            detail="Invalid TON wallet address format",
        )

    # ── Fee transparency (mirrors cb_confirm_chat) ────────────────────────────
    global_buyer_bps, global_admin_bps = await queries.get_global_fees()
    existing_group = await queries.get_group_by_chat_id(body.telegram_chat_id)
    buyer_bps = (
        existing_group["buyer_fee_bps"]
        if (existing_group and existing_group["buyer_fee_bps"] is not None)
        else global_buyer_bps
    )
    admin_bps = (
        existing_group["admin_fee_bps"]
        if (existing_group and existing_group["admin_fee_bps"] is not None)
        else global_admin_bps
    )

    price_usd  = body.price_usd_cents / 100.0
    buyer_fee  = price_usd * (buyer_bps / 10000.0)
    admin_fee  = price_usd * (admin_bps / 10000.0)
    fee_preview = {
        "buyer_fee_bps":       buyer_bps,
        "admin_fee_bps":       admin_bps,
        "example_price_usd":   price_usd,
        "member_pays_usd":     round(price_usd + buyer_fee, 4),
        "admin_receives_usd":  round(price_usd - admin_fee, 4),
        "platform_keeps_usd":  round(buyer_fee + admin_fee, 4),
    }

    # ── Activate — same three-call sequence as cb_final_confirm ──────────────
    chat_title = g["chat_title"]

    group_id = await queries.upsert_group(
        body.telegram_chat_id, telegram_user_id, chat_type=chat_type
    )

    await queries.activate_paywall(
        group_id=group_id,
        billing_interval_days=body.billing_interval_days,
        payout_wallet_address=body.wallet_address,
        chat_title=chat_title,
        invite_link=None,   # bot scheduler generates this; see docstring
        chat_type=chat_type,
    )
    await queries.update_group_price_usd_cents(group_id, body.price_usd_cents)

    # ── Audit log — real actor's telegram_id, matching pattern everywhere ─────
    await queries.audit(
        group_id,
        "paywall_created_via_miniapp",
        telegram_user_id,
        {
            "telegram_chat_id": body.telegram_chat_id,
            "price_usd_cents":  body.price_usd_cents,
            "interval_days":    body.billing_interval_days,
            "wallet":           body.wallet_address,
        },
    )

    log.info(
        "Paywall created via Mini App: group_id=%d chat_id=%d admin=%d",
        group_id,
        body.telegram_chat_id,
        telegram_user_id,
    )

    return {
        "ok":                    True,
        "group_id":              group_id,
        "chat_title":            chat_title,
        "chat_type":             chat_type,
        "price_usd_cents":       body.price_usd_cents,
        "billing_interval_days": body.billing_interval_days,
        "wallet_address":        body.wallet_address,
        "invite_link":           None,
        "invite_link_note": (
            "The bot will generate an invite link automatically. "
            "Refresh /api/my-groups in a few seconds to see it."
        ),
        "fee_preview": fee_preview,
    }


# ---------------------------------------------------------------------------
# Developer Tab / Platform Endpoints
# ---------------------------------------------------------------------------
from renewise.services import platform as platform_svc

class PlatformCreateRequest(BaseModel):
    platform_name: str

    @field_validator("platform_name")
    @classmethod
    def platform_name_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Platform name is required")
        return v.strip()

class PlatformWalletRequest(BaseModel):
    wallet_address: str
    passcode: str | None = None

class PlatformPasscodeRequest(BaseModel):
    new_passcode: str
    current_passcode: str | None = None

    @field_validator("new_passcode")
    @classmethod
    def passcode_must_be_4_digits(cls, v: str) -> str:
        if not v.isdigit() or len(v) != 4:
            raise ValueError("Passcode must be a 4-digit number")
        return v

@app.get("/api/developer/platforms")
@limiter.limit("60/minute")
async def api_developer_platforms_get(
    request: Request,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """Returns all active platforms owned by the authenticated user and aggregated stats."""
    platforms = await platform_svc.get_user_platforms(user["id"])
    total_revenue_usd_cents = sum(p.get("revenue_usd_cents", 0) for p in platforms)
    total_transactions = sum(p.get("transactions_count", 0) for p in platforms)
    return {
        "platforms": platforms,
        "total_revenue_usd_cents": total_revenue_usd_cents,
        "total_transactions": total_transactions
    }

@app.get("/api/developer/charges")
@limiter.limit("60/minute")
async def api_developer_charges_global(
    request: Request,
    user: Annotated[dict, Depends(get_telegram_user)],
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Retrieves paginated API charges across all platforms owned by the user."""
    limit = min(limit, 200)
    offset = max(offset, 0)
    _valid_statuses = {"pending", "completed", "expired", "failed"}
    if status is not None and status not in _valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(sorted(_valid_statuses))}")
    telegram_user_id = user["id"]
    charges = await platform_svc.get_all_user_platform_charges(telegram_user_id, status=status, limit=limit, offset=offset)

    # Include aggregate stats on the first page so the UI can show a summary strip
    stats = None
    if offset == 0:
        from renewise.db.connection import _db as _conn
        async with _conn() as db:
            row = await db.fetchrow(
                """
                SELECT
                    COUNT(*)                                             AS total,
                    COUNT(*) FILTER (WHERE c.status = 'completed')      AS completed,
                    COALESCE(SUM(c.amount_usd_cents)
                             FILTER (WHERE c.status = 'completed'), 0)  AS volume_usd_cents
                FROM platform_charges c
                JOIN platforms p ON p.id = c.platform_id
                WHERE p.owner_telegram_id = $1
                """,
                telegram_user_id,
            )
            if row:
                stats = {
                    "total":      int(row["total"]),
                    "completed":  int(row["completed"]),
                    "volume_usd": int(row["volume_usd_cents"]),
                }

    return {"charges": charges, "stats": stats}


@app.get("/api/developer/charges/{charge_id}")
@limiter.limit("60/minute")
async def api_developer_charge_detail(
    request: Request,
    charge_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Full detail for a single platform charge, authenticated via initData.
    Only returns the charge if it belongs to a platform owned by the
    authenticated user.
    """
    telegram_user_id = user["id"]
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        row = await db.fetchrow(
            """
            SELECT c.id, c.platform_id, p.platform_name, c.external_reference,
                   c.mode, c.amount_usd_cents, c.status, c.vault_address,
                   c.payment_url, c.buyer_fee_bps, c.platform_fee_bps,
                   c.tx_hash, c.required_nano_amount, c.created_at, c.completed_at
            FROM platform_charges c
            JOIN platforms p ON p.id = c.platform_id
            WHERE c.id = $1 AND p.owner_telegram_id = $2
            """,
            charge_id, telegram_user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Charge not found")

    base_url = str(request.base_url).rstrip("/")
    r = dict(row)
    r["checkout_url"] = f"{base_url}/checkout/{r['id']}"
    r["required_nano"] = r.pop("required_nano_amount", None)
    # Compute required TON for display
    nano = r.get("required_nano")
    r["required_ton"] = round(nano / 1e9, 9) if nano else None
    # Fee percentages for display
    r["buyer_fee_pct"]    = (r["buyer_fee_bps"] or 0) / 100.0
    r["platform_fee_pct"] = (r["platform_fee_bps"] or 0) / 100.0
    # TonScan link
    explorer = "testnet.tonscan.org" if r["mode"] == "test" else "tonscan.org"
    r["explorer_url"] = f"https://{explorer}/tx/{r['tx_hash']}" if r["tx_hash"] else None
    return r

@app.post("/api/developer/platforms")
@limiter.limit("10/minute")
async def api_developer_platforms_create(
    request: Request,
    body: PlatformCreateRequest,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """Creates a new platform and returns the raw secret key exactly once."""
    telegram_user_id = user["id"]
    
    # Simple check on max platforms (e.g. limit to 3 per user for now)
    existing = await platform_svc.get_user_platforms(telegram_user_id)
    if len(existing) >= 3:
        raise HTTPException(status_code=400, detail="Maximum of 3 platforms allowed per user.")

    platform, raw_secret_key_test = await platform_svc.create_platform(
        owner_telegram_id=telegram_user_id,
        platform_name=body.platform_name,
    )
    return {
        "platform": platform,
        "raw_secret_key_test": raw_secret_key_test
    }

@app.post("/api/developer/platforms/{platform_id}/live-keys")
@limiter.limit("5/minute")
async def api_developer_generate_live_keys(
    request: Request,
    platform_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """Generates live keys for a platform for the first time."""
    telegram_user_id = user["id"]
    await verify_platform_ownership(platform_id, telegram_user_id)
    
    try:
        pk_live, sk_live = await platform_svc.generate_live_keys(platform_id, telegram_user_id)
        return {
            "ok": True,
            "publishable_key_live": pk_live,
            "raw_secret_key_live": sk_live
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.put("/api/developer/platforms/{platform_id}/wallet")
@limiter.limit("5/minute")
async def api_developer_update_wallet(
    request: Request,
    platform_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """Updates the payout wallet for a developer platform."""
    body = await request.json()
    wallet_address = body.get("wallet_address")
    passcode = body.get("passcode")
    
    if not wallet_address:
        raise HTTPException(status_code=400, detail="Wallet address is required")

    # Validate format first
    if not await validate_ton_address(wallet_address):
        raise HTTPException(status_code=422, detail="Invalid TON wallet address format")

    telegram_user_id = user["id"]
    platform = await verify_platform_ownership(platform_id, telegram_user_id)

    # Network enforcement depends on whether the platform has live keys:
    #
    # • Test-only platform (no live keys yet): accept both mainnet and testnet
    #   wallet addresses. Test charges route to testnet regardless of address
    #   type — the developer is told to pay with testnet TON, not real money.
    #
    # • Live platform (live keys generated): enforce mainnet-only wallet when
    #   TONCENTER_TESTNET=false, because live charges settle real funds.
    #   If TONCENTER_TESTNET=true (full testnet mode) both types are accepted.
    from renewise.services.wallet import detect_address_network
    from renewise.config import TONCENTER_TESTNET
    _addr_net = detect_address_network(wallet_address)
    has_live_keys = bool(platform.get("publishable_key_live"))

    if has_live_keys and not TONCENTER_TESTNET and _addr_net == "testnet":
        raise HTTPException(
            status_code=422,
            detail=(
                "This platform has live keys. "
                "Testnet wallet address provided but the system is running on mainnet. "
                "Please use a mainnet address (EQ... or UQ...) to receive live payouts."
            ),
        )
    
    success = await platform_svc.update_platform_wallet(platform_id, wallet_address, telegram_user_id, passcode)
    if not success:
        raise HTTPException(status_code=403, detail="Incorrect or missing passcode")

    addr_network = _addr_net or "unknown"
    # For test-only platforms, remind the dev to use testnet TON when paying
    testnet_notice = (
        "This wallet is set for test charges. When testing, pay with testnet TON — "
        "not real money. Testnet TON is free from https://t.me/testgiver_ton_bot"
        if not has_live_keys else None
    )
    warning = (
        "Raw address format — unable to verify network compatibility. "
        "Ensure this address is reachable on the correct network."
        if addr_network == "raw" else testnet_notice
    )

    return {"ok": True, "address_network": addr_network, "has_live_keys": has_live_keys, "warning": warning}

@app.post("/api/developer/platforms/{platform_id}/passcode")
@limiter.limit("5/minute")
async def api_developer_set_passcode(
    request: Request,
    platform_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """Sets or updates the passcode for a developer platform."""
    body = await request.json()
    new_passcode = body.get("new_passcode")
    current_passcode = body.get("current_passcode")
    
    if not new_passcode:
        raise HTTPException(status_code=400, detail="New passcode is required")
        
    telegram_user_id = user["id"]
    await verify_platform_ownership(platform_id, telegram_user_id)
    
    try:
        success = await platform_svc.set_platform_passcode(platform_id, new_passcode, telegram_user_id, current_passcode)
        if not success:
            raise HTTPException(status_code=403, detail="Incorrect current passcode")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
        
    return {"ok": True}

@app.get("/api/developer/platforms/{platform_id}/charges")
@limiter.limit("60/minute")
async def api_developer_platform_charges(
    request: Request,
    platform_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
    status: str | None = None,
    limit: int = 50,
    offset: int = 0
) -> dict:
    """Retrieves paginated charges for a platform and basic aggregate stats."""
    limit = min(limit, 200)
    offset = max(offset, 0)
    _valid_statuses = {"pending", "completed", "expired", "failed"}
    if status is not None and status not in _valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(sorted(_valid_statuses))}")
    telegram_user_id = user["id"]
    await verify_platform_ownership(platform_id, telegram_user_id)
    
    charges = await platform_svc.get_platform_charges(platform_id, status=status, limit=limit, offset=offset)
    stats = await platform_svc.get_platform_stats(platform_id)
    webhook = await platform_svc.get_platform_webhook(platform_id)
    
    return {
        "charges": charges, 
        "stats": stats,
        "webhook": webhook
    }

class RegenerateKeyRequest(BaseModel):
    mode: str

@app.post("/api/developer/platforms/{platform_id}/keys/regenerate")
@limiter.limit("5/minute")
async def api_developer_regenerate_keys(
    request: Request,
    platform_id: int,
    body: RegenerateKeyRequest,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """Regenerates the secret key for a specific mode."""
    telegram_user_id = user["id"]
    await verify_platform_ownership(platform_id, telegram_user_id)
    
    if body.mode not in ("live", "test"):
        raise HTTPException(status_code=400, detail="Invalid mode")
        
    new_secret = await platform_svc.regenerate_platform_keys(platform_id, body.mode, telegram_user_id)
    
    # Immediately invalidate any cached auth entries for this platform's old keys
    # so the rotated secret stops working without waiting for the 60s TTL
    from renewise.api.platform import auth_cache
    stale = [k for k, v in list(auth_cache.items()) if isinstance(v, dict) and v.get('id') == platform_id and v.get('auth_mode') == body.mode]
    for k in stale:
        auth_cache.pop(k, None)
    
    return {"ok": True, "new_secret_key": new_secret}

@app.delete("/api/developer/platforms/{platform_id}")
@limiter.limit("3/minute")
async def api_developer_delete_platform(
    request: Request,
    platform_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Permanently deletes a platform and all its associated data:
    charges, webhooks, audit logs, and both key pairs.
    This action is irreversible.
    """
    telegram_user_id = user["id"]
    await verify_platform_ownership(platform_id, telegram_user_id)

    from renewise.superadmin import queries as superadmin_queries
    deleted = await superadmin_queries.delete_platform(platform_id, telegram_user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Platform not found")

    # Invalidate all cached auth entries for this platform (test + live)
    from renewise.api.platform import auth_cache
    stale = [k for k, v in list(auth_cache.items()) if isinstance(v, dict) and v.get('id') == platform_id]
    for k in stale:
        auth_cache.pop(k, None)

    return {"ok": True}

class WebhookRequest(BaseModel):
    url: str

@app.post("/api/developer/platforms/{platform_id}/webhook")
@limiter.limit("10/minute")
async def api_developer_set_webhook(
    request: Request,
    platform_id: int,
    body: WebhookRequest,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Sets or updates the webhook URL.
    - First setup: returns a one-time webhook_secret that must be saved immediately.
    - URL update: returns webhook_secret=null — the existing secret is preserved.
    """
    telegram_user_id = user["id"]
    await verify_platform_ownership(platform_id, telegram_user_id)

    if not body.url.startswith("https://") and not body.url.startswith("http://"):
        raise HTTPException(status_code=400, detail="Invalid URL format")

    result = await platform_svc.set_platform_webhook(platform_id, body.url, telegram_user_id)
    return {"ok": True, "webhook_secret": result["secret"], "is_new": result["is_new"]}

@app.post("/api/developer/platforms/{platform_id}/webhook/rotate-secret")
@limiter.limit("5/minute")
async def api_developer_rotate_webhook_secret(
    request: Request,
    platform_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """Explicitly rotates the webhook signing secret without changing the URL."""
    telegram_user_id = user["id"]
    await verify_platform_ownership(platform_id, telegram_user_id)

    try:
        new_secret = await platform_svc.rotate_webhook_secret(platform_id, telegram_user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"ok": True, "webhook_secret": new_secret}


async def verify_platform_ownership(platform_id: int, telegram_user_id: int) -> dict:
    """Helper to ensure the platform belongs to the requester."""
    platforms = await platform_svc.get_user_platforms(telegram_user_id)
    matching = [p for p in platforms if p["id"] == platform_id]
    if not matching:
        raise HTTPException(status_code=403, detail="Unauthorized access to platform data")
    return matching[0]


# ---------------------------------------------------------------------------
# GROUP MANAGEMENT ENDPOINTS
# Every endpoint: verifies initData + verify_admin_or_403, logs to audit log.
# ---------------------------------------------------------------------------

class UpdatePriceRequest(BaseModel):
    price_usd_cents: int

    @field_validator("price_usd_cents")
    @classmethod
    def price_positive(cls, v: int) -> int:
        if v < 100:
            raise ValueError("price_usd_cents must be at least 100 ($1.00)")
        return v


class UpdateWalletRequest(BaseModel):
    wallet_address: str
    passcode: str | None = None


class CompMemberRequest(BaseModel):
    telegram_user_id: int


# ── GET /api/groups/{group_id}/detail ────────────────────────────────────────

@app.get("/api/groups/{group_id}/detail")
@limiter.limit("60/minute")
async def api_group_detail(
    request: Request,
    group_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Returns full stats for a single group including live chat title resolution
    via the Telegram Bot API — this fixes the 'Unnamed' display for groups
    whose chat_title was NULL in the DB (created before the wizard was added).
    """
    telegram_user_id = user["id"]
    group = await verify_admin_or_403(telegram_user_id, group_id)

    # Try to get a live chat title if the DB value is NULL
    chat_title = group.get("chat_title")
    chat_type  = group.get("chat_type")
    if not chat_title or not chat_type:
        try:
            import aiohttp as _aiohttp
            async with _aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/getChat",
                    params={"chat_id": group["telegram_chat_id"]},
                    timeout=_aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        chat_title = chat_title or data["result"].get("title") or data["result"].get("first_name") or str(group["telegram_chat_id"])
                        chat_type  = chat_type  or data["result"].get("type", "group")
                        # Persist both fields so future calls are instant
                        await queries.activate_paywall(
                            group_id=group_id,
                            billing_interval_days=group.get("billing_interval_days") or 30,
                            payout_wallet_address=group.get("payout_wallet_address") or "",
                            chat_title=chat_title,
                            invite_link=group.get("invite_link"),
                            chat_type=chat_type,
                        )
        except OSError as exc:
            log.warning("Could not resolve chat info for group %d: %s", group_id, type(exc).__name__)
            chat_title = chat_title or f"Chat {group['telegram_chat_id']}"
            chat_type  = chat_type  or "group"

    # Revenue: sum of price_locked_in for active/comped subs — convert to cents
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        revenue_usd = await db.fetchval(
            "SELECT COALESCE(SUM(price_locked_in),0) FROM subscriptions "
            "WHERE group_id=$1 AND status IN ('active','comped')",
            group_id,
        ) or 0
        revenue_usd_cents = int(round(float(revenue_usd) * 100))

    active_count = await queries.count_members(group_id)

    return {
        "id":                    group_id,
        "chat_title":            chat_title,
        "chat_type":             chat_type,
        "status":                group.get("status", "active"),
        "price_usd_cents":       group.get("price_usd_cents") or int((group.get("price") or 0) * 100),
        "billing_interval_days": group.get("billing_interval_days", 30),
        "payout_wallet_address": group.get("payout_wallet_address"),
        "invite_link":           group.get("invite_link"),
        "active_members":        active_count,
        "revenue_usd_cents":     revenue_usd_cents,
        # Boolean so the hash is never exposed to the client
        "has_wallet_passcode":   bool(group.get("wallet_passcode_hash")),
    }


# ── GET /api/groups/{group_id}/members ───────────────────────────────────────

@app.get("/api/groups/{group_id}/members")
@limiter.limit("60/minute")
async def api_group_members(
    request: Request,
    group_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
    offset: int = 0,
    limit: int = 20,
) -> dict:
    telegram_user_id = user["id"]
    limit = min(limit, 200)
    offset = max(offset, 0)
    await verify_admin_or_403(telegram_user_id, group_id)

    members = await queries.get_members_page(group_id, offset, limit)
    total   = await queries.count_members(group_id)
    return {
        "members":    [dict(m) for m in members],
        "total":      total,
        "offset":     offset,
        "limit":      limit,
    }


# ── POST /api/groups/{group_id}/pause ────────────────────────────────────────

@app.post("/api/groups/{group_id}/pause")
@limiter.limit("10/minute")
async def api_group_pause(
    request: Request,
    group_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    telegram_user_id = user["id"]
    group = await verify_admin_or_403(telegram_user_id, group_id)

    if group["status"] == "paused":
        raise HTTPException(status_code=400, detail="Group is already paused")

    await queries.set_group_status(group_id, "paused")
    await queries.audit(group_id, "group_paused_via_miniapp", telegram_user_id)
    return {"ok": True, "status": "paused"}


# ── POST /api/groups/{group_id}/resume ───────────────────────────────────────

@app.post("/api/groups/{group_id}/resume")
@limiter.limit("10/minute")
async def api_group_resume(
    request: Request,
    group_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    telegram_user_id = user["id"]
    group = await verify_admin_or_403(telegram_user_id, group_id)

    if group["status"] == "active":
        raise HTTPException(status_code=400, detail="Group is already active")

    await queries.set_group_status(group_id, "active")
    await queries.audit(group_id, "group_resumed_via_miniapp", telegram_user_id)
    return {"ok": True, "status": "active"}


# ── PUT /api/groups/{group_id}/price ─────────────────────────────────────────

@app.put("/api/groups/{group_id}/price")
@limiter.limit("10/minute")
async def api_group_update_price(
    request: Request,
    group_id: int,
    body: UpdatePriceRequest,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Update price_usd_cents. Existing subscribers keep their price_locked_in
    until their next renewal — this rule is preserved because we only update
    the groups table, never the subscriptions.price_locked_in field.
    """
    telegram_user_id = user["id"]
    await verify_admin_or_403(telegram_user_id, group_id)

    await queries.update_group_price_usd_cents(group_id, body.price_usd_cents)
    await queries.audit(
        group_id, "price_updated_via_miniapp", telegram_user_id,
        {"new_price_usd_cents": body.price_usd_cents},
    )
    return {"ok": True, "price_usd_cents": body.price_usd_cents}


# ── PUT /api/groups/{group_id}/wallet ────────────────────────────────────────

@app.put("/api/groups/{group_id}/wallet")
@limiter.limit("10/minute")
async def api_group_update_wallet(
    request: Request,
    group_id: int,
    body: UpdateWalletRequest,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Schedule a wallet address change with a WALLET_CHANGE_DELAY_HOURS delay.

    Does NOT update groups.payout_wallet_address immediately. Creates a
    pending_wallet_changes row instead. Any existing pending change for this
    group is automatically superseded — one pending change per group at a time.
    """
    from datetime import datetime, timezone, timedelta
    from renewise.config import WALLET_CHANGE_DELAY_HOURS

    telegram_user_id = user["id"]
    group = await verify_admin_or_403(telegram_user_id, group_id)

    if not await validate_ton_address(body.wallet_address):
        raise HTTPException(status_code=422, detail="Invalid TON wallet address format")

    # On mainnet, reject testnet addresses outright — payouts will never arrive.
    # On testnet, both address types are accepted.
    from renewise.services.wallet import detect_address_network
    from renewise.config import TONCENTER_TESTNET
    _addr_net = detect_address_network(body.wallet_address)
    if not TONCENTER_TESTNET and _addr_net == "testnet":
        raise HTTPException(
            status_code=422,
            detail=(
                "Testnet wallet address provided but the system is running on mainnet. "
                "Please use a mainnet address (EQ... or UQ...) to receive payouts."
            ),
        )

    # Passkey check — required when a passkey is already set on this group.
    # First-time wallet setup has no passkey, so it passes through freely.
    if not await queries.check_group_wallet_passcode(group_id, body.passcode):
        raise HTTPException(status_code=403, detail="Incorrect or missing passkey")

    old_wallet = group.get("payout_wallet_address")
    now_utc = datetime.now(timezone.utc)
    activates_at_str = (now_utc + timedelta(hours=WALLET_CHANGE_DELAY_HOURS)).strftime("%Y-%m-%d %H:%M:%S")

    change_id = await queries.create_pending_wallet_change(
        group_id=group_id,
        old_wallet=old_wallet,
        new_wallet=body.wallet_address,
        requested_by=telegram_user_id,
        activates_at=activates_at_str,
    )

    superseded = await queries.get_superseded_pending_wallet_change(group_id, telegram_user_id)
    if superseded and superseded["id"] != change_id:
        await queries.audit(
            group_id,
            "wallet_change_auto_cancelled",
            telegram_user_id,
            {
                "superseded_change_id": superseded["id"],
                "superseded_wallet":    superseded["new_wallet_address"],
                "reason":               "new_request_submitted_via_miniapp",
            },
        )

    await queries.audit(
        group_id,
        "wallet_change_requested_via_miniapp",
        telegram_user_id,
        {"change_id": change_id, "new_wallet": body.wallet_address},
    )

    from renewise.services.wallet import detect_address_network
    from renewise.config import TONCENTER_TESTNET
    addr_network = _addr_net or "unknown"
    # warn on raw addresses only (can't determine testnet/mainnet from raw form)
    warning = (
        "Raw address format — unable to verify network compatibility. "
        "Ensure this address is reachable on the correct network."
        if addr_network == "raw" else None
    )

    return {
        "ok":          True,
        "scheduled":   True,
        "change_id":   change_id,
        "activates_at": activates_at_str,
        "delay_hours": WALLET_CHANGE_DELAY_HOURS,
        "address_network": addr_network,
        "warning": warning,
        "message": (
            f"Change scheduled \u2014 activates in {WALLET_CHANGE_DELAY_HOURS} hours. "
            "You'll be notified via the bot."
        ),
    }


# ── POST /api/groups/{group_id}/passcode ─────────────────────────────────────

@app.post("/api/groups/{group_id}/passcode")
@limiter.limit("5/minute")
async def api_group_set_passcode(
    request: Request,
    group_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Set or change the 4-digit passkey that protects wallet changes for a group.

    - First time (no passkey set): only new_passcode required.
    - Changing: current_passcode must match the stored hash.
    Returns 400 for invalid format, 403 for wrong current passkey.
    """
    telegram_user_id = user["id"]
    await verify_admin_or_403(telegram_user_id, group_id)

    body = await request.json()
    new_passcode     = body.get("new_passcode", "")
    current_passcode = body.get("current_passcode") or None

    try:
        success = await queries.set_group_passcode(
            group_id, new_passcode, telegram_user_id, current_passcode
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not success:
        raise HTTPException(status_code=403, detail="Incorrect current passkey")

    return {"ok": True}


# ── POST /api/groups/{group_id}/wallet-change/{change_id}/cancel ─────────────

@app.post("/api/groups/{group_id}/wallet-change/{change_id}/cancel")
@limiter.limit("10/minute")
async def api_cancel_wallet_change(
    request: Request,
    group_id: int,
    change_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Cancel a pending wallet change from the Mini App.

    Mirrors cb_cancel_wallet_change in admin_menu.py.
    Returns 404 if change_id doesn't exist, 409 if already applied/cancelled.
    """
    telegram_user_id = user["id"]
    await verify_admin_or_403(telegram_user_id, group_id)

    change = await queries.get_pending_wallet_change(change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Wallet change not found")

    if change["group_id"] != group_id:
        raise HTTPException(status_code=403, detail="Unauthorized")

    actually_cancelled = await queries.cancel_pending_wallet_change(change_id, telegram_user_id)
    if not actually_cancelled:
        status_map = {"applied": "already been applied", "cancelled": "already been cancelled"}
        reason = status_map.get(change["status"], "no longer pending")
        raise HTTPException(status_code=409, detail=f"This wallet change has {reason}")

    await queries.audit(
        group_id,
        "wallet_change_cancelled_via_miniapp",
        telegram_user_id,
        {"change_id": change_id, "cancelled_wallet": change["new_wallet_address"]},
    )
    return {"ok": True, "change_id": change_id, "status": "cancelled"}


# ── GET /api/groups/{group_id}/wallet-change-pending ─────────────────────────

@app.get("/api/groups/{group_id}/wallet-change-pending")
@limiter.limit("60/minute")
async def api_wallet_change_pending(
    request: Request,
    group_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Returns the active pending wallet change for a group (if any).
    Returns {"pending": null} when no pending change exists.
    """
    telegram_user_id = user["id"]
    await verify_admin_or_403(telegram_user_id, group_id)

    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        row = await db.fetchrow(
            "SELECT id, new_wallet_address, activates_at, requested_at FROM pending_wallet_changes "
            "WHERE group_id = $1 AND status = 'pending' ORDER BY requested_at DESC LIMIT 1",
            group_id,
        )
    if not row:
        return {"pending": None}
    r = dict(row)
    r["change_id"] = r.pop("id")
    return {"pending": r}


# ── GET /api/groups/{group_id}/wallet-change-history ─────────────────────────

@app.get("/api/groups/{group_id}/wallet-change-history")
@limiter.limit("60/minute")
async def api_wallet_change_history(
    request: Request,
    group_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Returns all pending_wallet_changes rows for a group, newest first.
    Each row includes: id, old_wallet_address, new_wallet_address,
    requested_at, activates_at, status, cancelled_at.
    """
    telegram_user_id = user["id"]
    await verify_admin_or_403(telegram_user_id, group_id)

    rows = await queries.get_wallet_change_history(group_id)
    return {"wallet_change_history": [dict(r) for r in rows]}


# ── DELETE /api/groups/{group_id} ───────────────────────────────────────────

@app.delete("/api/groups/{group_id}")
@limiter.limit("5/minute")
async def api_group_delete(
    request: Request,
    group_id: int,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Permanently delete a group and all its subscriber records.

    Calls the SAME queries.delete_group function the bot uses — no
    reimplementation.  Sequence (mirrors the bot handler exactly):

      i.   Set groups.status = 'paused'  (stops new payment activity)
      ii.  DM all active/pending subscribers before leaving
      iii. Bot leaves the chat via Telegram API
      iv.  Audit + cancel pending wallet changes + cascade delete (one tx)

    processed_tx_hashes and vault_registry are NOT deleted.
    Requires verify_admin_or_403 — no exceptions.
    """
    telegram_user_id = user["id"]
    group = await verify_admin_or_403(telegram_user_id, group_id)

    chat_title = group.get("chat_title") or f"Chat {group['telegram_chat_id']}"
    telegram_chat_id = group["telegram_chat_id"]

    # i. Pause immediately
    await queries.set_group_status(group_id, "paused")

    # ii. Notify subscribers before leaving
    subscribers = await queries.get_active_subscribers(group_id)
    import aiohttp as _aiohttp
    for row in subscribers:
        try:
            async with _aiohttp.ClientSession() as session:
                await session.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": row["telegram_user_id"],
                        "text": (
                            f"\u2139\ufe0f Access to <b>{chat_title}</b> has ended "
                            "because the admin closed this paywall."
                        ),
                        "parse_mode": "HTML",
                    },
                    timeout=_aiohttp.ClientTimeout(total=5),
                )
        except OSError as exc:
            log.warning("delete: subscriber DM failed for %d: %s", row["telegram_user_id"], type(exc).__name__)

    # iii. Leave the chat
    try:
        async with _aiohttp.ClientSession() as session:
            await session.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/leaveChat",
                json={"chat_id": telegram_chat_id},
                timeout=_aiohttp.ClientTimeout(total=5),
            )
    except OSError as exc:
        log.warning("delete: leaveChat failed for %d: %s", telegram_chat_id, type(exc).__name__)

    # iv. Audit + cancel pending wallet changes + cascade delete
    await queries.delete_group(
        group_id=group_id,
        actor_id=telegram_user_id,
        group_title=chat_title,
        telegram_chat_id=telegram_chat_id,
    )

    log.info(
        "Network deleted via Mini App: group_id=%d chat_id=%d admin=%d",
        group_id, telegram_chat_id, telegram_user_id,
    )
    return {"ok": True, "deleted_group_id": group_id}


# ── POST /api/groups/{group_id}/comp ─────────────────────────────────────────

@app.post("/api/groups/{group_id}/comp")
@limiter.limit("10/minute")
async def api_group_comp(
    request: Request,
    group_id: int,
    body: CompMemberRequest,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Comp a member by telegram_user_id.
    Reuses the exact upsert_user + comp_subscription path from admin_menu.py.
    The Telegram join-request approval must happen via the bot — the Mini App
    cannot call approve_chat_join_request directly.
    """
    telegram_user_id = user["id"]
    await verify_admin_or_403(telegram_user_id, group_id)

    user_db_id = await queries.upsert_user(telegram_user_id=body.telegram_user_id)
    await queries.comp_subscription(user_db_id, group_id)
    await queries.audit(
        group_id, "member_comped_via_miniapp", telegram_user_id,
        {"comped_user_telegram_id": body.telegram_user_id},
    )
    return {"ok": True, "comped_telegram_user_id": body.telegram_user_id}


# ---------------------------------------------------------------------------
# Terms of Service — status check and acceptance
# Shared with the bot: both read/write the same users.terms_accepted_at column.
# The Mini App calls GET /api/terms-status on load; if not accepted it shows
# the ToS screen before rendering any other content.  POST /api/terms-accept
# records acceptance and the app continues.  If the user already accepted via
# the bot, GET returns accepted=true and the gate is skipped entirely.
# ---------------------------------------------------------------------------

@app.get("/api/terms-status")
@limiter.limit("60/minute")
async def api_terms_status(
    request: Request,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Returns whether the authenticated user has accepted the Terms of Service.

    Response:
        {"accepted": bool, "accepted_at": "<ISO datetime>" | null}

    The Mini App calls this once on init.  If accepted=false it must show the
    ToS screen and call POST /api/terms-accept before proceeding.
    """
    telegram_user_id: int = user["id"]

    # Ensure the user row exists (first open of Mini App before any bot interaction)
    first_name = user.get("first_name")
    username   = user.get("username")
    await queries.upsert_user(
        telegram_user_id=telegram_user_id,
        first_name=first_name,
        username=username,
    )

    accepted = await queries.has_accepted_terms(telegram_user_id)

    # Fetch the actual timestamp for audit / display purposes
    accepted_at: str | None = None
    if accepted:
        from renewise.db.connection import _db as _conn
        async with _conn() as db:
            row = await db.fetchrow(
                "SELECT terms_accepted_at FROM users WHERE telegram_user_id=$1",
                telegram_user_id,
            )
            if row:
                accepted_at = str(row["terms_accepted_at"]) if row["terms_accepted_at"] else None

    return {"accepted": accepted, "accepted_at": accepted_at}


@app.post("/api/terms-accept")
@limiter.limit("10/minute")
async def api_terms_accept(
    request: Request,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Record that the authenticated user has accepted the Terms of Service.

    Idempotent — calling it multiple times is safe; the original acceptance
    timestamp is preserved (COALESCE in the query layer).

    Response:
        {"ok": true, "accepted_at": "<ISO datetime>"}
    """
    telegram_user_id: int = user["id"]
    first_name = user.get("first_name")
    username   = user.get("username")

    # Ensure the user row exists before writing the timestamp
    await queries.upsert_user(
        telegram_user_id=telegram_user_id,
        first_name=first_name,
        username=username,
    )
    await queries.accept_terms(telegram_user_id)

    # Fetch the timestamp we just set (or the pre-existing one)
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        row = await db.fetchrow(
            "SELECT terms_accepted_at FROM users WHERE telegram_user_id=$1",
            telegram_user_id,
        )
        accepted_at = str(row["terms_accepted_at"]) if row and row["terms_accepted_at"] else None

    safe_at = (accepted_at or "").replace("\n", " ").replace("\r", " ")
    log.info("ToS accepted via Mini App: user=%d at=%s", telegram_user_id, safe_at)

    return {"ok": True, "accepted_at": accepted_at}


# ── GET /api/search/tx ────────────────────────────────────────────────────────

@app.get("/api/search/tx")
@limiter.limit("30/minute")
async def api_search_tx(
    request: Request,
    hash: str,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Admin TX-hash search.

    Looks up a subscription by its on-chain TX hash, scoped strictly to groups
    owned by the authenticated user. Returns the full payment-detail shape
    (same fields as GET /api/groups/{group_id}/payments/{sub_id}) so the
    frontend can render it with the existing PmtDetail sheet.
    """
    tx_hash = hash.strip()
    if not tx_hash or len(tx_hash) < 16:
        raise HTTPException(status_code=400, detail="Provide a valid TX hash")

    telegram_user_id = user["id"]

    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        # Find the subscription by tx hash, then verify the group belongs to this admin
        row = await db.fetchrow(
            """
            SELECT s.id, s.status, s.price_locked_in, s.start_date,
                   s.next_renewal_date, s.last_payment_tx_hash,
                   s.vault_address, s.required_nano_amount, s.amount_paid_so_far,
                   s.created_at, s.updated_at,
                   u.telegram_user_id, u.first_name, u.username,
                   g.chat_title, g.billing_interval_days, g.id AS group_id,
                   g.admin_telegram_id
            FROM subscriptions s
            JOIN users u ON u.id = s.user_id
            JOIN groups g ON g.id = s.group_id
            WHERE (s.last_payment_tx_hash = $1
                   OR s.id IN (
                       SELECT sub_id FROM processed_tx_hashes WHERE tx_hash = $1
                   ))
              AND g.admin_telegram_id = $2
            LIMIT 1
            """,
            tx_hash, telegram_user_id,
        )

    if not row:
        return {"found": False, "result": None}

    result = dict(row)
    result.pop("admin_telegram_id", None)
    tx = result.get("last_payment_tx_hash")
    result["tonviewer_url"] = f"https://tonviewer.com/{tx}" if tx else None
    nano = result.get("required_nano_amount")
    result["required_ton"] = round(nano / 1e9, 9) if nano else None

    from renewise.db.queries import get_global_fees
    global_buyer_bps, global_admin_bps = await get_global_fees()
    group_row = await queries.get_group_by_id(result["group_id"])
    admin_fee_bps = (
        group_row["admin_fee_bps"]
        if group_row and group_row["admin_fee_bps"] is not None
        else global_admin_bps
    )
    buyer_fee_bps = (
        group_row["buyer_fee_bps"]
        if group_row and group_row["buyer_fee_bps"] is not None
        else global_buyer_bps
    )
    result["admin_fee_pct"]    = admin_fee_bps / 100.0
    result["buyer_fee_pct"]    = buyer_fee_bps / 100.0
    result["platform_fee_pct"] = (admin_fee_bps + buyer_fee_bps) / 100.0
    if nano:
        result["admin_payout_ton"] = round((nano / 1e9) * (1 - admin_fee_bps / 10000.0), 9)
    else:
        result["admin_payout_ton"] = None

    return {"found": True, "result": result}


# ── GET /api/developer/search ─────────────────────────────────────────────────

@app.get("/api/developer/search")
@limiter.limit("30/minute")
async def api_developer_search(
    request: Request,
    q: str,
    user: Annotated[dict, Depends(get_telegram_user)],
) -> dict:
    """
    Developer charge search.

    Searches platform_charges by external_reference (exact) or tx_hash
    (exact), scoped to platforms owned by the authenticated user. Returns
    all matching charges with the full charge shape (same as GET
    /api/platform/charges/{id}) plus platform_name for display.
    """
    query_str = q.strip()
    if not query_str or len(query_str) < 3:
        raise HTTPException(status_code=400, detail="Query must be at least 3 characters")

    telegram_user_id = user["id"]

    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        rows = await db.fetch(
            """
            SELECT pc.id, pc.external_reference, pc.mode, pc.amount_usd_cents,
                   pc.status, pc.vault_address, pc.payment_url, pc.required_nano_amount,
                   pc.tx_hash, pc.created_at, pc.completed_at,
                   p.platform_name
            FROM platform_charges pc
            JOIN platforms p ON p.id = pc.platform_id
            WHERE p.owner_telegram_id = $1
              AND (pc.external_reference = $2 OR pc.tx_hash = $2)
            ORDER BY pc.created_at DESC
            LIMIT 20
            """,
            telegram_user_id, query_str,
        )

    if not rows:
        return {"found": False, "results": []}

    base_url = str(request.base_url).rstrip('/')
    results = []
    for row in rows:
        r = dict(row)
        r["required_nano"] = r.pop("required_nano_amount", None)
        r["checkout_url"] = f"{base_url}/checkout/{r['id']}"
        results.append(r)

    return {"found": True, "results": results}


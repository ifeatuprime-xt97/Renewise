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

app.mount("/static", StaticFiles(directory="renewise/miniapp/static"), name="static")


# ---------------------------------------------------------------------------
# Static / config routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root() -> FileResponse:
    return FileResponse("renewise/miniapp/static/index.html")


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
        price=price_usd,
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
                            price=group.get("price") or 0,
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
        "revenue_nano":          0,  # deprecated — use revenue_usd_cents
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

    return {
        "ok":          True,
        "scheduled":   True,
        "change_id":   change_id,
        "activates_at": activates_at_str,
        "delay_hours": WALLET_CHANGE_DELAY_HOURS,
        "message": (
            f"Change scheduled \u2014 activates in {WALLET_CHANGE_DELAY_HOURS} hours. "
            "You'll be notified via the bot."
        ),
    }


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

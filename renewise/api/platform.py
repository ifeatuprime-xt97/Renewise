import hashlib
from fastapi import APIRouter, Header, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Annotated
from cachetools import TTLCache

from renewise.db.connection import _db
from renewise.services.payment import generate_platform_payment_request

platform_router = APIRouter(prefix="/api/platform")
auth_cache = TTLCache(maxsize=1000, ttl=60)

async def authenticate_platform(authorization: Annotated[str, Header()]) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    
    secret_key = authorization[7:]

    # Reject blank or clearly invalid keys immediately (e.g. after test key deletion)
    if not secret_key or len(secret_key) < 8:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    if secret_key in auth_cache:
        platform = auth_cache[secret_key]
        if platform["status"] == "revoked":
            raise HTTPException(status_code=401, detail="Platform keys have been revoked")
        return platform

    is_test = secret_key.startswith("sk_test_")
    
    async with _db() as db:
        if is_test:
            row = await db.fetchrow(
                "SELECT * FROM platforms WHERE secret_key_test = $1", 
                secret_key
            )
        else:
            secret_hash = hashlib.sha256(secret_key.encode("utf-8")).hexdigest()
            row = await db.fetchrow(
                "SELECT * FROM platforms WHERE secret_key_live_hash = $1", 
                secret_hash
            )
            
        if not row:
            raise HTTPException(status_code=401, detail="Invalid API key")
            
        platform = dict(row)
        platform["auth_mode"] = "test" if is_test else "live"
            
        auth_cache[secret_key] = platform
        
        if platform["status"] == "revoked":
            raise HTTPException(status_code=401, detail="Platform keys have been revoked")
            
        return platform

class CreateChargeRequest(BaseModel):
    amount_usd_cents: int
    external_reference: str

@platform_router.post("/charges")
async def create_charge(
    request: Request,
    req: CreateChargeRequest,
    platform: dict = Depends(authenticate_platform)
):
    if req.amount_usd_cents <= 0:
        raise HTTPException(status_code=400, detail="amount_usd_cents must be positive")
    if not platform.get("wallet_address"):
        raise HTTPException(status_code=400, detail="Platform has no wallet_address configured")
        
    auth_mode = platform["auth_mode"]
        
    async with _db() as db:
        # Check for duplicate external_reference
        existing = await db.fetchrow(
            "SELECT id FROM platform_charges WHERE platform_id = $1 AND external_reference = $2",
            platform["id"], req.external_reference
        )
        if existing:
            raise HTTPException(status_code=409, detail="Charge with this external_reference already exists")
            
        # Create pending charge to get an ID
        row = await db.fetchrow(
            """
            INSERT INTO platform_charges (platform_id, external_reference, mode, amount_usd_cents, status, expires_at)
            VALUES ($1, $2, $3, $4, 'pending', NOW() + INTERVAL '30 minutes')
            RETURNING id
            """,
            platform["id"], req.external_reference, auth_mode, req.amount_usd_cents
        )
        charge_id = row["id"]
        
    # Generate payment request.
    # When INTERNAL_API_URL is set (Vercel deployment), the .boc and wallet
    # config live on Render — proxy the generation there. Otherwise generate locally.
    import os as _os
    _internal_url = _os.environ.get("INTERNAL_API_URL", "").rstrip("/")
    _internal_secret = _os.environ.get("INTERNAL_API_SECRET", "")

    if _internal_url and _internal_secret:
        # ── Proxy to Render bot service ──────────────────────────────────────
        import aiohttp as _aiohttp
        from datetime import datetime as _dt

        def _json_safe(v):
            """Convert values that aren't JSON-serializable to strings."""
            if isinstance(v, _dt):
                return v.isoformat()
            return v

        _platform_payload = {
            k: _json_safe(v)
            for k, v in platform.items()
            if k != "auth_mode"
        }

        try:
            async with _aiohttp.ClientSession() as _session:
                async with _session.post(
                    f"{_internal_url}/internal/generate-payment-link",
                    json={
                        "platform":        _platform_payload,
                        "charge_id":       charge_id,
                        "price_usd_cents": req.amount_usd_cents,
                    },
                    headers={"X-Internal-Secret": _internal_secret},
                    timeout=_aiohttp.ClientTimeout(total=15),
                ) as _resp:
                    _data = await _resp.json()
                    if _resp.status != 200:
                        raise HTTPException(
                            status_code=_resp.status,
                            detail=_data.get("error", f"Internal service error {_resp.status}"),
                        )
                    from renewise.services.payment import PaymentRequest
                    payment_req = PaymentRequest(
                        payment_url=_data["payment_url"],
                        vault_address=_data["vault_address"],
                        required_nano=_data["required_nano"],
                        payload=_data["vault_address"],
                        amount=_data["required_nano"] / 1e9,
                        currency="TON",
                    )
        except HTTPException:
            raise
        except Exception as _e:
            import logging as _logging
            _logging.getLogger(__name__).exception("Proxy to internal service failed for charge %s", charge_id)
            # Give a more specific message based on the error type
            _ename = type(_e).__name__
            if "ContentType" in _ename or "JSON" in _ename:
                _detail = "Internal service returned a non-JSON response — Render may not be running the latest code with the /internal/generate-payment-link endpoint."
            elif "ClientConnectorError" in _ename or "ServerDisconnectedError" in _ename:
                _detail = "Could not reach the internal Render service. Check INTERNAL_API_URL and that Render is running."
            elif "TimeoutError" in _ename or "asyncio" in _ename:
                _detail = "Internal service timed out. Render may be cold-starting."
            else:
                _detail = f"Payment service unavailable ({_ename}). Check Render logs."
            raise HTTPException(status_code=503, detail=_detail)
    else:
        # ── Local generation (Render or local dev) ────────────────────────────
        try:
            payment_req = await generate_platform_payment_request(
                platform=platform,
                charge_id=charge_id,
                price_usd_cents=req.amount_usd_cents,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError:
            raise HTTPException(
                status_code=503,
                detail="Payment contract not compiled. Run: cd contracts && npm install && npm run build",
            )
        except Exception as e:
            import logging as _logging
            _logging.getLogger(__name__).exception("Payment link generation failed for charge %s", charge_id)
            raise HTTPException(
                status_code=500,
                detail=f"Payment link generation failed ({type(e).__name__}). Check server logs.",
            )
        
    # Update charge with vault details
    async with _db() as db:
        # Store the effective fees actually used for this charge (per-platform
        # override if set, otherwise global defaults).
        from renewise.db.queries import get_global_fees
        global_buyer, global_admin = await get_global_fees()
        buyer_fee = platform.get("buyer_fee_bps") if platform.get("buyer_fee_bps") is not None else global_buyer
        admin_fee = platform.get("admin_fee_bps") if platform.get("admin_fee_bps") is not None else global_admin
        
        await db.execute(
            """
            UPDATE platform_charges 
            SET vault_address = $1, required_nano_amount = $2, buyer_fee_bps = $3, platform_fee_bps = $4,
                payment_url = $5
            WHERE id = $6
            """,
            payment_req.vault_address, payment_req.required_nano, buyer_fee, admin_fee,
            payment_req.payment_url, charge_id
        )
        
        # Audit
        await db.execute(
            "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id, details) VALUES ($1, $2, $3, $4)",
            platform["id"], "charge_created", platform["owner_telegram_id"], f"charge_id={charge_id}"
        )

    base_url = str(request.base_url).rstrip('/')
    return {
        "id": charge_id,
        "external_reference": req.external_reference,
        "amount_usd_cents": req.amount_usd_cents,
        "required_nano": payment_req.required_nano,
        "vault_address": payment_req.vault_address,
        "payment_url": payment_req.payment_url,
        "status": "pending",
        "checkout_url": f"{base_url}/checkout/{charge_id}"
    }

@platform_router.get("/charges/{charge_id}")
async def get_charge(
    request: Request,
    charge_id: int,
    platform: dict = Depends(authenticate_platform)
):
    async with _db() as db:
        row = await db.fetchrow(
            "SELECT * FROM platform_charges WHERE id = $1 AND platform_id = $2",
            charge_id, platform["id"]
        )
        if not row:
            raise HTTPException(status_code=404, detail="Charge not found")
        
        # Auto-expire if past expiration time
        status = row["status"]
        if status == "pending" and row.get("expires_at"):
            from datetime import datetime, timezone
            expires_at = row["expires_at"]
            # Handle both naive and aware datetimes
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            
            if now > expires_at:
                # Mark as expired
                await db.execute(
                    "UPDATE platform_charges SET status = 'expired' WHERE id = $1",
                    charge_id
                )
                status = "expired"
            
        base_url = str(request.base_url).rstrip('/')
        return {
            "id": row["id"],
            "external_reference": row["external_reference"],
            "amount_usd_cents": row["amount_usd_cents"],
            "required_nano": row["required_nano_amount"],
            "vault_address": row["vault_address"],
            "payment_url": row["payment_url"],
            "status": status,
            "checkout_url": f"{base_url}/checkout/{row['id']}",
            "tx_hash": row["tx_hash"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }

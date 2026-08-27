import hashlib
from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel
from typing import Annotated
from cachetools import TTLCache

from renewise.db.connection import _db
from renewise.services.payment import generate_platform_payment_request

platform_router = APIRouter(prefix="/v1")
auth_cache = TTLCache(maxsize=1000, ttl=60)

async def authenticate_platform(authorization: Annotated[str, Header()]) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    
    secret_key = authorization[7:]
    
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
            INSERT INTO platform_charges (platform_id, external_reference, mode, amount_usd_cents, status)
            VALUES ($1, $2, $3, $4, 'pending')
            RETURNING id
            """,
            platform["id"], req.external_reference, auth_mode, req.amount_usd_cents
        )
        charge_id = row["id"]
        
    # Generate payment request
    try:
        payment_req = await generate_platform_payment_request(
            platform=platform,
            charge_id=charge_id,
            price_usd_cents=req.amount_usd_cents,
            mode=auth_mode
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
        
    # Update charge with vault details
    async with _db() as db:
        # Get global fees used during generation (for auditing/accounting)
        from renewise.db.queries import get_global_fees
        buyer_fee, admin_fee = await get_global_fees()
        
        await db.execute(
            """
            UPDATE platform_charges 
            SET vault_address = $1, required_nano_amount = $2, buyer_fee_bps = $3, platform_fee_bps = $4
            WHERE id = $5
            """,
            payment_req.vault_address, payment_req.required_nano, buyer_fee, admin_fee, charge_id
        )
        
        # Audit
        await db.execute(
            "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id, details) VALUES ($1, $2, $3, $4)",
            platform["id"], "charge_created", platform["owner_telegram_id"], f"charge_id={charge_id}"
        )

    return {
        "id": charge_id,
        "external_reference": req.external_reference,
        "amount_usd_cents": req.amount_usd_cents,
        "status": "pending",
        "payment_url": payment_req.payment_url,
        "vault_address": payment_req.vault_address,
        "required_nano": payment_req.required_nano
    }

@platform_router.get("/charges/{charge_id}")
async def get_charge(
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
            
        return {
            "id": row["id"],
            "external_reference": row["external_reference"],
            "amount_usd_cents": row["amount_usd_cents"],
            "status": row["status"],
            "vault_address": row["vault_address"],
            "required_nano": row["required_nano_amount"],
            "tx_hash": row["tx_hash"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }

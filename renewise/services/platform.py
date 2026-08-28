import os
import hashlib
import bcrypt
import aiohttp
from renewise.db.connection import _db
from renewise.config import BOT_TOKEN

async def _notify_live_keys(actor_telegram_id: int, platform_name: str):
    if not BOT_TOKEN:
        return
    text = (
        f"⚠️ <b>Live Keys Generated</b>\n\n"
        f"Live keys were just generated for your platform <b>{platform_name}</b>.\n"
        "Your live secret key was displayed in the dashboard and will never be shown again.\n\n"
        "<i>If you did not request this, please revoke your keys immediately.</i>"
    )
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": actor_telegram_id,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, json=payload, timeout=5.0)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to notify dev of live key generation: {e}")


def _hash_secret(secret: str) -> str:
    """Hashes the secret key using SHA256 for secure storage."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()

def _hash_passcode(passcode: str) -> str:
    """Hashes a 4-digit passcode using bcrypt."""
    return bcrypt.hashpw(passcode.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def _verify_passcode(passcode: str, hashed: str) -> bool:
    """Verifies a 4-digit passcode against its bcrypt hash."""
    return bcrypt.checkpw(passcode.encode("utf-8"), hashed.encode("utf-8"))

async def create_platform(owner_telegram_id: int, platform_name: str) -> tuple[dict, str]:
    """
    Creates a new platform with TEST keys only. Live keys are opt-in.
    Returns the platform details and the RAW test secret key.
    """
    publishable_key_test = f"pk_test_{os.urandom(16).hex()}"
    raw_secret_key_test = f"sk_test_{os.urandom(32).hex()}"
    
    async with _db() as db:
        row = await db.fetchrow(
            """
            INSERT INTO platforms (
                owner_telegram_id, platform_name, 
                publishable_key_live, secret_key_live_hash,
                publishable_key_test, secret_key_test
            )
            VALUES ($1, $2, NULL, NULL, $3, $4)
            RETURNING id
            """,
            owner_telegram_id, platform_name, 
            publishable_key_test, raw_secret_key_test
        )
        platform_id = row["id"]

        await db.execute(
            "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id) VALUES ($1, $2, $3)",
            platform_id, "create_platform", owner_telegram_id
        )

        platform = {
            "id": platform_id,
            "owner_telegram_id": owner_telegram_id,
            "platform_name": platform_name,
            "publishable_key_live": None,
            "publishable_key_test": publishable_key_test,
        }

    return platform, raw_secret_key_test


async def generate_live_keys(platform_id: int, actor_telegram_id: int) -> tuple[str, str]:
    """
    Generates live keys for a platform for the first time.
    Returns (publishable_key_live, raw_secret_key_live).
    """
    publishable_key = f"pk_live_{os.urandom(16).hex()}"
    raw_secret = f"sk_live_{os.urandom(32).hex()}"
    secret_hash = _hash_secret(raw_secret)

    async with _db() as db:
        # Ensure they aren't already generated
        existing = await db.fetchrow("SELECT publishable_key_live, platform_name FROM platforms WHERE id = $1", platform_id)
        if existing and existing["publishable_key_live"]:
            raise ValueError("Live keys already exist for this platform")
        
        platform_name = existing["platform_name"] if existing else "Unknown"

        await db.execute(
            "UPDATE platforms SET publishable_key_live = $1, secret_key_live_hash = $2 WHERE id = $3",
            publishable_key, secret_hash, platform_id
        )
        await db.execute(
            "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id) VALUES ($1, $2, $3)",
            platform_id, "generate_live_keys", actor_telegram_id
        )

    import asyncio
    asyncio.create_task(_notify_live_keys(actor_telegram_id, platform_name))

    return publishable_key, raw_secret


async def regenerate_platform_keys(platform_id: int, mode: str, actor_telegram_id: int) -> str:
    """
    Rotates the secret key for the specified mode ('test' or 'live').
    Returns the new raw secret key.
    - Test mode: stores raw key
    - Live mode: stores hashed key
    """
    if mode not in ("test", "live"):
        raise ValueError("Mode must be 'test' or 'live'")

    raw_secret_key = f"sk_{mode}_{os.urandom(32).hex()}"
    
    async with _db() as db:
        if mode == "live":
            # Live keys must already exist to be rotated
            existing = await db.fetchrow("SELECT publishable_key_live FROM platforms WHERE id = $1", platform_id)
            if not existing or not existing["publishable_key_live"]:
                raise ValueError("Cannot rotate live keys before they are generated")
                
            secret_val = _hash_secret(raw_secret_key)
            col_name = "secret_key_live_hash"
        else:
            secret_val = raw_secret_key
            col_name = "secret_key_test"

        await db.execute(
            f"UPDATE platforms SET {col_name} = $1 WHERE id = $2",
            secret_val, platform_id
        )
        
        await db.execute(
            "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id, details) VALUES ($1, $2, $3, $4)",
            platform_id, "rotate_secret_key", actor_telegram_id, f"mode={mode}"
        )

    return raw_secret_key

async def revoke_platform(platform_id: int, actor_telegram_id: int) -> None:
    """Revokes a platform's keys, preventing new charges from being created."""
    async with _db() as db:
        await db.execute(
            "UPDATE platforms SET status = 'revoked' WHERE id = $1",
            platform_id
        )
        await db.execute(
            "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id) VALUES ($1, $2, $3)",
            platform_id, "revoke_platform", actor_telegram_id
        )

async def get_platform_by_publishable_key(publishable_key: str) -> tuple[dict | None, str | None]:
    """
    Retrieves a platform by its publishable key, primarily for API authentication.
    Returns (platform_dict, mode) where mode is 'live' or 'test', or (None, None) if not found.
    """
    async with _db() as db:
        # Check live key
        if publishable_key.startswith("pk_live_"):
            row = await db.fetchrow("SELECT * FROM platforms WHERE publishable_key_live = $1 AND status = 'active'", publishable_key)
            return (dict(row), 'live') if row else (None, None)
        # Check test key
        elif publishable_key.startswith("pk_test_"):
            row = await db.fetchrow("SELECT * FROM platforms WHERE publishable_key_test = $1 AND status = 'active'", publishable_key)
            return (dict(row), 'test') if row else (None, None)
            
        return None, None

async def get_user_platforms(owner_telegram_id: int) -> list[dict]:
    """Retrieves all active platforms owned by a specific telegram user."""
    async with _db() as db:
        rows = await db.fetch(
            "SELECT id, owner_telegram_id, platform_name, publishable_key_live, publishable_key_test, "
            "secret_key_test, wallet_address, wallet_passcode_hash, status, created_at "
            "FROM platforms WHERE owner_telegram_id = $1 AND status = 'active' ORDER BY created_at DESC",
            owner_telegram_id
        )
        return [dict(r) for r in rows]

async def set_platform_passcode(platform_id: int, new_passcode: str, actor_telegram_id: int, current_passcode: str | None = None) -> bool:
    """
    Sets or changes the 4-digit passcode for a platform.
    If a passcode already exists, current_passcode must match.
    Returns True on success, False if the current_passcode is wrong.
    """
    if not (new_passcode.isdigit() and len(new_passcode) == 4):
        raise ValueError("Passcode must be a 4-digit number")

    async with _db() as db:
        row = await db.fetchrow("SELECT wallet_passcode_hash FROM platforms WHERE id = $1", platform_id)
        if not row:
            return False

        existing_hash = row["wallet_passcode_hash"]
        if existing_hash:
            if not current_passcode or not _verify_passcode(current_passcode, existing_hash):
                # Failed attempt audit log
                await db.execute(
                    "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id, details) VALUES ($1, $2, $3, $4)",
                    platform_id, "passcode_change_failed", actor_telegram_id, "Incorrect current passcode"
                )
                return False

        new_hash = _hash_passcode(new_passcode)
        await db.execute("UPDATE platforms SET wallet_passcode_hash = $1 WHERE id = $2", new_hash, platform_id)
        
        # Audit log
        action = "passcode_changed" if existing_hash else "passcode_set"
        await db.execute(
            "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id) VALUES ($1, $2, $3)",
            platform_id, action, actor_telegram_id
        )
        return True

async def update_platform_wallet(platform_id: int, wallet_address: str, actor_telegram_id: int, passcode: str | None = None) -> bool:
    """
    Updates the wallet address for a platform.
    If a wallet address is ALREADY set, requires a valid passcode.
    Returns True on success, False if the passcode is required but incorrect/missing.
    """
    async with _db() as db:
        row = await db.fetchrow("SELECT wallet_address, wallet_passcode_hash FROM platforms WHERE id = $1", platform_id)
        if not row:
            return False
            
        current_wallet = row["wallet_address"]
        existing_hash = row["wallet_passcode_hash"]
        
        # Friction only applies to CHANGING an existing wallet
        if current_wallet:
            if not passcode or not existing_hash or not _verify_passcode(passcode, existing_hash):
                await db.execute(
                    "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id, details) VALUES ($1, $2, $3, $4)",
                    platform_id, "update_wallet_failed", actor_telegram_id, "Incorrect or missing passcode"
                )
                return False

        await db.execute("UPDATE platforms SET wallet_address = $1 WHERE id = $2", wallet_address, platform_id)
        await db.execute(
            "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id, details) VALUES ($1, $2, $3, $4)",
            platform_id, "update_wallet", actor_telegram_id, f'{{"wallet_address": "{wallet_address}"}}'
        )
        return True

async def get_platform_charges(platform_id: int, status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    """Retrieves paginated charges for a specific platform, optionally filtered by status."""
    async with _db() as db:
        if status:
            rows = await db.fetch(
                "SELECT id, external_reference, mode, amount_usd_cents, status, vault_address, "
                "tx_hash, required_nano_amount, created_at, completed_at "
                "FROM platform_charges WHERE platform_id = $1 AND status = $2 ORDER BY created_at DESC LIMIT $3 OFFSET $4",
                platform_id, status, limit, offset
            )
        else:
            rows = await db.fetch(
                "SELECT id, external_reference, mode, amount_usd_cents, status, vault_address, "
                "tx_hash, required_nano_amount, created_at, completed_at "
                "FROM platform_charges WHERE platform_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                platform_id, limit, offset
            )
        return [dict(r) for r in rows]

async def get_all_user_platform_charges(owner_telegram_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
    """Retrieves paginated charges across all platforms owned by the user."""
    async with _db() as db:
        rows = await db.fetch(
            "SELECT c.id, c.platform_id, p.platform_name, c.external_reference, c.mode, c.amount_usd_cents, c.status, "
            "c.vault_address, c.tx_hash, c.required_nano_amount, c.created_at, c.completed_at "
            "FROM platform_charges c "
            "JOIN platforms p ON c.platform_id = p.id "
            "WHERE p.owner_telegram_id = $1 "
            "ORDER BY c.created_at DESC LIMIT $2 OFFSET $3",
            owner_telegram_id, limit, offset
        )
        return [dict(r) for r in rows]

async def get_platform_stats(platform_id: int) -> dict:
    """Aggregates total charges, volume, and success rate for a platform."""
    async with _db() as db:
        row = await db.fetchrow(
            """
            SELECT 
                COUNT(*) as total_charges,
                SUM(CASE WHEN status = 'completed' THEN amount_usd_cents ELSE 0 END) as total_volume_cents,
                COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed_charges
            FROM platform_charges 
            WHERE platform_id = $1
            """, platform_id
        )
        total_charges = row["total_charges"] or 0
        total_volume = (row["total_volume_cents"] or 0) / 100.0
        completed = row["completed_charges"] or 0
        success_rate = (completed / total_charges * 100) if total_charges > 0 else 0.0
        
        return {
            "total_charges": total_charges,
            "total_volume_usd": total_volume,
            "success_rate_pct": success_rate
        }

async def regenerate_platform_keys(platform_id: int, mode: str, actor_telegram_id: int) -> str:
    """Regenerates the secret key for a specific mode and invalidates the old one."""
    new_raw_secret = f"sk_{mode}_{os.urandom(32).hex()}"
    new_hash = _hash_secret(new_raw_secret)
    
    async with _db() as db:
        if mode == 'live':
            await db.execute("UPDATE platforms SET secret_key_live_hash = $1 WHERE id = $2", new_hash, platform_id)
        elif mode == 'test':
            await db.execute("UPDATE platforms SET secret_key_test_hash = $1 WHERE id = $2", new_hash, platform_id)
        else:
            raise ValueError("Mode must be 'live' or 'test'")
            
        await db.execute(
            "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id, details) VALUES ($1, $2, $3, $4)",
            platform_id, "regenerate_keys", actor_telegram_id, mode
        )
    return new_raw_secret

async def get_platform_webhook(platform_id: int) -> dict | None:
    """Gets the active webhook endpoint and its recent delivery stats."""
    async with _db() as db:
        ep = await db.fetchrow("SELECT * FROM webhook_endpoints WHERE platform_id = $1 AND active = 1", platform_id)
        if not ep:
            return None
        
        # Get recent deliveries
        deliveries = await db.fetch(
            "SELECT status, response_status_code, created_at, charge_id FROM webhook_deliveries "
            "WHERE webhook_endpoint_id = $1 ORDER BY created_at DESC LIMIT 5",
            ep["id"]
        )
        
        res = dict(ep)
        res["recent_deliveries"] = [dict(d) for d in deliveries]
        # Obfuscate secret slightly
        res["secret_preview"] = f"{res['secret'][:4]}...{res['secret'][-4:]}"
        return res

async def set_platform_webhook(platform_id: int, url: str, actor_telegram_id: int) -> dict:
    """
    Sets or updates the webhook URL.

    - If no endpoint exists yet: creates a new one and generates a fresh
      whsec_ signing secret (returned to the caller — shown only once).
    - If an endpoint already exists: updates the URL only; the existing secret
      is PRESERVED so the developer's server does not break.

    Returns {"secret": <raw_secret_or_None>, "is_new": bool}.
    Only a non-None secret needs to be shown to the developer.
    """
    async with _db() as db:
        existing = await db.fetchrow(
            "SELECT id, secret FROM webhook_endpoints WHERE platform_id = $1 AND active = 1",
            platform_id,
        )

        if existing:
            # URL-only update — secret stays the same
            await db.execute(
                "UPDATE webhook_endpoints SET url = $1 WHERE id = $2",
                url, existing["id"],
            )
            await db.execute(
                "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id, details) "
                "VALUES ($1, $2, $3, $4)",
                platform_id, "webhook_url_updated", actor_telegram_id, url,
            )
            return {"secret": None, "is_new": False}

        # First-time setup — generate secret
        # Deactivate any stale inactive rows just in case
        await db.execute(
            "UPDATE webhook_endpoints SET active = 0 WHERE platform_id = $1", platform_id
        )
        secret = f"whsec_{os.urandom(24).hex()}"
        await db.execute(
            "INSERT INTO webhook_endpoints (platform_id, url, secret, active) VALUES ($1, $2, $3, 1)",
            platform_id, url, secret,
        )
        await db.execute(
            "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id, details) "
            "VALUES ($1, $2, $3, $4)",
            platform_id, "webhook_created", actor_telegram_id, url,
        )
        return {"secret": secret, "is_new": True}


async def rotate_webhook_secret(platform_id: int, actor_telegram_id: int) -> str:
    """
    Explicitly rotates the signing secret for the active webhook endpoint.
    Returns the new raw secret (shown to the developer once).
    """
    new_secret = f"whsec_{os.urandom(24).hex()}"
    async with _db() as db:
        result = await db.execute(
            "UPDATE webhook_endpoints SET secret = $1 WHERE platform_id = $2 AND active = 1",
            new_secret, platform_id,
        )
        if result == "UPDATE 0":
            raise ValueError("No active webhook endpoint found for this platform")
        await db.execute(
            "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id) "
            "VALUES ($1, $2, $3)",
            platform_id, "webhook_secret_rotated", actor_telegram_id,
        )
    return new_secret


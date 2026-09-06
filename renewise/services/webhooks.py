"""
Webhook dispatcher for platform charges.

Delivery model
--------------
dispatch_webhook() is a fire-and-forget coroutine: the caller creates an
asyncio background task so the HTTP round-trip does not block the watcher
worker or the API response.

Retry policy (no Redis/RQ required)
------------------------------------
Each endpoint gets up to MAX_ATTEMPTS deliveries with exponential back-off:
  attempt 1: immediate
  attempt 2: 10 s
  attempt 3: 30 s
Each attempt is recorded in webhook_deliveries with status 'retrying',
'success', or 'failed'.  The final attempt always writes a terminal status.
"""
from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import logging

import aiohttp

from renewise.db.connection import _db

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
# Delays before each retry (seconds); index = attempt number (0-based)
_RETRY_DELAYS = [0, 10, 30]
_DELIVERY_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def _deliver_once(
    session: aiohttp.ClientSession,
    ep: dict,
    payload: bytes,
    charge_id: int,
    attempt: int,
    is_last: bool,
) -> bool:
    """Send one HTTP POST.  Returns True on success, False on failure."""
    secret = ep["secret"].encode("utf-8")
    signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "Renewise-Signature": signature,
    }

    try:
        async with session.post(
            ep["url"], data=payload, headers=headers, timeout=_DELIVERY_TIMEOUT
        ) as resp:
            status_code = resp.status
            success = resp.status < 400
            status_str = "success" if success else ("failed" if is_last else "retrying")
            log.log(
                logging.INFO if success else logging.WARNING,
                "Webhook delivery attempt %d/%d to %s → HTTP %d (%s)",
                attempt + 1, MAX_ATTEMPTS, ep["url"], status_code, status_str,
            )
            async with _db() as db:
                await db.execute(
                    "INSERT INTO webhook_deliveries "
                    "(webhook_endpoint_id, charge_id, status, response_status_code) "
                    "VALUES ($1, $2, $3, $4)",
                    ep["id"], charge_id, status_str, status_code,
                )
            return success
    except Exception as exc:
        log.warning(
            "Webhook delivery attempt %d/%d to %s failed: %s",
            attempt + 1, MAX_ATTEMPTS, ep["url"], exc,
        )
        status_str = "failed" if is_last else "retrying"
        async with _db() as db:
            await db.execute(
                "INSERT INTO webhook_deliveries "
                "(webhook_endpoint_id, charge_id, status, response_status_code) "
                "VALUES ($1, $2, $3, $4)",
                ep["id"], charge_id, status_str, None,
            )
        return False


async def _deliver_with_retry(ep: dict, payload: bytes, charge_id: int) -> None:
    """Attempt delivery up to MAX_ATTEMPTS times with exponential back-off."""
    async with aiohttp.ClientSession() as session:
        for attempt in range(MAX_ATTEMPTS):
            delay = _RETRY_DELAYS[attempt]
            if delay:
                await asyncio.sleep(delay)
            is_last = attempt == MAX_ATTEMPTS - 1
            success = await _deliver_once(session, ep, payload, charge_id, attempt, is_last)
            if success:
                return  # done — no further retries needed


async def dispatch_webhook(charge_id: int) -> None:
    """
    Fire-and-forget: fetch charge + endpoints, then kick off a background
    asyncio task for each endpoint with built-in retry logic.

    Callers should use:
        asyncio.create_task(dispatch_webhook(charge_id))
    so the webhook delivery does not block the calling coroutine.
    """
    async with _db() as db:
        charge = await db.fetchrow("SELECT * FROM platform_charges WHERE id = $1", charge_id)
        if not charge:
            return

        platform_id = charge["platform_id"]
        endpoints = await db.fetch(
            "SELECT * FROM webhook_endpoints WHERE platform_id = $1 AND active = TRUE",
            platform_id,
        )

    if not endpoints:
        return

    payload = json.dumps({
        "id": charge["id"],
        "external_reference": charge["external_reference"],
        "status": charge["status"],
        "amount_usd_cents": charge["amount_usd_cents"],
        "tx_hash": charge["tx_hash"],
        "mode": charge["mode"],
    }).encode("utf-8")

    for ep in endpoints:
        asyncio.create_task(
            _deliver_with_retry(dict(ep), payload, charge_id),
            name=f"webhook-{ep['id']}-charge-{charge_id}",
        )

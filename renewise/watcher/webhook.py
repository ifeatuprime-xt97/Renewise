"""
renewise/watcher/webhook.py

aiohttp webhook server.

TonCenter (and compatible blockchain indexers) can POST a JSON payload to this server
whenever a confirmed transaction arrives on a subscribed address.

Endpoint: POST /webhook
Headers:  X-Webhook-Token: <WEBHOOK_SECRET>   (verified if WEBHOOK_SECRET is set)

Payload (TonCenter format):
{
  "account_id": "EQ...",          ← vault address
  "transaction_id": {
    "hash": "...",
    "lt": "..."
  },
  "in_msg": {
    "value": "1050000000"         ← nanogram
  }
}

The server also exposes:
  GET  /health          — liveness probe
  POST /register        — register a vault address for polling fallback
  POST /recheck         — manual recheck (Phase 4 hook)

Run:
    python -m renewise.watcher.webhook
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import signal

import redis as redis_sync
from aiohttp import web
from rq import Queue

from renewise.watcher.config import (
    REDIS_URL,
    RQ_QUEUE_NAME,
    WEBHOOK_HOST,
    WEBHOOK_PORT,
    WEBHOOK_SECRET,
    POLL_INTERVAL_SECONDS,
)
from renewise.watcher.db import migrate, get_vault_registration
from renewise.watcher.toncenter import (
    VaultPoller,
    extract_tx_hash,
    extract_in_msg_value,
    is_confirmed,
)
from renewise.watcher.tasks import process_payment, _retry

log = logging.getLogger(__name__)


# ── Shared state ──────────────────────────────────────────────────────────────

_redis_conn = redis_sync.from_url(REDIS_URL)
_queue      = Queue(RQ_QUEUE_NAME, connection=_redis_conn)
_poller: VaultPoller | None = None


def _enqueue_payment(vault_address: str, tx: dict) -> None:
    tx_hash     = extract_tx_hash(tx)
    amount_nano = extract_in_msg_value(tx)
    if not tx_hash:
        log.warning("Skipping tx with no hash on vault %s", vault_address)
        return
    _queue.enqueue(
        process_payment,
        kwargs={
            "vault_address": vault_address,
            "tx_hash":       tx_hash,
            "amount_nano":   amount_nano,
        },
        retry=_retry(),
        job_timeout=120,
    )
    log.info("Enqueued process_payment | vault=%s tx=%s amount=%d", vault_address, tx_hash, amount_nano)


# ── Request handlers ──────────────────────────────────────────────────────────

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_webhook(request: web.Request) -> web.Response:
    # Verify secret token if configured
    if WEBHOOK_SECRET:
        token = request.headers.get("X-Webhook-Token", "")
        if not hmac.compare_digest(token, WEBHOOK_SECRET):
            log.warning("Webhook: invalid token from %s", request.remote)
            return web.Response(status=401, text="Unauthorized")

    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    vault_address = payload.get("account_id", "")
    tx            = payload  # TonCenter sends the full tx object

    if not vault_address or not is_confirmed(tx):
        return web.Response(status=200, text="ignored")

    _enqueue_payment(vault_address, tx)
    return web.Response(status=200, text="queued")


async def handle_register(request: web.Request) -> web.Response:
    """
    Register a vault address for polling.
    Called by the bot when it generates a payment link.
    Body: {"vault_address": "EQ..."}
    """
    try:
        body = await request.json()
        vault_address = body["vault_address"]
    except (KeyError, Exception):
        return web.Response(status=400, text="Missing vault_address")

    if _poller:
        _poller.add_address(vault_address)
        log.info("Registered vault for polling: %s", vault_address)

    return web.json_response({"registered": vault_address})


async def handle_recheck(request: web.Request) -> web.Response:
    """
    Manual recheck endpoint.  Accepts either a tx_hash or (vault_address).
    Fetches the latest transactions from TonCenter and enqueues them.
    Body: {"vault_address": "EQ..."} or {"tx_hash": "...", "vault_address": "EQ..."}
    """
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    vault_address = body.get("vault_address", "")
    tx_hash       = body.get("tx_hash", "")

    if not vault_address:
        return web.Response(status=400, text="vault_address required")

    from renewise.watcher.toncenter import fetch_transactions, fetch_single_transaction

    if tx_hash:
        tx = await fetch_single_transaction(tx_hash, vault_address)
        if tx:
            _enqueue_payment(vault_address, tx)
            return web.json_response({"enqueued": 1})
        return web.json_response({"enqueued": 0, "reason": "tx not found"})

    # No specific hash — fetch recent txs and enqueue any unprocessed ones
    from renewise.watcher.db import is_tx_processed
    txs = await fetch_transactions(vault_address, limit=10)
    enqueued = 0
    for tx in txs:
        h = extract_tx_hash(tx)
        if h and not await is_tx_processed(h):
            _enqueue_payment(vault_address, tx)
            enqueued += 1

    return web.json_response({"enqueued": enqueued})


# ── App factory ───────────────────────────────────────────────────────────────

async def create_app() -> web.Application:
    await migrate()

    global _poller
    _poller = VaultPoller(_enqueue_payment)

    # Seed poller with all known vault addresses from the registry
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        rows = await db.fetch("SELECT vault_address FROM vault_registry")
        for row in rows:
            _poller.add_address(row["vault_address"])

    app = web.Application()
    app.router.add_get("/health",    handle_health)
    app.router.add_post("/webhook",  handle_webhook)
    app.router.add_post("/register", handle_register)
    app.router.add_post("/recheck",  handle_recheck)

    # Start polling loop as a background task
    async def _start_poller(app: web.Application) -> None:
        app["poller_task"] = asyncio.create_task(_poller.run())

    async def _stop_poller(app: web.Application) -> None:
        task = app.get("poller_task")
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.on_startup.append(_start_poller)
    app.on_cleanup.append(_stop_poller)

    return app


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )
    app = asyncio.run(create_app())
    web.run_app(app, host=WEBHOOK_HOST, port=WEBHOOK_PORT)


if __name__ == "__main__":
    main()

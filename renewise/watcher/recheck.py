"""
renewise/watcher/recheck.py

Manual recheck function for support cases where the automated pipeline missed
a payment.  Exposed to the Phase 4 super-admin bot.

Two call patterns:
  1. recheck_by_tx_hash(tx_hash, vault_address)
     — fetches the specific tx from TonCenter and enqueues it.

  2. recheck_by_subscription(user_db_id, group_id)
     — looks up the vault address from vault_registry, fetches recent txs,
       and enqueues any that haven't been processed yet.

Both are async and safe to call from the bot's handler context.
"""
from __future__ import annotations

import logging
from typing import Any

import redis as redis_sync
from rq import Queue

from renewise.watcher.config import REDIS_URL, RQ_QUEUE_NAME
from renewise.watcher.db import get_vault_registration, is_tx_processed
from renewise.watcher.toncenter import (
    fetch_transactions,
    fetch_single_transaction,
    extract_tx_hash,
    extract_in_msg_value,
    is_confirmed,
)
from renewise.watcher.tasks import process_payment, _retry

log = logging.getLogger(__name__)


def _queue() -> Queue:
    return Queue(RQ_QUEUE_NAME, connection=redis_sync.from_url(REDIS_URL))


async def recheck_by_tx_hash(tx_hash: str, vault_address: str) -> dict[str, Any]:
    """
    Force-recheck a specific transaction hash.
    Returns {"enqueued": bool, "reason": str}.
    """
    log.info("recheck_by_tx_hash | tx=%s vault=%s", tx_hash, vault_address)

    if await is_tx_processed(tx_hash):
        return {"enqueued": False, "reason": "already_processed"}

    tx = await fetch_single_transaction(tx_hash, vault_address)
    if not tx:
        return {"enqueued": False, "reason": "tx_not_found_on_chain"}

    if not is_confirmed(tx):
        return {"enqueued": False, "reason": "not_confirmed"}

    amount_nano = extract_in_msg_value(tx)
    _queue().enqueue(
        process_payment,
        kwargs={
            "vault_address": vault_address,
            "tx_hash":       tx_hash,
            "amount_nano":   amount_nano,
        },
        retry=_retry(),
        job_timeout=120,
    )
    log.info("recheck_by_tx_hash: enqueued | tx=%s", tx_hash)
    return {"enqueued": True, "reason": "ok"}


async def recheck_by_subscription(user_db_id: int, group_id: int) -> dict[str, Any]:
    """
    Force-recheck all recent transactions for the vault associated with
    (user_db_id, group_id).  Enqueues any unprocessed confirmed txs.
    Returns {"enqueued": int, "vault_address": str | None}.
    """
    log.info("recheck_by_subscription | user_db_id=%d group_id=%d", user_db_id, group_id)

    # Find the vault address from the registry
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        row = await db.fetchrow(
            "SELECT vault_address FROM vault_registry "
            "WHERE user_id=$1 AND group_id=$2",
            user_db_id, group_id,
        )

    if not row:
        log.warning("recheck_by_subscription: no vault registered for user=%d group=%d", user_db_id, group_id)
        return {"enqueued": 0, "vault_address": None, "reason": "no_vault_registered"}

    vault_address = row["vault_address"]
    txs = await fetch_transactions(vault_address, limit=10)

    q = _queue()
    enqueued = 0
    for tx in txs:
        h = extract_tx_hash(tx)
        if not h or not is_confirmed(tx):
            continue
        if await is_tx_processed(h):
            continue
        amount_nano = extract_in_msg_value(tx)
        q.enqueue(
            process_payment,
            kwargs={
                "vault_address": vault_address,
                "tx_hash":       h,
                "amount_nano":   amount_nano,
            },
            retry=_retry(),
            job_timeout=120,
        )
        enqueued += 1

    log.info(
        "recheck_by_subscription: enqueued %d txs | vault=%s user=%d group=%d",
        enqueued, vault_address, user_db_id, group_id,
    )
    return {"enqueued": enqueued, "vault_address": vault_address, "reason": "ok"}

"""
renewise/watcher/watcher.py

Polling loop that watches vault addresses for incoming payments
and enqueues RQ jobs to process them.

The job function lives in renewise.watcher.tasks so it has a stable
importable path (RQ cannot serialize functions from __main__).
"""
import asyncio
import logging
import aiohttp

from renewise.config import (
    TONCENTER_TESTNET, REDIS_URL,
)
from renewise.watcher.toncenter import _key_manager
from renewise.db import queries

logger = logging.getLogger(__name__)


def get_toncenter_base_url() -> str:
    if TONCENTER_TESTNET:
        return "https://testnet.toncenter.com/api/v3"
    return "https://toncenter.com/api/v3"


async def fetch_transactions(session: aiohttp.ClientSession, account: str) -> list[dict]:
    """Fetch recent transactions for a given account from TonCenter v3 API."""
    url = f"{get_toncenter_base_url()}/transactions"
    params = {"account": account, "limit": 10, "sort": "desc"}
    try:
        data = await _key_manager.fetch_with_retry(session, url, params=params)
        return data.get("transactions", [])
    except Exception as e:
        logger.error("Error fetching txs for %s: %s", account, e)
        return []


def _extract_tx_fields(tx: dict) -> tuple[str | None, int]:
    """Pull tx_hash and amount_nano out of a raw TonCenter v3 transaction dict."""
    tx_hash = tx.get("hash")
    in_msg = tx.get("in_msg") or {}
    try:
        amount_nano = int(in_msg.get("value", 0))
    except (TypeError, ValueError):
        amount_nano = 0
    return tx_hash, amount_nano


async def poll_vaults(q: "Queue") -> None:
    """
    Main polling loop.  Fetches active vaults from the DB, polls TonCenter
    for new transactions, and enqueues a tasks.process_payment job for each
    transaction that hasn't been processed yet.

    The Queue is created once in __main__ (outside asyncio.run()) so its
    internal threads are never re-initialized across loop iterations.
    """
    from rq import Retry
    from renewise.config import JOB_MAX_RETRIES
    # Import the task from its proper module so RQ can serialize it
    from renewise.watcher.tasks import process_payment

    logger.info("Starting watcher polling loop...")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                vaults = await queries.get_vaults_to_watch()
                logger.debug("Watching %d vault(s)", len(vaults))

                for vault_address, _network in vaults:
                    # Normalise to raw 0:<hex> so TonCenter receives a stable,
                    # network-agnostic form regardless of which friendly variant
                    # (EQ/UQ/kQ/0Q) was stored in the DB.
                    try:
                        from pytoniq_core import Address as _Addr
                        _a = _Addr(vault_address)
                        canonical_addr = f"0:{_a.hash_part.hex()}"
                    except Exception:
                        canonical_addr = vault_address
                    txs = await fetch_transactions(session, canonical_addr)
                    for tx in txs:
                        tx_hash, amount_nano = _extract_tx_fields(tx)
                        if not tx_hash:
                            continue

                        # Fast pre-check: skip if already in processed_tx_hashes.
                        # The task does a second atomic check, but this avoids
                        # flooding the RQ queue with obvious duplicates.
                        if await queries.is_tx_processed(tx_hash):
                            continue

                        intervals = [10, 30, 60, 120, 300][:JOB_MAX_RETRIES] or [10]
                        q.enqueue(
                            process_payment,
                            kwargs={
                                "vault_address": vault_address,
                                "tx_hash": tx_hash,
                                "amount_nano": amount_nano,
                            },
                            retry=Retry(max=JOB_MAX_RETRIES, interval=intervals),
                        )
                        logger.info(
                            "Enqueued process_payment | vault=%s tx=%s amount=%d",
                            vault_address, tx_hash, amount_nano,
                        )

                await asyncio.sleep(5)

            except Exception as e:
                logger.error("Error in polling loop: %s", e)
                await asyncio.sleep(10)


if __name__ == "__main__":
    import redis as sync_redis
    from rq import Queue
    from renewise.config import RQ_QUEUE_NAME

    logging.basicConfig(level=logging.INFO)

    # Build the sync Redis client and RQ Queue ONCE, outside asyncio.run(),
    # so their internal connection-pool threads are never restarted.
    _sync_r = sync_redis.from_url(REDIS_URL)
    _q = Queue(RQ_QUEUE_NAME, connection=_sync_r)

    asyncio.run(poll_vaults(_q))

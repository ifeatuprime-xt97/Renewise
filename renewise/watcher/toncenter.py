"""
renewise/watcher/toncenter.py

TonCenter API client.

Two modes of operation (both enqueue the same RQ job):
  1. Webhook push  — TonCenter calls our /webhook endpoint on each confirmed tx.
                     Preferred: zero latency, no polling overhead.
  2. Polling loop  — Falls back to polling /getTransactions every POLL_INTERVAL_SECONDS.
                     Used when webhooks are unavailable (local dev, testnet limits).

The client is intentionally stateless: it only fetches raw tx data and hands it
to the job queue. All business logic lives in tasks.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from renewise.watcher.config import (
    TONCENTER_API_KEYS,
    TONCENTER_BASE_URL,
    TONCENTER_TESTNET,
    POLL_INTERVAL_SECONDS,
    MIN_CONFIRMATIONS,
)

log = logging.getLogger(__name__)

class TonCenterKeyManager:
    def __init__(self, keys: list[str]):
        self.keys = keys
        self.current_idx = 0

    def get_key(self) -> str | None:
        if not self.keys:
            return None
        return self.keys[self.current_idx]

    def rotate(self) -> str | None:
        if not self.keys:
            return None
        old_idx = self.current_idx
        self.current_idx = (self.current_idx + 1) % len(self.keys)
        log.warning("TonCenter rate limit hit (429). Rotating API key from index %d to %d.", old_idx, self.current_idx)
        return self.keys[self.current_idx]

    async def fetch_with_retry(self, session: aiohttp.ClientSession, url: str, params: dict[str, Any] = None) -> dict[str, Any]:
        """
        Execute a GET request with exponential backoff on 429.
        Rotates keys between retries when multiple keys are available.
        Raises the last exception if all retries are exhausted.
        """
        max_attempts = max(3, len(self.keys) + 1)
        delay = 2.0  # initial backoff in seconds

        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            headers = {}
            key = self.get_key()
            if key:
                headers["X-API-Key"] = key

            try:
                async with session.get(
                    url, params=params, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 429:
                        # Rotate to the next key (if available) then back off
                        if len(self.keys) > 1:
                            self.rotate()
                        wait = delay * (2 ** attempt)
                        log.warning(
                            "TonCenter 429 (attempt %d/%d) — backing off %.1fs",
                            attempt + 1, max_attempts, wait,
                        )
                        last_exc = aiohttp.ClientResponseError(
                            resp.request_info, resp.history, status=429
                        )
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientResponseError as exc:
                if exc.status == 429:
                    last_exc = exc
                    wait = delay * (2 ** attempt)
                    await asyncio.sleep(wait)
                    continue
                raise

        raise last_exc or RuntimeError("fetch_with_retry: all attempts exhausted")

_key_manager = TonCenterKeyManager(TONCENTER_API_KEYS)


# ── Raw API calls ─────────────────────────────────────────────────────────────

async def fetch_transactions(
    address: str,
    limit: int = 20,
    lt: int | None = None,
    hash_: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch confirmed transactions for a single vault address.
    Returns raw TonCenter transaction dicts, newest first.
    """
    params: dict[str, Any] = {"address": address, "limit": limit}
    if lt is not None:
        params["lt"] = lt
    if hash_ is not None:
        params["hash"] = hash_

    url = f"{TONCENTER_BASE_URL}/getTransactions"
    async with aiohttp.ClientSession() as session:
        data = await _key_manager.fetch_with_retry(session, url, params=params)
        if not data.get("ok"):
            raise RuntimeError(f"TonCenter error: {data}")
        return data.get("result", [])


async def fetch_single_transaction(tx_hash: str, address: str) -> dict[str, Any] | None:
    """
    Fetch a single transaction by hash for manual recheck.
    Returns None if not found.
    """
    try:
        txs = await fetch_transactions(address, limit=1, hash_=tx_hash)
        return txs[0] if txs else None
    except Exception as exc:
        log.warning("fetch_single_transaction failed for %s: %s", tx_hash, exc)
        return None


async def call_get_method(
    address: str,
    method: str,
    stack: list[Any] | None = None,
) -> Any:
    """
    Call a get-method on a TON smart contract via TonCenter /runGetMethod.

    Returns the first stack value as a Python int (for numeric getters like
    required_payment() and overage()), or None on any failure.

    This is used to read the vault's locked-in required_payment() value
    directly from the contract rather than re-deriving it from the live
    exchange rate — so the watcher's threshold always matches what the
    contract actually enforced on-chain.
    """
    url = f"{TONCENTER_BASE_URL}/runGetMethod"
    params: dict[str, Any] = {
        "address": address,
        "method":  method,
        "stack":   stack or [],
    }
    try:
        async with aiohttp.ClientSession() as session:
            data = await _key_manager.fetch_with_retry(session, url, params=params)
        if not data.get("ok"):
            log.warning("call_get_method %s.%s failed: %s", address, method, data)
            return None
        result_stack = data.get("result", {}).get("stack", [])
        if not result_stack:
            return None
        # TonCenter returns stack items as [["num", "0x..."], ...]
        top = result_stack[0]
        if isinstance(top, (list, tuple)) and len(top) >= 2:
            raw = top[1]
            return int(raw, 16) if isinstance(raw, str) else int(raw)
        return int(top) if top is not None else None
    except Exception as exc:
        log.warning("call_get_method %s.%s error: %s", address, method, exc)
        return None


def extract_tx_hash(tx: dict[str, Any]) -> str:
    """Extract the canonical hex tx hash from a TonCenter transaction dict."""
    return tx.get("transaction_id", {}).get("hash", "")


def extract_in_msg_value(tx: dict[str, Any]) -> int:
    """Extract the nanoTON value of the inbound message."""
    in_msg = tx.get("in_msg", {})
    return int(in_msg.get("value", 0))


def is_confirmed(tx: dict[str, Any]) -> bool:
    """
    A transaction returned by TonCenter /getTransactions is already confirmed
    (it's in a committed block). We check that it has a non-zero logical time
    as a basic sanity guard.
    """
    return bool(tx.get("transaction_id", {}).get("lt"))


# ── Polling loop ──────────────────────────────────────────────────────────────

class VaultPoller:
    """
    Polls TonCenter for new transactions on a set of vault addresses.
    Maintains a per-address cursor (last seen lt) to avoid re-processing.

    Usage:
        poller = VaultPoller(enqueue_fn)
        await poller.add_address("EQ...")
        await poller.run()   # runs forever; cancel to stop
    """

    def __init__(self, enqueue_fn) -> None:
        # enqueue_fn(vault_address, tx) — called for each new confirmed tx
        self._enqueue = enqueue_fn
        # vault_address → last seen logical time (int)
        self._cursors: dict[str, int] = {}

    def add_address(self, vault_address: str) -> None:
        if vault_address not in self._cursors:
            self._cursors[vault_address] = 0
            log.info("Polling: registered vault %s", vault_address)

    def remove_address(self, vault_address: str) -> None:
        self._cursors.pop(vault_address, None)

    async def poll_once(self) -> None:
        """Single poll cycle across all registered addresses."""
        for address, last_lt in list(self._cursors.items()):
            try:
                txs = await fetch_transactions(address, limit=20)
                # TonCenter returns newest-first; process oldest-first for correct cursor advance
                new_txs = [
                    t for t in reversed(txs)
                    if is_confirmed(t)
                    and int(t.get("transaction_id", {}).get("lt", 0)) > last_lt
                ]
                for tx in new_txs:
                    lt = int(tx["transaction_id"]["lt"])
                    self._enqueue(address, tx)
                    self._cursors[address] = max(self._cursors[address], lt)
                    log.debug("Polling: new tx lt=%s on %s", lt, address)
            except Exception as exc:
                log.warning("Poll error for %s: %s", address, exc)

    async def run(self) -> None:
        """Run the polling loop forever. Cancel the task to stop."""
        log.info("Polling loop started (interval=%ss)", POLL_INTERVAL_SECONDS)
        while True:
            await self.poll_once()
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

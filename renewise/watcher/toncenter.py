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
    TONCENTER_API_KEYS_MAINNET,
    TONCENTER_API_KEYS_TESTNET_LIST,
    TONCENTER_BASE_URL,
    TONCENTER_TESTNET,
    POLL_INTERVAL_SECONDS,
    MIN_CONFIRMATIONS,
)

log = logging.getLogger(__name__)

class TonCenterKeyManager:
    """
    Cooldown-aware, least-recently-used TonCenter API key manager.

    Design
    ──────
    Each key tracks two timestamps:
      • last_used_at   — monotonic time of the most recent successful request
      • cooldown_until — monotonic time after which the key is usable again

    On every request:
      1. Build the set of available keys (cooldown expired).
      2. Pick the one with the oldest last_used_at (LRU) so load is spread
         evenly across keys rather than hammering the first one.
      3. If ALL keys are cooling down, wait for the shortest remaining
         cooldown before retrying — no wasted requests.

    On 429:
      • Stamp the offending key with cooldown_until = now + COOLDOWN_SECONDS.
      • Immediately try the next best available key rather than sleeping
        the entire process.

    Adding more keys to TONCENTER_API_KEYS automatically increases capacity —
    no other changes needed.
    """

    # How long a key sits out after a 429 response (seconds)
    COOLDOWN_SECONDS: float = 60.0

    def __init__(self, keys: list[str]) -> None:
        self.keys: list[str] = list(keys)
        now = asyncio.get_event_loop().time() if self.keys else 0.0
        # Per-key state: key → (last_used_at, cooldown_until)
        self._last_used:   dict[str, float] = {k: 0.0  for k in self.keys}
        self._cooldown_until: dict[str, float] = {k: 0.0 for k in self.keys}

    def _best_key(self) -> tuple[str | None, float]:
        """
        Return (best_key, wait_seconds).

        best_key    — the LRU key whose cooldown has expired, or None if all
                      keys are still cooling down.
        wait_seconds — seconds to sleep before the next key becomes available
                       (0.0 when best_key is not None).
        """
        if not self.keys:
            return None, 0.0

        import time as _t
        now = _t.monotonic()
        available = [k for k in self.keys if self._cooldown_until[k] <= now]

        if available:
            # Pick the key that was used least recently
            best = min(available, key=lambda k: self._last_used[k])
            return best, 0.0

        # All keys are cooling — return the shortest remaining wait
        wait = min(self._cooldown_until[k] - now for k in self.keys)
        return None, max(wait, 0.0)

    def _mark_used(self, key: str) -> None:
        import time as _t
        self._last_used[key] = _t.monotonic()

    def _mark_rate_limited(self, key: str) -> None:
        import time as _t
        now = _t.monotonic()
        self._cooldown_until[key] = now + self.COOLDOWN_SECONDS
        remaining = len([k for k in self.keys if self._cooldown_until[k] <= now])
        log.warning(
            "TonCenter 429 — key ...%s cooling for %.0fs. "
            "%d/%d key(s) still available.",
            key[-6:], self.COOLDOWN_SECONDS, remaining, len(self.keys),
        )

    async def fetch_with_retry(
        self,
        session: aiohttp.ClientSession,
        url: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Execute a GET request, rotating cooldown-aware keys on 429.

        Strategy:
          • On success: mark key as used, return response.
          • On 429: cool the key, immediately retry with the next best key.
          • If all keys are cooling: sleep until the shortest cooldown expires,
            then retry. Max total attempts = max(3, n_keys × 2).
          • On non-429 HTTP error or network error: raise immediately.
        """
        max_attempts = max(3, len(self.keys) * 2) if self.keys else 3
        last_exc: Exception | None = None

        for attempt in range(max_attempts):
            key, wait = self._best_key()

            if key is None:
                # All keys cooling — wait for the shortest cooldown
                log.info(
                    "TonCenter: all %d key(s) cooling, waiting %.1fs (attempt %d/%d)",
                    len(self.keys), wait, attempt + 1, max_attempts,
                )
                await asyncio.sleep(wait + 0.1)  # +0.1s margin
                key, _ = self._best_key()
                if key is None:
                    continue  # shouldn't happen but be safe

            headers: dict[str, str] = {}
            if key:
                headers["X-API-Key"] = key

            try:
                async with session.get(
                    url, params=params, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 429:
                        if key:
                            self._mark_rate_limited(key)
                        last_exc = aiohttp.ClientResponseError(
                            resp.request_info, resp.history, status=429
                        )
                        continue  # immediately try next available key
                    resp.raise_for_status()
                    if key:
                        self._mark_used(key)
                    return await resp.json()

            except aiohttp.ClientResponseError as exc:
                if exc.status == 429:
                    if key:
                        self._mark_rate_limited(key)
                    last_exc = exc
                    continue
                raise

        raise last_exc or RuntimeError("fetch_with_retry: all attempts exhausted")

_key_manager = TonCenterKeyManager(TONCENTER_API_KEYS)

# Per-network key managers — always available regardless of TONCENTER_TESTNET setting.
# Used when the watcher needs to query both networks in the same process.
_key_manager_mainnet = TonCenterKeyManager(TONCENTER_API_KEYS_MAINNET)
_key_manager_testnet = TonCenterKeyManager(TONCENTER_API_KEYS_TESTNET_LIST)

log.info(
    "TonCenter key manager init — mainnet keys: %d, testnet keys: %d, active keys: %d (testnet=%s)",
    len(TONCENTER_API_KEYS_MAINNET),
    len(TONCENTER_API_KEYS_TESTNET_LIST),
    len(TONCENTER_API_KEYS),
    TONCENTER_TESTNET,
)

_MAINNET_URL = "https://toncenter.com/api/v2"
_TESTNET_URL = "https://testnet.toncenter.com/api/v2"


def _url_and_manager(network: str | None) -> tuple[str, TonCenterKeyManager]:
    """Return (base_url, key_manager) for the requested network."""
    if network == "testnet":
        # Fall back to global manager if testnet-specific list is empty
        km = _key_manager_testnet if _key_manager_testnet.keys else _key_manager
        return _TESTNET_URL, km
    if network == "mainnet":
        # Fall back to global manager if mainnet-specific list is empty
        km = _key_manager_mainnet if _key_manager_mainnet.keys else _key_manager
        return _MAINNET_URL, km
    # None → use global config (legacy / bot subscriptions)
    return TONCENTER_BASE_URL, _key_manager


# ── Raw API calls ─────────────────────────────────────────────────────────────

async def fetch_transactions(
    address: str,
    limit: int = 20,
    lt: int | None = None,
    hash_: str | None = None,
    network: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch confirmed transactions for a single vault address.
    Returns raw TonCenter transaction dicts, newest first.

    network: 'testnet', 'mainnet', or None (uses global TONCENTER_TESTNET config).
    """
    params: dict[str, Any] = {"address": address, "limit": limit}
    if lt is not None:
        params["lt"] = lt
    if hash_ is not None:
        params["hash"] = hash_

    base_url, km = _url_and_manager(network)
    url = f"{base_url}/getTransactions"
    async with aiohttp.ClientSession() as session:
        data = await km.fetch_with_retry(session, url, params=params)
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
    network: str | None = None,
) -> Any:
    """
    Call a get-method on a smart contract via TonCenter /runGetMethod.
    network: 'testnet', 'mainnet', or None (uses global config).
    """
    base_url, km = _url_and_manager(network)
    url = f"{base_url}/runGetMethod"
    params: dict[str, Any] = {
        "address": address,
        "method":  method,
        "stack":   stack or [],
    }
    try:
        async with aiohttp.ClientSession() as session:
            data = await km.fetch_with_retry(session, url, params=params)
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
    """Extract the nanogram value of the inbound message."""
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

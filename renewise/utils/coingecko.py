"""
renewise/utils/coingecko.py

Gram/USD price feed with multi-provider support.

Fetch order on each cache miss:
  1. CoinGecko   (free, no key required)
  2. CoinMarketCap (requires CMC_API_KEY in .env, skipped if not set)
  3. Last known cached value  ← stale-rate alerting fires here if > STALE_THRESHOLD

A successful fetch from either provider updates the shared cache and resets
the timestamp, so provider 2 is only ever called when provider 1 fails.

Constants
─────────
CACHE_TTL       — how long a fresh price is trusted (default 30 min)
STALE_THRESHOLD — age at which the fallback is flagged as stale (default 2 h)
FLOOR_PRICE     — hard fallback used only on the very first process start if
                  both providers fail before any price has ever been fetched
                  (set conservatively low: $1.50)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import aiohttp
import redis.asyncio as aioredis

from renewise.config import REDIS_URL, BOT_ACTIONS_CHANNEL

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

CACHE_TTL       = 1800   # 30 minutes: how long a price is considered fresh
STALE_THRESHOLD = 7200   # 2 hours: after this, log WARNING + publish Redis alert
FLOOR_PRICE     = 1.5    # last-resort fallback price (USD) before first successful fetch

# CoinMarketCap API key — optional. Leave blank to skip CMC entirely.
CMC_API_KEY: str = os.getenv("CMC_API_KEY", "")

# ── Shared cache (in-process) ─────────────────────────────────────────────────

_cache: dict = {
    "price_usd": FLOOR_PRICE,  # populated on first successful fetch
    "timestamp": 0,            # epoch of last successful fetch; 0 = never
}

# ── Alert helper ──────────────────────────────────────────────────────────────

async def _publish_stale_alert(hours_old: float, price: float) -> None:
    try:
        r = aioredis.from_url(REDIS_URL)
        await r.publish(BOT_ACTIONS_CHANNEL, json.dumps({
            "action":        "rate_stale_alert",
            "hours_old":     hours_old,
            "current_price": price,
        }))
        await r.aclose()
    except Exception as exc:
        log.error("Failed to publish rate stale alert to Redis: %s", exc)

# ── Individual provider fetchers ──────────────────────────────────────────────

async def _fetch_coingecko(session: aiohttp.ClientSession) -> float:
    """Fetch Gram price from CoinGecko (free tier, no key needed)."""
    async with session.get(
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=the-open-network&vs_currencies=usd",
        timeout=aiohttp.ClientTimeout(total=5),
    ) as resp:
        if resp.status == 429:
            raise RuntimeError(f"429 Too Many Requests — CoinGecko rate limit hit")
        resp.raise_for_status()
        data = await resp.json()
        return float(data["the-open-network"]["usd"])


async def _fetch_okx(session: aiohttp.ClientSession) -> float:
    """Fetch Gram price from OKX public ticker (no key needed)."""
    async with session.get(
        "https://www.okx.com/api/v5/market/ticker?instId=TON-USDT",
        timeout=aiohttp.ClientTimeout(total=5),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        # OKX returns code "0" on success; any other code means an error body
        if data.get("code") != "0":
            raise RuntimeError(f"OKX returned error: code={data.get('code')} msg={data.get('msg')}")
        rows = data.get("data") or []
        if not rows:
            raise RuntimeError("OKX returned empty data array for TON-USDT")
        return float(rows[0]["last"])


async def _fetch_coinmarketcap(session: aiohttp.ClientSession) -> float:
    """Fetch Gram price from CoinMarketCap (requires CMC_API_KEY)."""
    if not CMC_API_KEY:
        raise RuntimeError("CMC_API_KEY not configured — skipping CoinMarketCap")
    async with session.get(
        "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
        params={"symbol": "TON", "convert": "USD"},
        headers={"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"},
        timeout=aiohttp.ClientTimeout(total=5),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        coin_data = data.get("data", {})
        if "TON" not in coin_data:
            # CMC may return a status error body instead of coin data
            status = data.get("status", {})
            raise RuntimeError(
                f"CMC response missing 'TON' key — "
                f"error_code={status.get('error_code')} "
                f"error_message={status.get('error_message')!r}"
            )
        # CMC can return a list or a dict depending on how many symbols matched
        entry = coin_data["TON"]
        if isinstance(entry, list):
            entry = entry[0]
        return float(entry["quote"]["USD"]["price"])

# ── Main public API ───────────────────────────────────────────────────────────

async def get_ton_usd_price() -> float:
    """
    Return the current Gram/USD price with a 30-minute in-process cache.

    Provider waterfall (each is only tried if the previous fails):
      1. CoinGecko
      2. CoinMarketCap (if CMC_API_KEY is set)
      3. Last cached value (stale alert fires if > STALE_THRESHOLD old)
      4. FLOOR_PRICE ($1.50) — only on very first call ever if all fail
    """
    now = time.time()

    # Cache hit — price is still fresh
    if _cache["timestamp"] > 0 and now - _cache["timestamp"] < CACHE_TTL:
        return _cache["price_usd"]

    async with aiohttp.ClientSession() as session:

        # ── Provider 1: CoinGecko ─────────────────────────────────────────
        try:
            price = await _fetch_coingecko(session)
            _cache["price_usd"] = price
            _cache["timestamp"] = now
            log.debug("Gram price from CoinGecko: $%.4f", price)
            return price
        except Exception as exc:
            log.warning("CoinGecko fetch failed: %s", exc)

        # ── Provider 2: OKX (free, no key) ───────────────────────────────
        try:
            price = await _fetch_okx(session)
            _cache["price_usd"] = price
            _cache["timestamp"] = now
            log.info("Gram price from OKX (CoinGecko fallback): $%.4f", price)
            return price
        except Exception as exc:
            log.warning("OKX fetch failed: %s", exc)

        # ── Provider 3: CoinMarketCap ─────────────────────────────────────
        try:
            price = await _fetch_coinmarketcap(session)
            _cache["price_usd"] = price
            _cache["timestamp"] = now
            log.info("Gram price from CoinMarketCap (CoinGecko+OKX fallback): $%.4f", price)
            return price
        except Exception as exc:
            log.warning("CoinMarketCap fetch failed: %s", exc)

    # ── Provider 3: stale cache / floor ──────────────────────────────────
    age_seconds = now - _cache["timestamp"]
    fallback    = _cache["price_usd"]

    if _cache["timestamp"] > 0 and age_seconds > STALE_THRESHOLD:
        hours_old = age_seconds / 3600
        log.warning(
            "🚨 GRAM/USD rate is STALE both providers failed. "
            "Fallback is %.1f hours old. Using stale rate: $%.2f",
            hours_old, fallback,
        )
        asyncio.create_task(_publish_stale_alert(hours_old, fallback))
    elif _cache["timestamp"] == 0:
        # First ever call and both providers failed — use the floor price
        log.warning(
            "Both price providers failed on first fetch. "
            "Using FLOOR_PRICE=$%.2f", FLOOR_PRICE
        )
    else:
        log.warning(
            "Both price providers failed. Using last cached rate: $%.2f "
            "(%.0f s old)", fallback, age_seconds,
        )

    return fallback

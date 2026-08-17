"""
renewise/watcher/config.py

All watcher configuration is read from environment variables so nothing
operational is hardcoded. Defaults are documented in .env.example.
"""
from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv()

# ── Redis / RQ ────────────────────────────────────────────────────────────────
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# RQ queue name — workers and enqueuers must use the same name
RQ_QUEUE_NAME: str = os.getenv("RQ_QUEUE_NAME", "renewise")

# TONCENTER_API_KEYS_TESTNET and TONCENTER_API_KEYS (mainnet) are separate env vars
# because TON's API portal issues per-network keys — a testnet key will be
# rejected on api.toncenter.com and vice-versa.
#
# For backward compat, the legacy TONCENTER_API_KEY single-key env var is still
# accepted and is treated as a mainnet key (the common prior use-case).
#
# .env examples:
#   Single mainnet key (legacy):  TONCENTER_API_KEY=abc123
#   Multi mainnet keys:           TONCENTER_API_KEYS=abc123,def456
#   Testnet keys:                 TONCENTER_API_KEYS_TESTNET=xyz789
#
TONCENTER_TESTNET: bool = os.getenv("TONCENTER_TESTNET", "false").lower() == "true"

_mainnet_keys_str  = os.getenv("TONCENTER_API_KEYS", os.getenv("TONCENTER_API_KEY", ""))
_testnet_keys_str  = os.getenv("TONCENTER_API_KEYS_TESTNET", "")

TONCENTER_API_KEYS_MAINNET: list[str] = [k.strip() for k in _mainnet_keys_str.split(",") if k.strip()]
TONCENTER_API_KEYS_TESTNET_LIST: list[str] = [k.strip() for k in _testnet_keys_str.split(",") if k.strip()]

# The active key list — whichever network we're pointed at
TONCENTER_API_KEYS: list[str] = TONCENTER_API_KEYS_TESTNET_LIST if TONCENTER_TESTNET else TONCENTER_API_KEYS_MAINNET

# Single-value compat alias (used by legacy import sites)
TONCENTER_API_KEY: str = TONCENTER_API_KEYS[0] if TONCENTER_API_KEYS else ""

TONCENTER_BASE_URL: str = (
    "https://testnet.toncenter.com/api/v2"
    if TONCENTER_TESTNET
    else "https://toncenter.com/api/v2"
)

# How many confirmed blocks (logical time units) before we treat a tx as final.
# TON finalises in ~5 s; 1 confirmation is sufficient for most use-cases.
MIN_CONFIRMATIONS: int = int(os.getenv("MIN_CONFIRMATIONS", "1"))

# ── Polling fallback ──────────────────────────────────────────────────────────
# Interval in seconds between polling cycles when webhooks are not available.
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))

# ── Webhook server ────────────────────────────────────────────────────────────
WEBHOOK_HOST: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8080"))
# Secret token TonCenter sends in X-Webhook-Token header for verification
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")

# ── Subscription lifecycle ────────────────────────────────────────────────────
# Days after next_renewal_date before a subscription is expired and user kicked
GRACE_PERIOD_DAYS: int = int(os.getenv("GRACE_PERIOD_DAYS", "3"))

# Days before next_renewal_date to send the renewal reminder DM
REMINDER_WINDOW_DAYS: int = int(os.getenv("REMINDER_WINDOW_DAYS", "3"))

# ── RQ job retry ─────────────────────────────────────────────────────────────
# Number of automatic retries for a failed payment-processing job
JOB_MAX_RETRIES: int = int(os.getenv("JOB_MAX_RETRIES", "5"))
# Base delay in seconds for exponential backoff (delay = base * 2^attempt)
JOB_RETRY_BASE_DELAY: int = int(os.getenv("JOB_RETRY_BASE_DELAY", "10"))

# ── Bot inter-process communication ──────────────────────────────────────────
# The watcher triggers bot actions by publishing to a Redis pub/sub channel.
# The bot process subscribes and executes the Telegram API calls.
BOT_ACTIONS_CHANNEL: str = os.getenv("BOT_ACTIONS_CHANNEL", "renewise:bot_actions")

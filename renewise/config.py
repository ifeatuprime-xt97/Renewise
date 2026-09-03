import os
from dotenv import load_dotenv

load_dotenv()


def _safe_int(value: str | None, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


BOT_TOKEN: str     = os.environ["BOT_TOKEN"]
DATABASE_PATH: str = os.getenv("DATABASE_PATH", "./data/renewise.db")
# Neon / PostgreSQL connection string.
# When set, the app uses asyncpg instead of aiosqlite.
# Format: postgresql://user:password@host/dbname?sslmode=require
# Leave blank to use SQLite (DATABASE_PATH) — default for local dev.
DATABASE_URL: str  = os.getenv("DATABASE_URL", "")

# ── DB mode helper ────────────────────────────────────────────────────────────
# Use this throughout the codebase instead of checking DATABASE_URL directly.
USE_POSTGRES: bool = bool(DATABASE_URL)

# ── DB mode note ─────────────────────────────────────────────────────────────
# Set DATABASE_URL to a Neon (PostgreSQL) connection string for production.
# Leave it blank to use SQLite (DATABASE_PATH) for local development.
# The connection adapter in renewise/db/connection.py handles both transparently.
# ─────────────────────────────────────────────────────────────────────────────

SUPERADMIN_BOT_TOKEN: str = os.getenv("SUPERADMIN_BOT_TOKEN", "")
ALLOWED_SUPERADMIN_IDS = [
    int(x.strip()) for x in os.getenv("ALLOWED_SUPERADMIN_IDS", "").split(",") if x.strip()
]

MEMBERS_PAGE_SIZE = 5

# ── Payment config ────────────────────────────────────────────────────────────
# Platform wallet that receives the combined 5.3% fee
PLATFORM_WALLET: str = os.getenv("PLATFORM_WALLET", "")

# Address that receives PaymentLog messages for off-chain indexing.
# Can be the same as PLATFORM_WALLET or a dedicated chain-watcher address.
LOG_ADDRESS: str = os.getenv("LOG_ADDRESS", "")

# Dedicated trigger wallet — a cheap hot wallet used ONLY to send Refund{}
# messages to vaults. NOT the platform fee-collection wallet.
# Compromise of this wallet can only trigger refunds the contract already
# validated — it cannot drain any funds.
TRIGGER_WALLET: str = os.getenv("TRIGGER_WALLET", "")
TRIGGER_MNEMONIC: str = os.getenv("TRIGGER_MNEMONIC", "")

# Overpayment threshold in USD — refunds are only issued above this amount.
OVERPAYMENT_REFUND_THRESHOLD_USD: float = float(os.getenv("OVERPAYMENT_REFUND_THRESHOLD_USD", "1.00"))

# Fee basis points (1 bps = 0.01%).
# Default: 200 (2.00% buyer) + 330 (3.30% admin) = 5.30% combined platform fee.
BUYER_FEE_BPS: int = int(os.getenv("BUYER_FEE_BPS", "200"))
ADMIN_FEE_BPS: int = int(os.getenv("ADMIN_FEE_BPS", "330"))

_keys_str = os.getenv("TONCENTER_API_KEYS", os.getenv("TONCENTER_API_KEY", ""))
TONCENTER_API_KEYS = [k.strip() for k in _keys_str.split(",") if k.strip()]
TONCENTER_API_KEY: str = TONCENTER_API_KEYS[0] if TONCENTER_API_KEYS else ""
TONCENTER_TESTNET: bool = os.getenv("TONCENTER_TESTNET", "false").lower() == "true"

# ── Redis & Background Jobs (Phase 3) ─────────────────────────────────────────
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
RQ_QUEUE_NAME: str = os.getenv("RQ_QUEUE_NAME", "renewise_tasks")
BOT_ACTIONS_CHANNEL: str = os.getenv("BOT_ACTIONS_CHANNEL", "bot_actions")
JOB_MAX_RETRIES: int = int(os.getenv("JOB_MAX_RETRIES", "3"))

# ── Enforcement Config ────────────────────────────────────────────────────────
MIN_CONFIRMATIONS: int = int(os.getenv("MIN_CONFIRMATIONS", "1"))
GRACE_PERIOD_DAYS: int = int(os.getenv("GRACE_PERIOD_DAYS", "3"))
REMINDER_WINDOW_DAYS: int = int(os.getenv("REMINDER_WINDOW_DAYS", "3"))
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")
SUPPORT_USERNAME: str = os.getenv("SUPPORT_USERNAME", "")  # e.g. "ReneWiseSupport"

# ── Render keep-alive ─────────────────────────────────────────────────────────
# Free Render web services sleep after 15 minutes with no inbound HTTP.
# Telegram polling does not count. When PORT or RENDER_EXTERNAL_URL is set
# (Render sets both automatically), run.py binds a /health server and pings
# the public URL on this interval so the process stays awake.
# KEEP_ALIVE=false disables it. KEEP_ALIVE_URLS pings extra services (miniapp).
KEEP_ALIVE: bool = os.getenv("KEEP_ALIVE", "true").lower() not in ("0", "false", "no", "off")
KEEP_ALIVE_URL: str = os.getenv("KEEP_ALIVE_URL", "")
KEEP_ALIVE_URLS: list[str] = [
    u.strip() for u in os.getenv("KEEP_ALIVE_URLS", "").split(",") if u.strip()
]
KEEP_ALIVE_INTERVAL: int = _safe_int(os.getenv("KEEP_ALIVE_INTERVAL", "600"), 600)
KEEP_ALIVE_PORT: int = _safe_int(os.getenv("KEEP_ALIVE_PORT") or os.getenv("PORT") or "0")

# ── Wallet change protection ──────────────────────────────────────────────────
# How long (in hours) a submitted wallet change sits in "pending" before it
# applies automatically.  The admin receives an immediate DM and can cancel
# within this window.  Adjust here — never hardcode the number inline.
WALLET_CHANGE_DELAY_HOURS: int = int(os.getenv("WALLET_CHANGE_DELAY_HOURS", "24"))

"""
renewise/monitor.py

In-process health monitor.

Runs as a background asyncio task inside run.py alongside the main bot,
superadmin bot, and payment watcher. No external dependencies — uses only
what's already in the project.

Checks (every 5 minutes unless noted):
  1. Database          — SELECT 1 roundtrip against Neon
  2. TonCenter API     — GET /getMasterchainInfo; alerts after 3 consecutive failures
  3. Telegram Bot API  — GET /getMe on BOT_TOKEN
  4. Watcher heartbeat — timestamp updated by the polling loop; alert if stale >3 min
  5. Trigger wallet    — balance check every 30 minutes (not every 5)
  6. Pending refunds   — alert if pending_send count exceeds REFUND_ALERT_THRESHOLD
  7. CoinGecko feed    — alert if last successful price fetch is >30 minutes old

Unhandled exceptions:
  install_global_exception_handler() installs a custom asyncio exception handler
  that captures any unhandled Task exception and DMs it to all superadmins with
  the full traceback. Limited to one DM per unique exception type per 5 minutes
  to prevent spam during a crash loop.

Alert behaviour:
  - Each check has a boolean "firing" state.
  - A DM is sent ONCE when the state transitions False → True (alert).
  - A DM is sent ONCE when the state transitions True → False (resolved).
  - No repeated DMs while the issue persists.
  - All alerts are also logged at ERROR level for Render log visibility.

Usage:
    from renewise.monitor import start_monitor, install_global_exception_handler
    monitor_task = asyncio.create_task(start_monitor(bot))
    install_global_exception_handler(bot)
"""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Bot

log = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────

# How often the main monitor loop runs
MONITOR_INTERVAL_SECONDS: int = 300          # 5 minutes

# TonCenter: alert after this many consecutive failures
TONCENTER_FAILURE_THRESHOLD: int = 3

# Watcher heartbeat: alert if no update for this long
WATCHER_STALE_SECONDS: int = 180             # 3 minutes

# Trigger wallet checks run every Nth monitor cycle (5 min × 6 = 30 min)
TRIGGER_WALLET_CHECK_EVERY_N: int = 6

# CoinGecko: alert if last success is older than this
PRICE_FEED_STALE_SECONDS: int = 1800         # 30 minutes

# Pending refund alert threshold
REFUND_ALERT_THRESHOLD: int = 5

# Exception dedup: same exception type suppressed for this long after first DM
EXCEPTION_DEDUP_SECONDS: int = 300           # 5 minutes

# Telegram message length cap (Bot API hard limit is 4096)
_TG_MAX_LEN: int = 4000


# ── Shared heartbeat (written by watcher, read by monitor) ───────────────────

# Monotonic timestamp of the last time the watcher polling loop completed
# a full vault scan. Updated by renewise/watcher/inprocess_watcher.py.
# Initialised to current time so the monitor doesn't false-alarm at startup.
watcher_last_heartbeat: float = time.monotonic()


# ── Internal state ────────────────────────────────────────────────────────────

@dataclass
class _CheckState:
    """Tracks alert/resolved state for a single check so we only DM on transitions."""
    firing: bool = False
    consecutive_failures: int = 0
    last_alert_time: float = 0.0


@dataclass
class _MonitorState:
    db:              _CheckState = field(default_factory=_CheckState)
    toncenter:       _CheckState = field(default_factory=_CheckState)
    telegram_api:    _CheckState = field(default_factory=_CheckState)
    watcher:         _CheckState = field(default_factory=_CheckState)
    trigger_wallet:  _CheckState = field(default_factory=_CheckState)
    price_feed:      _CheckState = field(default_factory=_CheckState)
    refunds:         _CheckState = field(default_factory=_CheckState)
    # cycle counter for trigger wallet throttle
    cycle: int = 0
    # exception dedup: exc_type_name → last DM timestamp
    exc_dedup: dict = field(default_factory=dict)


_state = _MonitorState()


# ── Telegram helpers ──────────────────────────────────────────────────────────

async def _dm_superadmins(bot: "Bot", text: str) -> None:
    """
    DM every superadmin. Truncates to Telegram's 4096-char limit.
    Never raises — a broken DM must not crash the monitor itself.
    """
    from renewise.config import ALLOWED_SUPERADMIN_IDS
    if not ALLOWED_SUPERADMIN_IDS:
        return
    if len(text) > _TG_MAX_LEN:
        text = text[:_TG_MAX_LEN - 20] + "\n\n… <i>(truncated)</i>"
    for uid in ALLOWED_SUPERADMIN_IDS:
        try:
            await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
        except Exception as exc:
            log.warning("monitor: could not DM superadmin %d: %s", uid, exc)


async def _alert(bot: "Bot", state: _CheckState, name: str, detail: str) -> None:
    """Fire an alert DM if this check is not already firing."""
    if state.firing:
        return
    state.firing = True
    state.last_alert_time = time.monotonic()
    msg = f"🔴 <b>ALERT: {name}</b>\n\n{detail}"
    log.error("monitor ALERT — %s: %s", name, detail)
    await _dm_superadmins(bot, msg)


async def _resolve(bot: "Bot", state: _CheckState, name: str, detail: str = "") -> None:
    """Fire a resolved DM if this check was previously firing."""
    if not state.firing:
        return
    state.firing = False
    state.consecutive_failures = 0
    duration = int(time.monotonic() - state.last_alert_time)
    mins, secs = divmod(duration, 60)
    duration_str = f"{mins}m {secs}s" if mins else f"{secs}s"
    msg = (
        f"🟢 <b>RESOLVED: {name}</b>\n\n"
        f"Downtime: {duration_str}"
        + (f"\n{detail}" if detail else "")
    )
    log.info("monitor RESOLVED — %s (was down %s)", name, duration_str)
    await _dm_superadmins(bot, msg)


# ── Individual checks ─────────────────────────────────────────────────────────

async def _check_database(bot: "Bot") -> None:
    try:
        from renewise.db.connection import _db
        async with _db() as db:
            await db.fetchval("SELECT 1")
        await _resolve(bot, _state.db, "Database (Neon)")
    except Exception as exc:
        await _alert(
            bot, _state.db, "Database (Neon)",
            f"<code>SELECT 1</code> failed:\n<code>{exc}</code>\n\n"
            f"Payments cannot be recorded. Users may lose access after paying."
        )


async def _check_toncenter(bot: "Bot") -> None:
    try:
        import aiohttp
        from renewise.config import TONCENTER_API_KEY, TONCENTER_TESTNET
        base = (
            "https://testnet.toncenter.com/api/v2"
            if TONCENTER_TESTNET
            else "https://toncenter.com/api/v2"
        )
        headers = {"X-API-Key": TONCENTER_API_KEY} if TONCENTER_API_KEY else {}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{base}/getMasterchainInfo",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 500:
                    raise RuntimeError(f"HTTP {resp.status}")
        _state.toncenter.consecutive_failures = 0
        await _resolve(bot, _state.toncenter, "TonCenter API")
    except Exception as exc:
        _state.toncenter.consecutive_failures += 1
        if _state.toncenter.consecutive_failures >= TONCENTER_FAILURE_THRESHOLD:
            await _alert(
                bot, _state.toncenter, "TonCenter API",
                f"Consecutive failures: <b>{_state.toncenter.consecutive_failures}</b>\n"
                f"Last error: <code>{exc}</code>\n\n"
                f"The payment watcher cannot confirm on-chain transactions. "
                f"Users who pay will not receive automatic access until TonCenter recovers."
            )


async def _check_telegram_api(bot: "Bot") -> None:
    try:
        await bot.get_me()
        await _resolve(bot, _state.telegram_api, "Telegram Bot API")
    except Exception as exc:
        await _alert(
            bot, _state.telegram_api, "Telegram Bot API",
            f"<code>getMe</code> failed:\n<code>{exc}</code>\n\n"
            f"The bot cannot receive or send messages."
        )


async def _check_watcher_heartbeat(bot: "Bot") -> None:
    age = time.monotonic() - watcher_last_heartbeat
    if age > WATCHER_STALE_SECONDS:
        await _alert(
            bot, _state.watcher, "Payment Watcher",
            f"Last heartbeat was <b>{int(age)}s ago</b> "
            f"(threshold: {WATCHER_STALE_SECONDS}s).\n\n"
            f"The polling loop may have crashed or hung. "
            f"Payments will not be confirmed until the watcher recovers."
        )
    else:
        await _resolve(bot, _state.watcher, "Payment Watcher")


async def _check_trigger_wallet(bot: "Bot") -> None:
    from renewise.config import TRIGGER_WALLET, TONCENTER_API_KEY, TONCENTER_TESTNET
    if not TRIGGER_WALLET:
        return  # already warned at startup
    try:
        from renewise.superadmin.queries import get_trigger_wallet_balance
        balance = await get_trigger_wallet_balance(
            TRIGGER_WALLET, TONCENTER_API_KEY, TONCENTER_TESTNET
        )
        if balance is None:
            return  # TonCenter error — don't double-alert with toncenter check

        if balance < 0.02:
            await _alert(
                bot, _state.trigger_wallet, "Trigger Wallet Balance",
                f"Balance: <b>{balance:.6f} GRAM</b> (critical — need ≥ 0.10 GRAM)\n"
                f"Automatic refunds will fail until the wallet is topped up.\n\n"
                f"Top-up address: <code>{TRIGGER_WALLET}</code>"
            )
        elif balance < 0.10:
            await _alert(
                bot, _state.trigger_wallet, "Trigger Wallet Balance",
                f"Balance: <b>{balance:.6f} GRAM</b> (low — recommended ≥ 0.10 GRAM)\n"
                f"Top-up soon to avoid refund delays.\n\n"
                f"Address: <code>{TRIGGER_WALLET}</code>"
            )
        else:
            await _resolve(bot, _state.trigger_wallet, "Trigger Wallet Balance")
    except Exception as exc:
        log.warning("monitor: trigger wallet check failed: %s", exc)


async def _check_price_feed(bot: "Bot") -> None:
    try:
        from renewise.utils.coingecko import _cache as _cg_cache
        last_success = _cg_cache.get("timestamp", 0)
        age = time.time() - last_success if last_success else PRICE_FEED_STALE_SECONDS + 1
        if age > PRICE_FEED_STALE_SECONDS:
            await _alert(
                bot, _state.price_feed, "CoinGecko Price Feed",
                f"Last successful fetch: <b>{int(age // 60)}m ago</b> "
                f"(threshold: {PRICE_FEED_STALE_SECONDS // 60}m).\n\n"
                f"Payment amounts may be calculated at a stale exchange rate."
            )
        else:
            await _resolve(bot, _state.price_feed, "CoinGecko Price Feed")
    except Exception:
        pass  # non-critical check — never crash the monitor


async def _check_pending_refunds(bot: "Bot") -> None:
    try:
        from renewise.superadmin.queries import get_pending_send_refund_count
        count = await get_pending_send_refund_count()
        if count >= REFUND_ALERT_THRESHOLD:
            await _alert(
                bot, _state.refunds, "Pending Refund Backlog",
                f"<b>{count}</b> refund(s) stuck in <code>pending_send</code>.\n\n"
                f"The auto-retry loop will attempt these every 10 minutes. "
                f"If the count keeps growing, check the trigger wallet balance "
                f"and TonCenter connectivity.\n\n"
                f"Use /pendingrefunds in the superadmin bot to review."
            )
        else:
            await _resolve(bot, _state.refunds, "Pending Refund Backlog",
                           f"Queue cleared ({count} remaining).")
    except Exception as exc:
        log.warning("monitor: pending refund check failed: %s", exc)


# ── Main monitor loop ─────────────────────────────────────────────────────────

async def start_monitor(bot: "Bot") -> None:
    """
    Main monitor coroutine. Run as a background asyncio task in run.py.

        monitor_task = asyncio.create_task(start_monitor(app.bot))

    Runs an initial check immediately on startup, then every
    MONITOR_INTERVAL_SECONDS thereafter.
    """
    log.info("monitor: started (interval=%ds)", MONITOR_INTERVAL_SECONDS)

    async def _run_all_checks() -> None:
        _state.cycle += 1
        await _check_database(bot)
        await _check_toncenter(bot)
        await _check_telegram_api(bot)
        await _check_watcher_heartbeat(bot)
        await _check_pending_refunds(bot)
        await _check_price_feed(bot)
        # Trigger wallet only every Nth cycle to avoid hammering TonCenter
        if _state.cycle % TRIGGER_WALLET_CHECK_EVERY_N == 0:
            await _check_trigger_wallet(bot)

    # Run once immediately at startup so superadmins know the monitor is live
    # and any pre-existing issues surface right away.
    try:
        await _run_all_checks()
    except Exception as exc:
        log.error("monitor: initial check failed: %s", exc, exc_info=True)

    while True:
        try:
            await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
            await _run_all_checks()
        except asyncio.CancelledError:
            log.info("monitor: cancelled, shutting down.")
            return
        except Exception as exc:
            # The monitor itself must never crash — log and keep going.
            log.error("monitor: unexpected error in check loop: %s", exc, exc_info=True)


# ── Global asyncio exception handler ─────────────────────────────────────────

def install_global_exception_handler(bot: "Bot") -> None:
    """
    Install a custom asyncio loop exception handler that DMs all superadmins
    whenever an unhandled Task exception occurs anywhere in the process.

    This catches crashes in:
      - Bot handlers (callback queries, message handlers, etc.)
      - The payment watcher loop
      - The monitor itself (if it somehow escapes the try/except above)
      - Any asyncio.create_task() call whose exception is never retrieved

    Each unique exception type is deduplicated for EXCEPTION_DEDUP_SECONDS
    to prevent flooding superadmins during a crash loop.

    Call once after the event loop is running:
        install_global_exception_handler(app.bot)
    """
    loop = asyncio.get_event_loop()

    def _handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc: BaseException | None = context.get("exception")
        message: str = context.get("message", "Unknown asyncio error")
        task = context.get("task")
        task_name = task.get_name() if task else "unknown"

        if exc is None:
            log.warning("monitor: asyncio error (no exception): %s | task=%s", message, task_name)
            return

        exc_type = type(exc).__name__

        # Deduplicate: same exception type → only DM once per dedup window
        now = time.monotonic()
        last_sent = _state.exc_dedup.get(exc_type, 0.0)
        if now - last_sent < EXCEPTION_DEDUP_SECONDS:
            log.error(
                "monitor: suppressed duplicate alert for %s (sent %ds ago)",
                exc_type, int(now - last_sent),
            )
            return
        _state.exc_dedup[exc_type] = now

        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        # Truncate traceback to fit Telegram's message limit
        max_tb = _TG_MAX_LEN - 300
        if len(tb) > max_tb:
            tb = "…(truncated)\n" + tb[-max_tb:]

        dm_text = (
            f"💥 <b>Unhandled Exception: {exc_type}</b>\n"
            f"Task: <code>{task_name}</code>\n\n"
            f"<pre>{tb}</pre>"
        )

        log.error(
            "monitor: unhandled exception in task '%s': %s\n%s",
            task_name, exc, tb,
        )

        # Schedule the DM as a fire-and-forget task so the sync handler
        # can hand off to async without blocking the event loop.
        asyncio.ensure_future(_dm_superadmins(bot, dm_text))

    loop.set_exception_handler(_handler)
    log.info("monitor: global asyncio exception handler installed.")

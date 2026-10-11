"""
renewise/utils/alerts.py

Centralized utility for dispatching critical alerts to superadmins.
Works in all environments: Render daemon, Vercel serverless, background tasks.
"""
from __future__ import annotations
import html
import logging
from typing import Any, Optional

import renewise.config as cfg

log = logging.getLogger(__name__)


async def notify_superadmins(text: str) -> None:
    """
    Send an HTML-formatted message to all superadmins via SUPERADMIN_BOT_TOKEN.
    Safe and non-blocking: never raises an exception.
    """
    token = cfg.SUPERADMIN_BOT_TOKEN
    admin_ids = cfg.ALLOWED_SUPERADMIN_IDS

    if not token or not admin_ids:
        log.warning("notify_superadmins: SUPERADMIN_BOT_TOKEN or ALLOWED_SUPERADMIN_IDS not configured.")
        return

    # Cap at Telegram's 4096-char limit
    if len(text) > 4000:
        text = text[:3980] + "\n\n… <i>(truncated)</i>"

    import aiohttp

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            for admin_id in admin_ids:
                try:
                    payload = {
                        "chat_id": admin_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    }
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status != 200:
                            err_body = await resp.text()
                            log.warning(
                                "Failed to send superadmin alert to %s: HTTP %s (%s)",
                                admin_id, resp.status, err_body
                            )
                except Exception as ex:
                    log.warning("Failed to DM superadmin %s: %s", admin_id, ex)
    except Exception as e:
        log.warning("Could not establish session for superadmin alert: %s", e)


async def alert_payment_failure(
    platform: dict[str, Any],
    charge_id: int | str,
    amount_usd_cents: int,
    error: Exception | str,
    details: Optional[str] = None,
) -> None:
    """
    Report a payment service or charge generation failure to superadmins
    and record it in the platform audit log.
    """
    error_type = type(error).__name__ if isinstance(error, Exception) else "Error"
    error_msg = str(error)

    platform_name = html.escape(str(platform.get("platform_name") or "Unknown App"))
    platform_id = platform.get("id", "N/A")
    owner_tg_id = platform.get("owner_telegram_id", "N/A")
    auth_mode = html.escape(str(platform.get("auth_mode") or "test"))
    amount_usd = amount_usd_cents / 100.0

    msg = (
        f"🚨 <b>Payment Service Failure</b>\n\n"
        f"<b>App:</b> {platform_name} (ID: <code>{platform_id}</code>)\n"
        f"<b>Owner TG ID:</b> <code>{owner_tg_id}</code>\n"
        f"<b>Charge ID:</b> <code>{charge_id}</code>\n"
        f"<b>Amount:</b> ${amount_usd:.2f} USD\n"
        f"<b>Mode:</b> <code>{auth_mode}</code>\n"
        f"<b>Error Type:</b> <code>{html.escape(error_type)}</code>\n"
        f"<b>Message:</b> <code>{html.escape(error_msg)}</code>\n"
    )

    if details:
        msg += f"\n<b>Details:</b> <code>{html.escape(str(details))}</code>\n"

    msg += "\n<i>Reported automatically by ReneWise platform.</i>"

    # Audit in DB if reachable (best-effort)
    try:
        from renewise.db.connection import _db
        async with _db() as db:
            await db.execute(
                "INSERT INTO platform_audit_log (platform_id, action, actor_telegram_id, details) VALUES ($1, $2, $3, $4)",
                platform.get("id"),
                "charge_creation_failed",
                platform.get("owner_telegram_id"),
                f"charge_id={charge_id} error={error_type}: {error_msg[:200]}",
            )
    except Exception as dbe:
        log.warning("alert_payment_failure: could not log to platform_audit_log: %s", dbe)

    # Send Telegram alert to superadmins
    await notify_superadmins(msg)

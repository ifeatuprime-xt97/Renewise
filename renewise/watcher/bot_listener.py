"""
renewise/watcher/bot_listener.py

Redis pub/sub listener that runs inside the bot process (or as a sidecar).

The watcher workers publish bot actions to the BOT_ACTIONS_CHANNEL Redis
channel.  This listener subscribes, deserialises each message, and calls
the appropriate Telegram Bot API method.

This decouples the watcher workers (which have no Telegram context) from the
bot process (which holds the Application instance and its HTTP session).

Actions handled
───────────────
approve_and_welcome  — approve join request + send welcome DM
send_renewal_reminder — DM the user a renewal reminder with [Renew Now] button
kick_member          — ban then immediately unban (Telegram kick pattern)

Integration
───────────
Call start_bot_listener(application) from bot.py's post_init hook.
It spawns a background asyncio task that runs for the lifetime of the process.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import redis.asyncio as aioredis
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from renewise.watcher.config import REDIS_URL, BOT_ACTIONS_CHANNEL
from renewise.db.queries import get_group_by_id
from renewise.watcher.db import get_vault_registration

log = logging.getLogger(__name__)


async def _handle_approve_and_welcome(
    app: Application,
    payload: dict[str, Any],
) -> None:
    vault_address = payload.get("vault_address", "")
    reg = await get_vault_registration(vault_address)
    if not reg:
        log.warning("approve_and_welcome: no registry for vault %s", vault_address)
        return

    user_id  = reg["user_id"]
    group_id = reg["group_id"]

    # Resolve DB user_id → telegram_user_id + chat context in one query
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        row = await db.fetchrow(
            "SELECT u.telegram_user_id, g.telegram_chat_id, g.price, "
            "g.billing_interval_days "
            "FROM users u JOIN subscriptions s ON s.user_id=u.id "
            "JOIN groups g ON g.id=s.group_id "
            "WHERE u.id=$1 AND g.id=$2",
            user_id, group_id,
        )

    if not row:
        log.warning("approve_and_welcome: no user/group row for user_id=%d group_id=%d", user_id, group_id)
        return

    tg_user_id = row["telegram_user_id"]
    chat_id    = row["telegram_chat_id"]

    # Delete the "partial payment" warning if one was sent earlier — it's stale now
    insuf_msg_id = app.bot_data.get("insufficient_msgs", {}).pop((user_id, group_id), None)
    if insuf_msg_id:
        try:
            await app.bot.delete_message(chat_id=tg_user_id, message_id=insuf_msg_id)
            log.info("Deleted insufficient-payment msg | user=%d group=%d msg=%d",
                     tg_user_id, group_id, insuf_msg_id)
        except Exception as exc:
            log.debug("Could not delete insufficient-payment msg user=%d: %s", tg_user_id, exc)

    # Approve the pending join request
    try:
        await app.bot.approve_chat_join_request(chat_id=chat_id, user_id=tg_user_id)
        log.info("Approved join request | user=%d chat=%d", tg_user_id, chat_id)
    except Exception as exc:
        log.warning("approve_chat_join_request failed (may already be approved): %s", exc)

    # Send welcome DM — and delete the QR/payment-details message if still present
    try:
        group = await get_group_by_id(group_id)
        # aiosqlite.Row supports [] but not .get() — use dict() conversion for safety
        group_dict = dict(group) if group else {}
        group_name = group_dict.get("chat_title") or str(group_dict.get("telegram_chat_id", "the group"))

        # Delete the QR payment message from pending_joins so chat stays clean
        pending = app.bot_data.get("pending_joins", {}).get(tg_user_id, {})
        pay_msg_id = pending.get("payment_msg_id") if isinstance(pending, dict) else None
        if pay_msg_id:
            try:
                await app.bot.delete_message(chat_id=tg_user_id, message_id=pay_msg_id)
            except Exception:
                pass
        app.bot_data.get("pending_joins", {}).pop(tg_user_id, None)

        await app.bot.send_message(
            chat_id=tg_user_id,
            text=(
                "🎉 <b>Payment confirmed!</b>\n\n"
                f"You're now a member of <b>{group_name}</b>. Welcome! 🚀\n\n"
                f"Your subscription renews every {row['billing_interval_days']} days."
            ),
            parse_mode="HTML",
        )
        log.info("Welcome DM sent | user=%d", tg_user_id)
    except Exception as exc:
        log.warning("Welcome DM failed for user %d: %s", tg_user_id, exc)


async def _handle_renewal_reminder(
    app: Application,
    payload: dict[str, Any],
) -> None:
    tg_user_id      = payload.get("telegram_user_id")
    group_id        = payload.get("group_id")
    subscription_id = payload.get("subscription_id")
    renewal_cycle   = payload.get("renewal_cycle", "")

    if not tg_user_id or not group_id:
        return

    group = await get_group_by_id(group_id)
    if not group:
        log.warning("_handle_renewal_reminder: group %d not found", group_id)
        return

    group_dict  = dict(group)
    group_title = group_dict.get("chat_title") or f"Group {group_id}"

    # Compute days remaining until renewal
    days_remaining = "soon"
    if renewal_cycle:
        try:
            from datetime import datetime, timezone
            renewal_dt = datetime.fromisoformat(renewal_cycle.replace("Z", "+00:00"))
            if renewal_dt.tzinfo is None:
                renewal_dt = renewal_dt.replace(tzinfo=timezone.utc)
            delta = renewal_dt - datetime.now(timezone.utc)
            d = delta.days
            days_remaining = f"in {d} day{'s' if d != 1 else ''}" if d > 0 else "today"
        except Exception:
            pass

    # Use the same renew_{group_id} deep-link pattern used by /start My Subscriptions
    # This routes through cb_renew → _send_renewal_payment (proven working path)
    bot_username = (await app.bot.get_me()).username
    renew_url = f"https://t.me/{bot_username}?start=renew_{group_id}"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Renew Now", url=renew_url)
    ]])

    try:
        sent = await app.bot.send_message(
            chat_id=tg_user_id,
            text=(
                f"⏰ <b>Your subscription to {group_title} renews {days_remaining}.</b>\n\n"
                "Tap <b>Renew Now</b> to generate a payment link and keep your access."
            ),
            parse_mode="HTML",
            reply_markup=kb,
        )
        # Store the message_id so it can be deleted once renewal is confirmed
        from renewise.watcher.db import record_reminder_sent
        await record_reminder_sent(subscription_id, renewal_cycle, message_id=sent.message_id)
        log.info("Renewal reminder sent | user=%d sub=%d group=%d msg=%d",
                 tg_user_id, subscription_id, group_id, sent.message_id)
    except Exception as exc:
        log.warning("Renewal reminder DM failed for user %d: %s", tg_user_id, exc)


async def _handle_kick_member(
    app: Application,
    payload: dict[str, Any],
) -> None:
    tg_user_id  = payload.get("telegram_user_id")
    chat_id     = payload.get("telegram_chat_id")
    sub_id      = payload.get("subscription_id")

    if not tg_user_id or not chat_id:
        return

    try:
        # Telegram kick pattern: ban then immediately unban
        # This removes the user without a permanent ban so they can rejoin later.
        await app.bot.ban_chat_member(chat_id=chat_id, user_id=tg_user_id)
        await app.bot.unban_chat_member(chat_id=chat_id, user_id=tg_user_id, only_if_banned=True)
        log.info("Kicked user | user=%d chat=%d sub=%d", tg_user_id, chat_id, sub_id)
    except Exception as exc:
        log.error("Kick failed for user %d in chat %d: %s", tg_user_id, chat_id, exc)

    # Notify the user
    try:
        await app.bot.send_message(
            chat_id=tg_user_id,
            text=(
                "😔 <b>Your subscription has expired.</b>\n\n"
                "You've been removed from the group. "
                "You can rejoin at any time by requesting access and renewing your subscription."
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        log.warning("Expiry DM failed for user %d: %s", tg_user_id, exc)


async def _handle_rate_stale_alert(
    app: Application,
    payload: dict[str, Any],
) -> None:
    hours_old = payload.get("hours_old", 0)
    current_price = payload.get("current_price", 0)
    
    from renewise.config import ALLOWED_SUPERADMIN_IDS
    
    text = (
        f"🚨 <b>GRAM/USD Rate Feed Stale!</b>\n\n"
        f"The cached rate is currently <b>{hours_old:.1f} hours old</b>.\n"
        f"Fallback rate in use: <b>${current_price:.2f} USD</b>\n\n"
        f"<i>Please investigate the TonCenter API or CoinGecko availability.</i>"
    )
    
    for admin_id in ALLOWED_SUPERADMIN_IDS:
        try:
            await app.bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="HTML",
            )
            log.info("Sent rate stale alert DM to superadmin %s", admin_id)
        except Exception as exc:
            log.warning("Failed to send rate stale alert DM to %s: %s", admin_id, exc)

async def _handle_renewal_confirmed(
    app: Application,
    payload: dict[str, Any],
) -> None:
    vault_address    = payload.get("vault_address", "")
    new_renewal_date = payload.get("new_renewal_date", "a later date")
    subscription_id  = payload.get("subscription_id")
    old_renewal_cycle = payload.get("old_renewal_cycle", "")

    reg = await get_vault_registration(vault_address)
    if not reg:
        log.warning("renewal_confirmed: no registry for vault %s", vault_address)
        return

    user_id = reg["user_id"]
    if subscription_id is None:
        subscription_id = reg["subscription_id"]

    # Resolve DB user_id → telegram_user_id
    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        user_row = await db.fetchrow(
            "SELECT telegram_user_id FROM users WHERE id=$1", user_id
        )

    if not user_row:
        log.warning("renewal_confirmed: no user for user_id=%d", user_id)
        return

    tg_user_id = user_row["telegram_user_id"]
    group_id   = reg["group_id"]

    # ── Delete the "partial payment" warning if one was sent earlier ──────────
    insuf_msg_id = app.bot_data.get("insufficient_msgs", {}).pop((user_id, group_id), None)
    if insuf_msg_id:
        try:
            await app.bot.delete_message(chat_id=tg_user_id, message_id=insuf_msg_id)
            log.info("Deleted insufficient-payment msg | user=%d group=%d msg=%d",
                     tg_user_id, group_id, insuf_msg_id)
        except Exception as exc:
            log.debug("Could not delete insufficient-payment msg user=%d: %s", tg_user_id, exc)

    # ── Delete the renewal reminder message ───────────────────────────────────
    if subscription_id and old_renewal_cycle:
        try:
            from renewise.watcher.db import get_reminder_message_id
            reminder_msg_id = await get_reminder_message_id(subscription_id, old_renewal_cycle)
            if reminder_msg_id:
                await app.bot.delete_message(chat_id=tg_user_id, message_id=reminder_msg_id)
                log.info("Deleted renewal reminder msg | user=%d msg=%d", tg_user_id, reminder_msg_id)
        except Exception as exc:
            log.debug("Could not delete renewal reminder msg for user %d: %s", tg_user_id, exc)

    # ── Delete the renewal QR/payment message from pending_joins ─────────────
    pending = app.bot_data.get("pending_joins", {}).get(tg_user_id, {})
    pay_msg_id = pending.get("payment_msg_id") if isinstance(pending, dict) else None
    if pay_msg_id:
        try:
            await app.bot.delete_message(chat_id=tg_user_id, message_id=pay_msg_id)
            log.info("Deleted renewal QR msg | user=%d msg=%d", tg_user_id, pay_msg_id)
        except Exception as exc:
            log.debug("Could not delete renewal QR msg for user %d: %s", tg_user_id, exc)
    app.bot_data.get("pending_joins", {}).pop(tg_user_id, None)

    # ── Send clean renewal confirmation ───────────────────────────────────────
    try:
        await app.bot.send_message(
            chat_id=tg_user_id,
            text=(
                "✅ <b>Subscription Renewed!</b>\n\n"
                f"Your payment was received. You now have access until <b>{new_renewal_date}</b> UTC.\n"
                "Thank you for your support! 🚀"
            ),
            parse_mode="HTML",
        )
        log.info("Renewal confirmation DM sent | user=%d", tg_user_id)
    except Exception as exc:
        log.warning("Renewal confirmation DM failed for user %d: %s", tg_user_id, exc)


async def _handle_insufficient_payment(
    app: Application,
    payload: dict[str, Any],
) -> None:
    user_id       = payload.get("user_id")       # DB user id
    group_id      = payload.get("group_id")
    amount_nano   = payload.get("amount_nano", 0)
    required_nano = payload.get("required_nano", 0)

    if not user_id or not group_id:
        return

    from renewise.db.connection import _db as _conn
    async with _conn() as db:
        row = await db.fetchrow(
            "SELECT u.telegram_user_id, g.chat_title "
            "FROM users u JOIN groups g ON g.id=$1 WHERE u.id=$2",
            group_id, user_id,
        )

    if not row:
        log.warning("_handle_insufficient_payment: no user/group row for user_id=%d group_id=%d", user_id, group_id)
        return

    tg_user_id = row["telegram_user_id"]
    group_name = row["chat_title"] or "the group"

    shortfall_ton = (required_nano - amount_nano) / 1_000_000_000
    sent_ton      = amount_nano / 1_000_000_000
    required_ton  = required_nano / 1_000_000_000

    try:
        sent = await app.bot.send_message(
            chat_id=tg_user_id,
            text=(
                f"⚠️ <b>Payment received but amount is too low.</b>\n\n"
                f"We received <b>{sent_ton:.4f} GRAM</b> but need "
                f"<b>{required_ton:.4f} GRAM</b> for <b>{group_name}</b>.\n\n"
                f"You're short by <b>{shortfall_ton:.4f} GRAM</b>.\n\n"
                f"Please send a new payment for the full amount using the same "
                f"payment link. Your previous transaction cannot be topped up."
            ),
            parse_mode="HTML",
        )
        # Store so the successful-payment path can delete this warning
        app.bot_data.setdefault("insufficient_msgs", {})[(user_id, group_id)] = sent.message_id
        log.info("Insufficient payment DM sent | user_id=%d group=%d msg=%d",
                 user_id, group_id, sent.message_id)
    except Exception as exc:
        log.warning("Insufficient payment DM failed for user %d: %s", tg_user_id, exc)


# ── Dispatch table ────────────────────────────────────────────────────────────

_HANDLERS = {
    "approve_and_welcome":   _handle_approve_and_welcome,
    "renewal_confirmed":     _handle_renewal_confirmed,
    "send_renewal_reminder": _handle_renewal_reminder,
    "kick_member":           _handle_kick_member,
    "rate_stale_alert":      _handle_rate_stale_alert,
    "insufficient_payment":  _handle_insufficient_payment,
}


async def _listener_loop(app: Application) -> None:
    try:
        r = aioredis.from_url(REDIS_URL)
        pubsub = r.pubsub()
        await pubsub.subscribe(BOT_ACTIONS_CHANNEL)
        log.info("Bot listener subscribed to channel '%s'", BOT_ACTIONS_CHANNEL)
    except Exception as exc:
        log.warning("Bot listener could not connect to Redis: %s", exc)
        return

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                data    = json.loads(message["data"])
                action  = data.pop("action", "")
                handler = _HANDLERS.get(action)
                if handler:
                    await handler(app, data)
                else:
                    log.warning("Unknown bot action: %s", action)
            except Exception as exc:
                log.error("Bot listener error: %s", exc)
    except asyncio.CancelledError:
        # Task cancelled on shutdown — unsubscribe cleanly before exiting.
        log.info("Bot listener cancelled, unsubscribing from Redis.")
        try:
            await pubsub.unsubscribe(BOT_ACTIONS_CHANNEL)
            await pubsub.close()
        except Exception:
            pass
        raise  # re-raise so the task is marked done correctly


def start_bot_listener(app: Application) -> asyncio.Task:
    """
    Start the listener as a background asyncio task.
    Call from bot.py's post_init hook:

        from renewise.watcher.bot_listener import start_bot_listener
        start_bot_listener(application)
    """
    task = asyncio.create_task(_listener_loop(app))
    log.info("Bot listener task started")
    return task

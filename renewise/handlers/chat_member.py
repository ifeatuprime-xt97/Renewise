"""
Handles my_chat_member updates:
  - Bot promoted to admin  → record grant event; DM the adder pointing to /start
  - Bot demoted / removed  → freeze group + DM registered admin

Also handles chat_member updates:
  - A member leaves or is banned → cancel their subscription so they are not
    shown as active and do not receive renewal reminders for a group they left.
"""
from __future__ import annotations
import logging
from telegram import Update, ChatMemberAdministrator, ChatMemberMember, ChatMemberLeft, ChatMemberBanned
from telegram.ext import ContextTypes
from renewise.db import queries

log = logging.getLogger(__name__)

# Required Bot API permission fields by chat type.
# can_invite_users      = Add Users + Process Join Requests
# can_manage_chat       = Manage Chat (stored as can_manage_chat in DB)
# can_post_messages     = Manage Messages (channels only)
GROUP_REQUIRED   = ("can_invite_users", "can_manage_chat")
CHANNEL_REQUIRED = ("can_invite_users", "can_manage_chat", "can_post_messages")


def _required_perms(chat_type: str) -> tuple[str, ...]:
    return CHANNEL_REQUIRED if chat_type == "channel" else GROUP_REQUIRED


async def handle_my_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.my_chat_member
    if not change:
        return

    chat       = change.chat
    new_status = change.new_chat_member
    old_status = change.old_chat_member
    adder      = change.from_user

    # ── Bot gained admin ──────────────────────────────────────────────────────
    if isinstance(new_status, ChatMemberAdministrator):
        # Capture the exact permissions granted
        can_invite   = bool(getattr(new_status, "can_invite_users",  False))
        can_manage   = bool(getattr(new_status, "can_manage_chat",   False))
        can_post     = bool(getattr(new_status, "can_post_messages", False))

        # Check whether all required permissions are present BEFORE touching the groups table.
        # We do NOT upsert_group here when permissions are missing the group must not appear
        # in "My Networks" until the paywall setup wizard completes successfully.
        required = _required_perms(chat.type)
        missing  = [p for p in required if not getattr(new_status, p, False)]

        # Persist the grant event regardless used by "Done Check Now" detection
        await queries.record_admin_grant(
            telegram_chat_id=chat.id,
            chat_title=chat.title or str(chat.id),
            chat_type=chat.type,
            from_user_id=adder.id,
            can_invite_users=can_invite,
            can_manage_chat=can_manage,
            can_post_messages=can_post,
        )

        if missing:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            perm_labels = {
                "can_invite_users":  "Add Users / Process Join Requests",
                "can_manage_chat":   "Ban Users",
                "can_post_messages": "Post Messages",
            }
            missing_str = "\n".join(f"  ❌ {perm_labels.get(p, p)}" for p in missing)
            msg = (
                f"⚠️ I've been added as admin to <b>{chat.title}</b>, "
                f"but I'm missing required permissions:\n\n"
                f"{missing_str}\n\n"
                "Please grant these permissions in the group/channel settings, "
                "then tap <b>✅ I've Done It</b> below."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ I've Done It", callback_data="pw:check_now")],
                [InlineKeyboardButton("🔙 Cancel Setup",  callback_data="pw:back_to_start")],
            ])
            paywall_msg_id = ctx.application.user_data.get(adder.id, {}).get("paywall_msg_id")
            success = False
            if paywall_msg_id:
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=adder.id,
                        message_id=paywall_msg_id,
                        text=msg,
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                    success = True
                except Exception:
                    pass
            if not success:
                try:
                    await ctx.bot.send_message(adder.id, msg, parse_mode="HTML", reply_markup=kb)
                except Exception as e:
                    log.warning("Could not DM adder %s: %s", adder.id, e)
        else:
            # All permissions present now it's safe to record the group in the DB.
            # We defer upsert_group until here so that an incomplete admin grant
            # (missing permissions) never creates an 'active' group that would
            # appear in "My Networks" before setup is complete.
            await queries.upsert_group(
                telegram_chat_id=chat.id,
                admin_telegram_id=adder.id,
                chat_type=chat.type,
            )

            # Check if this is a re-add to a previously frozen group.
            # If so, the group stays 'frozen' until activate_paywall() runs.
            # DM note tells the admin they need to re-run the setup wizard.
            existing_group = await queries.get_group_by_chat_id(chat.id)
            is_reactivation = existing_group and existing_group["status"] == "frozen"

            # Fetch the grant we just recorded so we have its DB id
            from renewise.db.queries import get_recent_admin_grants
            grants = await get_recent_admin_grants(adder.id, minutes=2)
            matching = [g for g in grants if g["telegram_chat_id"] == chat.id]
            grant_id = matching[0]["id"] if matching else None

            chat_kind = "channel" if chat.type == "channel" else "group"
            kind_label = "Channel" if chat.type == "channel" else "Group"
            icon = "📢" if chat.type == "channel" else "👥"

            if grant_id:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "✅ Yes, set up paywall",
                        callback_data=f"pw:grant_confirm:{grant_id}"
                    )],
                    [InlineKeyboardButton(
                        "❌ No, redo setup",
                        callback_data="start:create_paywall"
                    )],
                ])
                msg = (
                    f"✅ I'm now admin in <b>{chat.title}</b> with all required permissions!\n\n"
                    + (
                        "⚠️ This group was previously paused. Completing the setup wizard below "
                        "will reactivate it and resume accepting new members.\n\n"
                        if is_reactivation else ""
                    )
                    + f"Is this the right {chat_kind} to set up a paywall for?\n\n"
                    f"📌 <b>{chat.title}</b> ({kind_label})"
                )
                paywall_msg_id = ctx.application.user_data.get(adder.id, {}).get("paywall_msg_id")
                success = False
                if paywall_msg_id:
                    try:
                        await ctx.bot.edit_message_text(
                            chat_id=adder.id,
                            message_id=paywall_msg_id,
                            text=msg,
                            parse_mode="HTML",
                            reply_markup=kb,
                        )
                        success = True
                    except Exception:
                        pass
                if not success:
                    try:
                        await ctx.bot.send_message(adder.id, msg, parse_mode="HTML", reply_markup=kb)
                    except Exception as e:
                        log.warning("Could not DM adder %s: %s", adder.id, e)
            else:
                # Fallback: no grant found, send manual instructions
                fallback_msg = (
                    f"✅ I'm now admin in <b>{chat.title}</b> with all required permissions.\n\n"
                    f"Head back to our DM and tap <b>✅ Done Check Now</b> to continue "
                    f"setting up your {chat_kind} paywall."
                )
                paywall_msg_id = ctx.application.user_data.get(adder.id, {}).get("paywall_msg_id")
                success = False
                if paywall_msg_id:
                    try:
                        await ctx.bot.edit_message_text(
                            chat_id=adder.id,
                            message_id=paywall_msg_id,
                            text=fallback_msg,
                            parse_mode="HTML"
                        )
                        success = True
                    except Exception:
                        pass
                if not success:
                    try:
                        await ctx.bot.send_message(adder.id, fallback_msg, parse_mode="HTML")
                    except Exception as e:
                        log.warning("Could not DM adder %s: %s", adder.id, e)
        return

    # ── Bot lost admin or was removed ─────────────────────────────────────────
    was_admin  = isinstance(old_status, ChatMemberAdministrator)
    was_member = isinstance(old_status, ChatMemberMember)
    is_gone    = isinstance(new_status, (ChatMemberMember, ChatMemberLeft, ChatMemberBanned))

    # Freeze on any removal: covers both demotion-from-admin AND the channel/group
    # being deleted while the bot was a plain member. Telegram sends a ChatMemberLeft
    # or ChatMemberBanned event in both cases — we freeze regardless of prior role so
    # the group never stays 'active' in the DB after the bot is no longer in the chat.
    if (was_admin or was_member) and is_gone:
        group = await queries.get_group_by_chat_id(chat.id)

        # If the group is already paused/suspended, this removal was triggered by the
        # intentional delete flow (suspend → leave_chat). Skip freeze and the
        # warning DM — the admin already knows what's happening.
        if group and group["status"] in ("paused", "suspended"):
            return

        await queries.freeze_group(chat.id)
        if group:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📖 How to re-add me", callback_data="start:create_paywall")
            ]])
            warn = (
                f"⚠️ I've been removed from <b>{chat.title}</b>.\n\n"
                "Here's what this means:\n"
                "  • All new join requests are <b>paused</b> — no one can join until I'm back\n"
                "  • Existing subscribers keep their access for now\n"
                "  • Nothing is deleted — your paywall settings are safe\n\n"
                "To resume, simply re-add me as admin with the same permissions and I'll pick up where I left off."
            )
            try:
                await ctx.bot.send_message(
                    group["admin_telegram_id"], warn, parse_mode="HTML", reply_markup=kb
                )
            except Exception as e:
                log.warning("Could not DM admin %s: %s", group["admin_telegram_id"], e)
        await queries.audit(
            group_id=group["id"] if group else None,
            action="bot_removed_or_demoted",
            actor_id=adder.id,
            details={"chat_id": chat.id, "chat_title": chat.title},
        )


async def handle_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fires on chat_member updates — i.e. changes to OTHER members' status, not
    the bot's own status (that is handled by handle_my_chat_member above).

    When a subscriber voluntarily leaves or is banned from a paywalled group,
    cancel their subscription so:
      - They no longer appear as 'active' in the admin dashboard
      - They do not receive renewal reminders for a group they have left
      - /start correctly stops showing them as a current member

    We only cancel — we do NOT refund. Refunds are a separate flow.
    We do NOT cancel 'comped' subscriptions — those are intentional free grants.
    """
    change = update.chat_member
    if not change:
        return

    chat       = change.chat
    new_status = change.new_chat_member
    old_status = change.old_chat_member
    affected   = new_status.user   # the member whose status changed

    # We only care when someone who was an active member left or was banned.
    was_member = isinstance(old_status, (ChatMemberMember, ChatMemberAdministrator))
    now_gone   = isinstance(new_status, (ChatMemberLeft, ChatMemberBanned))

    if not (was_member and now_gone):
        return

    # Ignore bot events — those are handled by handle_my_chat_member
    if affected.is_bot:
        return

    group = await queries.get_group_by_chat_id(chat.id)
    if not group:
        return   # not a paywalled group

    user_db = await queries.get_user_by_telegram_id(affected.id)
    if not user_db:
        return   # never had a subscription

    sub = await queries.get_subscription(user_db["id"], group["id"])
    if not sub:
        return   # no subscription row at all

    # Only cancel paid/pending subscriptions — leave 'comped' alone
    if sub["status"] in ("active", "pending", "frozen"):
        await queries.cancel_subscription(user_db["id"], group["id"])
        await queries.audit(
            group_id=group["id"],
            action="subscription_cancelled_member_left",
            actor_id=affected.id,
            details={
                "telegram_user_id": affected.id,
                "previous_status":  sub["status"],
                "trigger":          "chat_member_left_or_banned",
            },
        )
        log.info(
            "Subscription cancelled — member left/banned | user=%d group=%d chat=%d",
            affected.id, group["id"], chat.id,
        )

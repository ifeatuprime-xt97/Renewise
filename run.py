"""
run.py  —  start BOTH bots + in-process payment watcher in a single process.

Usage:
    python run.py

Three coroutines run concurrently via asyncio.gather:
  1. Main bot        (BOT_TOKEN)
  2. Superadmin bot  (SUPERADMIN_BOT_TOKEN)
  3. Payment watcher (polls TonCenter every POLL_INTERVAL_SECONDS)

On Render, a keep-alive HTTP server also binds $PORT and self-pings so
the free-tier web service does not spin down after 15 minutes idle.

No Redis, no RQ, no extra terminals required.
For production horizontal scaling, swap the watcher for the separate
watcher.py + worker.py + Redis stack instead.
"""
import asyncio
import sys
import logging

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("run")


async def run_main_bot() -> None:
    """Async wrapper around the main bot Application."""
    from renewise.config import BOT_TOKEN
    from renewise.db.schema import init_db
    from renewise.bot import post_init, cmd_start, cb_start_my_groups, cb_start_how_it_works, cb_start_back
    from renewise.bot import cb_terms_accept, cb_terms_decline
    from renewise.handlers.chat_member import handle_my_chat_member
    from renewise.handlers.join_request import handle_join_request, cb_pay_now, cb_ive_paid, cb_cancel_payment
    from renewise.handlers.create_paywall import build_create_paywall_handler
    from renewise.handlers.admin_menu import register_menu_callbacks
    from renewise.handlers.refund import cb_refund_prompt, handle_refund_wallet_message
    from renewise.watcher.inprocess_watcher import poll_vaults_inprocess
    from telegram.ext import (
        Application, ChatMemberHandler, ChatJoinRequestHandler,
        CallbackQueryHandler, CommandHandler, MessageHandler, filters,
    )

    # post_init_with_watcher wraps the existing post_init and additionally
    # launches the payment watcher task once the Application is fully ready.
    async def post_init_with_watcher(application: Application) -> None:
        # Run the original post_init first (DB init, Redis listener, etc.)
        try:
            await post_init(application)
        except Exception as exc:
            log.error("post_init failed: %s", exc, exc_info=True)

        # Start the in-process payment watcher as a background task.
        try:
            watcher_task = asyncio.create_task(
                poll_vaults_inprocess(application),
                name="inprocess_watcher",
            )
            application.bot_data["watcher_task"] = watcher_task
            log.info("▶ In-process payment watcher started.")
        except Exception as exc:
            log.error("Failed to start in-process watcher: %s", exc, exc_info=True)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_terms_accept,       pattern=r"^terms:accept$"))
    app.add_handler(CallbackQueryHandler(cb_terms_decline,      pattern=r"^terms:decline$"))
    app.add_handler(CallbackQueryHandler(cb_start_my_groups,    pattern=r"^start:my_groups$"))
    app.add_handler(CallbackQueryHandler(cb_start_how_it_works, pattern=r"^start:how_it_works$"))
    app.add_handler(CallbackQueryHandler(cb_start_back,         pattern=r"^start:back$"))
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    app.add_handler(CallbackQueryHandler(cb_pay_now,         pattern=r"^join:pay_now$"))
    app.add_handler(CallbackQueryHandler(cb_ive_paid,        pattern=r"^join:paid$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_payment,  pattern=r"^join:cancel$"))
    app.add_handler(build_create_paywall_handler())
    register_menu_callbacks(app)

    # ── refund wallet collection ──────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(cb_refund_prompt, pattern=r"^refund:prompt_\d+$"))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_refund_wallet_message,
    ))

    log.info("▶ Main bot starting…")
    async with app:
        await app.start()
        await post_init_with_watcher(app)

        await app.updater.start_polling(allowed_updates=[
            "message", "callback_query", "chat_member",
            "my_chat_member", "chat_join_request",
        ])

        # Run until Ctrl+C / CancelledError
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            # Cancel background tasks first
            for task_key in ("watcher_task", "listener_task"):
                task = app.bot_data.get(task_key)
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

            # Stop the updater (stops polling) then stop the app.
            # Both must be called before `async with app` exits — __aexit__
            # calls shutdown() which raises RuntimeError if the app is still
            # in the "running" state when it is called.
            await app.updater.stop()
            await app.stop()
            # shutdown() is called automatically by __aexit__ after this block.


async def run_superadmin_bot() -> None:
    """Async wrapper around the superadmin bot Application."""
    from renewise.config import SUPERADMIN_BOT_TOKEN
    if not SUPERADMIN_BOT_TOKEN:
        log.warning("SUPERADMIN_BOT_TOKEN not set — superadmin bot skipped.")
        return

    # Import the superadmin bot's build function
    import renewise.superadmin.bot as sa_module
    app = sa_module.build_app()          # calls the builder inside superadmin/bot.py

    log.info("▶ Superadmin bot starting…")
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=["message", "callback_query"])
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await app.updater.stop()
            await app.stop()
            # shutdown() called automatically by __aexit__


async def main() -> None:
    # Bind $PORT immediately so Render's deploy health check succeeds
    # before DB init / Telegram getMe finish.
    from renewise.keepalive import start_keepalive_background

    ka_task = await start_keepalive_background()
    try:
        await asyncio.gather(
            run_main_bot(),
            run_superadmin_bot(),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down…")
        # Re-raise CancelledError so asyncio.run() knows all tasks are done.
        # Without this, the peer coroutine in gather() may be abandoned rather
        # than cancelled and awaited, which causes "Task was destroyed but it is
        # pending!" warnings on exit.
        raise
    finally:
        if ka_task is not None and not ka_task.done():
            ka_task.cancel()
            try:
                await ka_task
            except (asyncio.CancelledError, Exception):
                pass


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())

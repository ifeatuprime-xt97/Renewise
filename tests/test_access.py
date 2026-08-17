import asyncio
import logging
from telegram import Update, User, Message, Chat
from telegram.ext import Application, CommandHandler, TypeHandler, ApplicationHandlerStop
from renewise.config import ALLOWED_SUPERADMIN_IDS

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

async def access_control(update: Update, context):
    user_id = None
    if update.effective_user:
        user_id = update.effective_user.id
    
    print(f"-> access_control: checking user_id={user_id}")
    if user_id not in ALLOWED_SUPERADMIN_IDS:
        print(f"-> access_control: REJECTED (ApplicationHandlerStop)")
        raise ApplicationHandlerStop()
    print(f"-> access_control: ALLOWED")
    return None

async def start_cmd(update: Update, context):
    print("-> start_cmd: reached! Responding to /start")

async def main():
    ALLOWED_SUPERADMIN_IDS.clear()
    ALLOWED_SUPERADMIN_IDS.append(111111)

    app = Application.builder().token("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11").build()
    # Mock get_me to prevent InvalidToken error during initialization
    from unittest.mock import AsyncMock, patch
    from telegram.ext import MessageHandler, filters

    with patch('telegram.ext.ExtBot.get_me', new_callable=AsyncMock, return_value=User(id=123, first_name="TestBot", is_bot=True, username="testbot")):
        app.add_handler(TypeHandler(Update, access_control), group=-1)
        app.add_handler(MessageHandler(filters.ALL, start_cmd))
        await app.initialize()

        # Mock Update 1: Allowlisted
        u1 = User(id=111111, first_name="Allowed", is_bot=False)
        c1 = Chat(id=111111, type="private")
        m1 = Message(
            message_id=1, 
            date=None, 
            chat=c1, 
            from_user=u1, 
            text="/start"
        )
        up1 = Update(update_id=1, message=m1)

        print("=== TESTING ALLOWLISTED USER (ID: 111111) ===")
        await app.process_update(up1)

        # Mock Update 2: Not allowlisted
        u2 = User(id=999999, first_name="Blocked", is_bot=False)
        c2 = Chat(id=999999, type="private")
        m2 = Message(
            message_id=2, 
            date=None, 
            chat=c2, 
            from_user=u2, 
            text="/start"
        )
        up2 = Update(update_id=2, message=m2)

        print("\n=== TESTING NON-ALLOWLISTED USER (ID: 999999) ===")
        await app.process_update(up2)

        await app.shutdown()

asyncio.run(main())

import asyncio
import logging
from unittest.mock import AsyncMock, patch
from telegram import Update, User, Message, Chat, CallbackQuery, ChatJoinRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ChatJoinRequestHandler, TypeHandler
import aiosqlite

from renewise.config import ALLOWED_SUPERADMIN_IDS, DATABASE_PATH
from renewise.superadmin.bot import groups_cmd, sa_callback_handler
from renewise.handlers.join_request import handle_join_request
from renewise.db.queries import _db

logging.basicConfig(level=logging.ERROR, format='%(message)s')

async def main():
    ALLOWED_SUPERADMIN_IDS.clear()
    ALLOWED_SUPERADMIN_IDS.append(111)

    print("=== 1. Setup Test Group ===")
    async with _db() as db:
        # Create a test group
        await db.execute("INSERT OR REPLACE INTO groups (id, telegram_chat_id, admin_telegram_id, price, status) VALUES (999, -100123456, 111, 10, 'active')")
        await db.commit()
    print("Test group inserted (ID: 999, Chat ID: -100123456)")

    print("\n=== 2. Test set_group_status to 'suspended' ===")
    from renewise.superadmin.queries import set_group_status, get_group_details
    await set_group_status(999, 'suspended', 111)
    g = await get_group_details(999)
    print(f"Group 999 status after DB update: {g['status']}")

    print("\n=== 3. Test Superadmin UI Flow ===")
    # Reset to active to test suspend via UI
    await set_group_status(999, 'active', 111)

    app = Application.builder().token("123:ABC").build()
    with patch('telegram.ext.ExtBot.get_me', new_callable=AsyncMock, return_value=User(id=123, first_name="TestBot", is_bot=True, username="testbot")):
        app.add_handler(CommandHandler("groups", groups_cmd))
        app.add_handler(CallbackQueryHandler(sa_callback_handler, pattern="^sa_"))
        app.add_handler(ChatJoinRequestHandler(handle_join_request))
        await app.initialize()

        u = User(id=111, first_name="Admin", is_bot=False)
        c = Chat(id=111, type="private")

        # Mock edit_message_text and answer on the query
        mock_query = AsyncMock(spec=CallbackQuery)
        mock_query.data = "sa_suspend_999"
        mock_query.from_user = u

        up_cb = Update(update_id=1, callback_query=mock_query)
        # Manually set _effective_user so update.effective_user works
        up_cb._effective_user = u

        print("Simulating UI callback 'sa_suspend_999'...")
        await app.process_update(up_cb)
        
        g = await get_group_details(999)
        print(f"Group 999 status after UI callback: {g['status']}")
        if mock_query.answer.called and mock_query.edit_message_text.called:
             print("UI successfully answered callback and updated message!")

        print("\n=== 4. Test Chat Join Request ===")
        req_user = User(id=555, first_name="Joiner", is_bot=False)
        req_chat = Chat(id=-100123456, type="supergroup")
        join_req = ChatJoinRequest(chat=req_chat, from_user=req_user, date=None, user_chat_id=req_user.id)
        up_join = Update(update_id=2, chat_join_request=join_req)
        
        # We need to mock decline_chat_join_request on the ExtBot class
        with patch('telegram.ext.ExtBot.decline_chat_join_request', new_callable=AsyncMock) as mock_decline:
            print("Simulating real ChatJoinRequest on suspended group...")
            await app.process_update(up_join)
            
            # Check if decline was called
            if mock_decline.called:
                print("SUCCESS: decline_chat_join_request actually fired!")
                args, kwargs = mock_decline.call_args
                print(f"Called with kwargs: {kwargs}")
            else:
                print("FAILURE: decline_chat_join_request was NOT called.")

        await app.shutdown()

asyncio.run(main())

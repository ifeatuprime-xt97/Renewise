import asyncio
from telegram import Update, User, Chat, Message
from telegram.ext import ContextTypes
from renewise.bot import cmd_start
from renewise.db import queries
from renewise.db.queries import _db
from unittest.mock import AsyncMock, MagicMock

async def setup_test_data():
    telegram_id = 999999999
    
    # Clean up first
    async with _db() as db:
        await db.execute("DELETE FROM subscriptions WHERE user_id = (SELECT id FROM users WHERE telegram_user_id = ?)", (telegram_id,))
        await db.execute("DELETE FROM users WHERE telegram_user_id = ?", (telegram_id,))
        await db.execute("DELETE FROM groups WHERE admin_telegram_id = ?", (telegram_id,))
        await db.commit()
    
    # Insert user
    await queries.upsert_user(telegram_id, "TestUser", "testuser")
    user_db = await queries.get_user_by_telegram_id(telegram_id)
    
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    # Insert group
    async with _db() as db:
        cur = await db.execute(
            "INSERT INTO groups (admin_telegram_id, telegram_chat_id, price, price_usd_cents, billing_interval_days, payout_wallet_address, status, chat_title) "
            "VALUES (?, ?, 10, 999, 30, 'EQDtFpEwcFAEcRe5mLVh2N6C0x-_hJEM7W61_DKEIS065rSz', 'active', 'Test Group')",
            (telegram_id, -100123)
        )
        group_id = cur.lastrowid
        await db.commit()
        
    # Insert subscription
    async with _db() as db:
        await db.execute(
            "INSERT INTO subscriptions (user_id, group_id, status, price_locked_in) VALUES (?, ?, 'active', 10)",
            (user_db["id"], group_id)
        )
        await db.commit()
        
    return telegram_id, group_id

async def main():
    telegram_id, group_id = await setup_test_data()
    
    # Mock update
    update = MagicMock(spec=Update)
    update.effective_chat = MagicMock(spec=Chat)
    update.effective_chat.type = "private"
    
    update.effective_user = MagicMock(spec=User)
    update.effective_user.id = telegram_id 
    
    update.message = MagicMock(spec=Message)
    update.message.reply_text = AsyncMock()
    
    # Mock generate_payment_request to succeed and return a dummy payment
    class DummyPayment:
        payment_url = "ton://transfer/EQMockVault...&amount=700000000"
        amount = 0.7
        vault_address = "EQMockVault..."
        
    import sys
    sys.modules['renewise.services'] = MagicMock()
    sys.modules['renewise.services.payment'] = MagicMock()
    sys.modules['renewise.services.payment'].generate_payment_request = AsyncMock(return_value=DummyPayment())
    
    # Mock context with args
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.args = [f"renew_{group_id}"]
    
    print(f"Simulating: /start renew_{group_id}")
    await cmd_start(update, context)
    
    print("\nBot replied with:")
    for call in update.message.reply_text.call_args_list:
        text = call.args[0] if call.args else call.kwargs.get("text", "")
        print(f"--- MESSAGE TEXT ---\n{text}\n--------------------")
        
        reply_markup = call.kwargs.get("reply_markup")
        if reply_markup:
            for row in reply_markup.inline_keyboard:
                for btn in row:
                    print(f"[Button: {btn.text} -> {btn.url or btn.callback_data}]")

if __name__ == "__main__":
    asyncio.run(main())

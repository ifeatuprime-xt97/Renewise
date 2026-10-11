import asyncio
import os
from telegram import Bot
from dotenv import load_dotenv

load_dotenv()

async def main():
    bot = Bot(os.getenv("SUPERADMIN_BOT_TOKEN"))
    try:
        chat = await bot.get_chat(73635302)  # A known user ID from the screenshot
        print(f"Success: {chat.first_name} {chat.username}")
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())

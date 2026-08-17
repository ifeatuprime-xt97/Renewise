import asyncio
from renewise.db.schema import init_db

async def main():
    await init_db()
    print("Database initialized successfully.")

if __name__ == "__main__":
    asyncio.run(main())

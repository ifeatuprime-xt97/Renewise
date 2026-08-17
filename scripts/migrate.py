import asyncio
import aiosqlite
import os

from renewise.config import DATABASE_PATH
from renewise.db.schema import CREATE_GROUPS

async def migrate():
    print(f"Migrating database at {DATABASE_PATH}")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("PRAGMA foreign_keys=OFF")
        
        # 1. Rename old table
        await db.execute("ALTER TABLE groups RENAME TO old_groups")
        
        # 2. Create new table with updated constraints
        await db.execute(CREATE_GROUPS)
        
        # 3. Copy data
        await db.execute("""
            INSERT INTO groups (id, telegram_chat_id, admin_telegram_id, price, currency, billing_interval_days, payout_wallet_address, status, created_at)
            SELECT id, telegram_chat_id, admin_telegram_id, price, currency, billing_interval_days, payout_wallet_address, status, created_at
            FROM old_groups
        """)
        
        # 4. Drop old table
        await db.execute("DROP TABLE old_groups")
        
        await db.execute("PRAGMA foreign_keys=ON")
        await db.commit()
    print("Migration complete.")

if __name__ == "__main__":
    asyncio.run(migrate())

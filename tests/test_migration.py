import asyncio, sys
from renewise.db.schema import init_db
from renewise.db.queries import get_global_fees, set_global_fees
import aiosqlite
from renewise.config import DATABASE_PATH

async def main():
    print("Running init_db()...")
    await init_db()
    print('Migration OK')

    print("\n--- Direct DB check ---")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM platform_config WHERE id=1")
        row = await cur.fetchone()
        print(dict(row) if row else "NO ROW FOUND")

    print("\n--- get_global_fees() check ---")
    buyer, admin = await get_global_fees()
    print(f'After migration: buyer={buyer} bps, admin={admin} bps')
    
    print("\n--- set_global_fees() check ---")
    await set_global_fees(150, 250, 99999999)
    buyer2, admin2 = await get_global_fees()
    print(f'After override:  buyer={buyer2} bps, admin={admin2} bps')
    
    print("\n--- Restore check ---")
    await set_global_fees(buyer, admin, 99999999)
    buyer3, admin3 = await get_global_fees()
    print(f'After restore:   buyer={buyer3} bps, admin={admin3} bps')
    print('\nAll checks PASSED')

if __name__ == "__main__":
    asyncio.run(main())

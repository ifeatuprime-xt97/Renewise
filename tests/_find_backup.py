"""Find which DB file has the complete groups data."""
import asyncio, aiosqlite, os

dbs = ["data/renewise.db", "db.sqlite", "db.sqlite3"]

async def check(path):
    if not os.path.exists(path):
        return
    size = os.path.getsize(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        try:
            cur = await db.execute("SELECT id, telegram_chat_id, chat_title, chat_type, admin_telegram_id, payout_wallet_address, price_usd_cents, invite_link, billing_interval_days FROM groups ORDER BY id")
            groups = await cur.fetchall()
            print(f"\n{path} ({size} bytes) — {len(groups)} groups:")
            for g in groups:
                print(f"  id={g['id']} title={g['chat_title']!r} type={g['chat_type']} admin={g['admin_telegram_id']} price_cents={g['price_usd_cents']}")
                print(f"    wallet={g['payout_wallet_address']!r} invite={g['invite_link']!r}")
        except Exception as e:
            print(f"{path}: {e}")

async def main():
    for db in dbs:
        await check(db)

asyncio.run(main())

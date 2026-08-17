"""Recover deleted groups from audit log and other sources."""
import asyncio, aiosqlite
from renewise.config import DATABASE_PATH

async def main():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Check audit log for clues about deleted groups
        cur = await db.execute(
            "SELECT * FROM admin_audit_log WHERE group_id IS NULL ORDER BY created_at DESC LIMIT 30"
        )
        rows = await cur.fetchall()
        print(f"Audit log rows with group_id=NULL (recent): {len(rows)}")
        for r in rows:
            print(f"  id={r['id']} action={r['action']} actor={r['actor_telegram_id']} details={r['details']} at={r['created_at']}")

        # Check processed_tx_hashes for sub_id links
        cur = await db.execute("SELECT * FROM processed_tx_hashes")
        txs = await cur.fetchall()
        print(f"\nprocessed_tx_hashes: {len(txs)}")
        for t in txs:
            print(f"  {dict(t)}")

        # Check users
        cur = await db.execute("SELECT * FROM users")
        users = await cur.fetchall()
        print(f"\nUsers: {len(users)}")
        for u in users:
            print(f"  {dict(u)}")

        # Check subscriptions
        cur = await db.execute("SELECT * FROM subscriptions")
        subs = await cur.fetchall()
        print(f"\nSubscriptions: {len(subs)}")
        for s in subs:
            print(f"  {dict(s)}")

        # Check data/renewise.db (backup might exist)
        import os
        alt_paths = ["data/renewise.db", "db.sqlite", "db.sqlite3"]
        for p in alt_paths:
            if os.path.exists(p):
                print(f"\nBackup DB found: {p}")

asyncio.run(main())

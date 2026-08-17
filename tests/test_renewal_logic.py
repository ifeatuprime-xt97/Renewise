import asyncio
import aiosqlite
import datetime
import uuid
import sys
from unittest.mock import patch

# Add the project root to sys.path so we can import renewise modules
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from renewise.config import DATABASE_PATH
from renewise.watcher.tasks import process_payment

published_actions = []

def mock_publish_bot_action(action: str, payload: dict):
    published_actions.append({"action": action, "payload": payload})
    print(f"[Redis Mock] Published action: {action}")
    print(f"             Payload: {payload}")

def run_test():
    import random
    vault_addr = "EQTestVault" + str(uuid.uuid4())[:8]
    user_tg = random.randint(1000000, 99999999)
    group_tg = -1 * random.randint(10000000000, 99999999999)
    
    async def setup_db():
        async with aiosqlite.connect(DATABASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            
            # 1. Setup mock data
            cur = await db.execute(
                "INSERT INTO users (telegram_user_id) VALUES (?) RETURNING id", 
                (user_tg,)
            )
            user_id = (await cur.fetchone())["id"]
            
            cur = await db.execute(
                "INSERT INTO groups (telegram_chat_id, admin_telegram_id, price, billing_interval_days) VALUES (?, ?, 10, 30) RETURNING id",
                (group_tg, user_tg)
            )
            group_id = (await cur.fetchone())["id"]
            
            cur = await db.execute(
                "INSERT INTO subscriptions (user_id, group_id, status, price_locked_in, vault_address) VALUES (?, ?, 'pending', 10, ?) RETURNING id",
                (user_id, group_id, vault_addr)
            )
            sub_id = (await cur.fetchone())["id"]
            
            await db.execute(
                "INSERT INTO vault_registry (vault_address, subscription_id, user_id, group_id) VALUES (?, ?, ?, ?)",
                (vault_addr, sub_id, user_id, group_id)
            )
            await db.commit()
            return user_id, group_id, sub_id

    user_id, group_id, sub_id = asyncio.run(setup_db())

    print(f"\n--- Initial State ---")
    print(f"User ID: {user_id}, Group ID: {group_id}, Sub ID: {sub_id}, Vault: {vault_addr}")

    # Patch the Redis publisher in tasks.py
    with patch("renewise.watcher.tasks._publish_bot_action", side_effect=mock_publish_bot_action):
        
        # 2. Simulate First Payment
        print("\n--- Simulating First Payment ---")
        tx_hash_1 = "tx_hash_initial_" + str(uuid.uuid4())[:8]
        process_payment(vault_addr, tx_hash_1, 15000000000) # 15 TON (enough for 10 price + fees)

        async def get_dates():
            async with aiosqlite.connect(DATABASE_PATH) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT start_date, next_renewal_date FROM subscriptions WHERE id=?", (sub_id,))
                return await cur.fetchone()

        row1 = asyncio.run(get_dates())
        start_date_1 = row1["start_date"]
        renewal_date_1 = row1["next_renewal_date"]
            
        print(f"After Payment 1:")
        print(f"Start Date:        {start_date_1}")
        print(f"Next Renewal Date: {renewal_date_1}")
        
        assert published_actions[-1]["action"] == "approve_and_welcome", "Expected approve_and_welcome action for first payment"
        
        # Simulate some time passing by artificially moving the dates backward in the DB
        # so we can prove the second payment adds to the end, not to 'now'
        print("\n--- Simulating time pass (moving dates back 10 days) ---")
        
        async def adjust_dates():
            async with aiosqlite.connect(DATABASE_PATH) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("UPDATE subscriptions SET start_date = datetime(start_date, '-10 days'), next_renewal_date = datetime(next_renewal_date, '-10 days') WHERE id=?", (sub_id,))
                await db.commit()
                
                cur = await db.execute("SELECT start_date, next_renewal_date FROM subscriptions WHERE id=?", (sub_id,))
                return await cur.fetchone()

        row_adjusted = asyncio.run(adjust_dates())
        start_date_adj = row_adjusted["start_date"]
        renewal_date_adj = row_adjusted["next_renewal_date"]
            
        print(f"Adjusted Start Date:        {start_date_adj}")
        print(f"Adjusted Next Renewal Date: {renewal_date_adj}")

        # 3. Simulate Second Payment
        print("\n--- Simulating Second Payment (Renewal) ---")
        tx_hash_2 = "tx_hash_renewal_" + str(uuid.uuid4())[:8]
        process_payment(vault_addr, tx_hash_2, 15000000000)

        row2 = asyncio.run(get_dates())
        start_date_2 = row2["start_date"]
        renewal_date_2 = row2["next_renewal_date"]

        print(f"After Payment 2:")
        print(f"Start Date:        {start_date_2}")
        print(f"Next Renewal Date: {renewal_date_2}")

        # 4. Assertions
        print("\n--- Assertions ---")
        print("1. Checking if start_date remained untouched...")
        assert start_date_adj == start_date_2, f"Failed: start_date changed! {start_date_adj} != {start_date_2}"
        print("   ✅ start_date is IDENTICAL.")

        print("2. Checking if next_renewal_date is extended correctly...")
        # Difference between adjusted start date and new renewal date should be exactly 60 days
        sd = datetime.datetime.strptime(start_date_adj, "%Y-%m-%d %H:%M:%S")
        nd = datetime.datetime.strptime(renewal_date_2, "%Y-%m-%d %H:%M:%S")
        diff_days = (nd - sd).days
        assert diff_days == 60, f"Failed: Difference is {diff_days} days, expected 60 days"
        print(f"   ✅ next_renewal_date extended correctly ({diff_days} days from start_date).")
        
        print("3. Checking if correct bot action was published...")
        assert published_actions[-1]["action"] == "renewal_confirmed", f"Failed: Action was {published_actions[-1]['action']}"
        print(f"   ✅ renewal_confirmed action was published.")

        print("\n🎉 ALL TESTS PASSED! The logic works correctly.")

if __name__ == "__main__":
    run_test()

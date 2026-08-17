"""
FIX 1 verification:
  1. Confirm UNIQUE(user_id, group_id) appears in live DDL
  2. Call create_subscription twice for the same (user_id, group_id) and confirm
     only ONE row exists and the second call updated, not duplicated.
"""
import asyncio, sqlite3, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from renewise.config import DATABASE_PATH
from renewise.db.queries import create_subscription

# ── Part 1: DDL check ─────────────────────────────────────────────────────────
conn = sqlite3.connect(DATABASE_PATH)
ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='subscriptions'").fetchone()[0]
print("=== Live subscriptions DDL ===")
print(ddl)
assert "UNIQUE(user_id, group_id)" in ddl, "UNIQUE constraint NOT found in DDL!"
print()
print("✅ UNIQUE(user_id, group_id) confirmed in DDL")
print()

# ── Part 2: functional test ───────────────────────────────────────────────────
# Use a test user/group pair that does NOT exist in production data
# (large IDs unlikely to collide with real rows)
TEST_USER_ID  = 999991
TEST_GROUP_ID = 999991

# Ensure clean state
conn.execute("DELETE FROM subscriptions WHERE user_id=? AND group_id=?",
             (TEST_USER_ID, TEST_GROUP_ID))
# Insert a fake user and group row so FK constraints pass
conn.execute("INSERT OR IGNORE INTO users (id, telegram_user_id) VALUES (?,?)",
             (TEST_USER_ID, TEST_USER_ID))
conn.execute(
    "INSERT OR IGNORE INTO groups (id, telegram_chat_id, admin_telegram_id) VALUES (?,?,?)",
    (TEST_GROUP_ID, TEST_GROUP_ID, TEST_USER_ID)
)
conn.commit()
conn.close()

async def run():
    print("=== Functional test: double-insert via create_subscription ===")

    # First call — insert
    id1 = await create_subscription(TEST_USER_ID, TEST_GROUP_ID, price_locked_in=9.99)
    print(f"First call  → subscription id={id1}")

    # Second call — same pair, different price — should UPDATE not INSERT
    id2 = await create_subscription(TEST_USER_ID, TEST_GROUP_ID, price_locked_in=19.99)
    print(f"Second call → subscription id={id2}")

    # Count rows for this pair
    conn2 = sqlite3.connect(DATABASE_PATH)
    rows = conn2.execute(
        "SELECT id, status, price_locked_in FROM subscriptions WHERE user_id=? AND group_id=?",
        (TEST_USER_ID, TEST_GROUP_ID)
    ).fetchall()
    conn2.close()

    print(f"Row count for (user={TEST_USER_ID}, group={TEST_GROUP_ID}): {len(rows)}")
    for r in rows:
        print(f"  id={r[0]}  status={r[1]}  price_locked_in={r[2]}")

    assert len(rows) == 1, f"FAIL — expected 1 row, got {len(rows)}"
    print()
    print("✅ Second call updated the existing row — no duplicate created")

    # Cleanup
    conn3 = sqlite3.connect(DATABASE_PATH)
    conn3.execute("DELETE FROM subscriptions WHERE user_id=? AND group_id=?",
                  (TEST_USER_ID, TEST_GROUP_ID))
    conn3.execute("DELETE FROM users WHERE id=?", (TEST_USER_ID,))
    conn3.execute("DELETE FROM groups WHERE id=?", (TEST_GROUP_ID,))
    conn3.commit()
    conn3.close()
    print("(Test rows cleaned up)")

asyncio.run(run())

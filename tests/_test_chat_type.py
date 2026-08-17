"""
FIX — chat_type verification tests:
  1. upsert_group with chat_type='channel' writes and reads back correctly
  2. upsert_group on an existing row with new type updates it
  3. activate_paywall with chat_type persists correctly
  4. get_admin_detail (the function /api/my-groups calls) returns chat_type
  5. No row exists without chat_type
"""
import asyncio, sqlite3, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from renewise.config import DATABASE_PATH
from renewise.db.queries import upsert_group, activate_paywall, get_group_by_id

async def run():
    print("=== Test 1: upsert_group writes chat_type='channel' ===")
    gid = await upsert_group(
        telegram_chat_id=-9999001,
        admin_telegram_id=8888001,
        chat_type="channel",
    )
    conn = sqlite3.connect(DATABASE_PATH)
    row = conn.execute("SELECT id, chat_type FROM groups WHERE id=?", (gid,)).fetchone()
    print(f"  group id={row[0]}  chat_type={row[1]}")
    assert row[1] == "channel", f"Expected 'channel', got {row[1]}"
    print("  ✅ pass")

    print()
    print("=== Test 2: upsert_group on same chat_id updates type to 'supergroup' ===")
    gid2 = await upsert_group(
        telegram_chat_id=-9999001,
        admin_telegram_id=8888001,
        chat_type="supergroup",
    )
    assert gid == gid2, "Should return same id"
    row2 = conn.execute("SELECT chat_type FROM groups WHERE id=?", (gid,)).fetchone()
    print(f"  chat_type now={row2[0]}")
    assert row2[0] == "supergroup", f"Expected 'supergroup', got {row2[0]}"
    print("  ✅ pass")

    print()
    print("=== Test 3: activate_paywall writes chat_type='channel' ===")
    await activate_paywall(
        group_id=gid,
        price=0,
        billing_interval_days=30,
        payout_wallet_address="EQtest",
        chat_title="Test Channel",
        chat_type="channel",
    )
    row3 = conn.execute("SELECT chat_type, chat_title FROM groups WHERE id=?", (gid,)).fetchone()
    print(f"  chat_type={row3[0]}  chat_title={row3[1]}")
    assert row3[0] == "channel", f"Expected 'channel', got {row3[0]}"
    print("  ✅ pass")

    print()
    print("=== Test 4: get_admin_detail returns chat_type ===")
    from renewise.superadmin.queries import get_admin_detail
    detail = await get_admin_detail(8888001)
    for g in detail["groups"]:
        print(f"  group id={g['id']}  chat_type={g.get('chat_type')}  chat_title={g.get('chat_title')}")
        assert g.get("chat_type") is not None, "chat_type missing from get_admin_detail result!"
    print("  ✅ pass")

    print()
    print("=== Test 5: no groups row has NULL chat_type ===")
    nulls = conn.execute(
        "SELECT id, chat_title FROM groups WHERE chat_type IS NULL OR chat_type=''"
    ).fetchall()
    if nulls:
        print(f"  FAIL — rows with missing chat_type: {nulls}")
        assert False
    print("  ✅ all rows have chat_type set")

    # Cleanup test row
    conn.execute("DELETE FROM groups WHERE telegram_chat_id=-9999001")
    conn.commit()
    conn.close()
    print()
    print("=== All tests passed ===")

asyncio.run(run())

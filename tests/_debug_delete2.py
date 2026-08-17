"""Reproduce the exact api_group_delete logic to find the 500 cause."""
import asyncio, aiohttp
from renewise.config import DATABASE_PATH, BOT_TOKEN
from renewise.db.schema import init_db
from renewise.db import queries

async def simulate_delete(group_id: int, telegram_user_id: int):
    await init_db()

    group_row = await queries.get_group_by_id(group_id)
    if not group_row:
        print(f"Group {group_id} not found")
        return

    group = dict(group_row)
    chat_title       = group.get("chat_title") or f"Chat {group['telegram_chat_id']}"
    telegram_chat_id = group["telegram_chat_id"]

    print(f"group: id={group_id} title={chat_title!r} chat_id={telegram_chat_id}")

    # i. Pause
    await queries.set_group_status(group_id, "paused")
    print("set_group_status OK")

    # ii. Get subscribers
    subscribers = await queries.get_active_subscribers(group_id)
    print(f"get_active_subscribers OK, {len(subscribers)} row(s)")

    # Simulate the DM loop (don't actually send)
    for row in subscribers:
        try:
            uid = row["telegram_user_id"]
            print(f"  would DM user {uid}")
        except Exception as e:
            print(f"  ROW ACCESS ERROR: {e} — row type={type(row)} row={row}")

    # iii. Simulate leaveChat (don't actually call)
    print("leaveChat would fire here")

    # iv. delete_group
    try:
        await queries.delete_group(
            group_id=group_id,
            actor_id=telegram_user_id,
            group_title=chat_title,
            telegram_chat_id=telegram_chat_id,
        )
        print("delete_group OK")
    except Exception as e:
        print(f"delete_group FAILED: {type(e).__name__}: {e}")

# Use the first group that exists
async def main():
    await init_db()
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("SELECT id, admin_telegram_id FROM groups LIMIT 1")
        row = await cur.fetchone()
    if not row:
        print("No groups in DB")
        return
    print(f"Testing with group_id={row[0]} admin={row[1]}")
    await simulate_delete(row[0], row[1])

asyncio.run(main())

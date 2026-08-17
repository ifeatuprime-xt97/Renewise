"""
Migration: add chat_type column to groups table and back-fill from
recent_admin_grants where possible, then verify.

Run from the project root:
    python _migrate_chat_type.py
"""
import sqlite3, sys
sys.path.insert(0, '.')
from renewise.config import DATABASE_PATH

conn = sqlite3.connect(DATABASE_PATH)
conn.execute("PRAGMA foreign_keys=OFF")
conn.execute("PRAGMA journal_mode=WAL")

# ── Step 1: add the column (safe if already exists) ───────────────────────────
try:
    conn.execute("ALTER TABLE groups ADD COLUMN chat_type TEXT NOT NULL DEFAULT 'group'")
    conn.commit()
    print("Added chat_type column to groups.")
except Exception as e:
    print(f"Column already exists or error: {e}")

# ── Step 2: back-fill from recent_admin_grants ────────────────────────────────
# Join on telegram_chat_id; take the most recent grant per chat to get the type.
updated = conn.execute("""
    UPDATE groups
    SET chat_type = (
        SELECT rag.chat_type
        FROM recent_admin_grants rag
        WHERE rag.telegram_chat_id = groups.telegram_chat_id
        ORDER BY rag.granted_at DESC
        LIMIT 1
    )
    WHERE chat_type = 'group'
      AND EXISTS (
        SELECT 1 FROM recent_admin_grants rag
        WHERE rag.telegram_chat_id = groups.telegram_chat_id
      )
""").rowcount
conn.commit()
print(f"Back-filled {updated} rows from recent_admin_grants.")

# ── Step 3: show final state ──────────────────────────────────────────────────
rows = conn.execute(
    "SELECT id, telegram_chat_id, chat_title, chat_type, status FROM groups"
).fetchall()
print()
print("=== groups rows after migration ===")
for r in rows:
    print(f"  id={r[0]}  chat_id={r[1]}  title={r[2]!r:30s}  chat_type={r[3]}  status={r[4]}")

# ── Step 4: confirm DDL includes the column ───────────────────────────────────
ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='groups'").fetchone()[0]
print()
print("=== groups DDL ===")
print(ddl)
assert "chat_type" in ddl, "chat_type NOT in DDL after migration!"
print()
print("✅ chat_type column confirmed in live groups DDL.")
conn.close()

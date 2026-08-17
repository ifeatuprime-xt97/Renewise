import sqlite3, sys
sys.path.insert(0, '.')
from renewise.config import DATABASE_PATH

conn = sqlite3.connect(DATABASE_PATH)

print("=== groups DDL ===")
print(conn.execute("SELECT sql FROM sqlite_master WHERE name='groups'").fetchone()[0])
print()
print("=== current groups rows ===")
cols = [d[0] for d in conn.execute("PRAGMA table_info(groups)").fetchall()]
print("columns:", cols)
rows = conn.execute("SELECT id, telegram_chat_id, chat_title, status FROM groups").fetchall()
for r in rows:
    print(r)
conn.close()

import sqlite3, sys
sys.path.insert(0, '.')
from renewise.config import DATABASE_PATH

conn = sqlite3.connect(DATABASE_PATH)

print("=== CURRENT subscriptions DDL ===")
row = conn.execute("SELECT sql FROM sqlite_master WHERE name='subscriptions'").fetchone()
print(row[0])

print()
print("=== vault_registry in sqlite_master ===")
row2 = conn.execute("SELECT sql FROM sqlite_master WHERE name='vault_registry'").fetchone()
print(row2[0] if row2 else "NOT FOUND")

print()
print("=== row counts ===")
print("subscriptions:", conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0])
print("DATABASE_PATH:", DATABASE_PATH)
conn.close()

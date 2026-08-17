"""Step 1: Schema check + live groups table query"""
import sqlite3
import sys
import re

# 1. Schema pattern search (simulating Select-String)
print("=" * 60)
print("SELECT-STRING RESULTS: renewise/db/schema.py")
print("Pattern: buyer_fee_bps|admin_fee_bps")
print("=" * 60)

with open("renewise/db/schema.py", "r") as f:
    for i, line in enumerate(f, 1):
        if re.search(r"buyer_fee_bps|admin_fee_bps", line):
            print(f"renewise\\db\\schema.py:{i}: {line}", end="")

print()

# 2. Live DB query
print("=" * 60)
print("SQL: SELECT id, buyer_fee_bps, admin_fee_bps FROM groups;")
print("=" * 60)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
try:
    from renewise.config import DATABASE_PATH
    print(f"Database path: {DATABASE_PATH}")
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, buyer_fee_bps, admin_fee_bps FROM groups")
    rows = cur.fetchall()
    if not rows:
        print("(no rows — groups table is empty)")
    else:
        print(f"{'id':<6} {'buyer_fee_bps':<16} {'admin_fee_bps'}")
        print("-" * 40)
        for r in rows:
            print(f"{r['id']:<6} {str(r['buyer_fee_bps']):<16} {str(r['admin_fee_bps'])}")
    conn.close()
except Exception as e:
    print(f"ERROR: {e}")

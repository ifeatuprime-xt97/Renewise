"""
Migration: add UNIQUE(user_id, group_id) to the subscriptions table.

Strategy (same safe pattern used for the groups CHECK constraint fix):
  1. Create subscriptions_new with all columns + UNIQUE constraint
  2. Copy all rows
  3. Verify row counts match BEFORE touching anything
  4. DROP old table
  5. RENAME new table into place

Run from the project root:
    python _migrate_subscriptions_unique.py
"""
import sqlite3
import sys
import os

sys.path.insert(0, '.')
from renewise.config import DATABASE_PATH

print(f"Target DB: {DATABASE_PATH}")
conn = sqlite3.connect(DATABASE_PATH)
conn.execute("PRAGMA foreign_keys=OFF")   # must be OFF during table rebuild
conn.execute("PRAGMA journal_mode=WAL")

# ── Step 0: count rows before we do anything ──────────────────────────────────
old_count = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
print(f"Rows before migration: {old_count}")

# ── Step 1: create the new table with UNIQUE(user_id, group_id) ───────────────
conn.execute("DROP TABLE IF EXISTS subscriptions_new")
conn.execute("""
CREATE TABLE subscriptions_new (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER NOT NULL REFERENCES users(id),
    group_id             INTEGER NOT NULL REFERENCES groups(id),
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK(status IN ('pending','active','comped','expired','cancelled')),
    price_locked_in      REAL NOT NULL DEFAULT 0,
    vault_address        TEXT,
    start_date           DATETIME,
    next_renewal_date    DATETIME,
    last_payment_tx_hash TEXT,
    reminder_sent_at     DATETIME,
    frozen_at            DATETIME,
    required_nano_amount INTEGER,
    amount_paid_so_far   INTEGER DEFAULT 0,
    updated_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, group_id)
)
""")
print("Created subscriptions_new with UNIQUE(user_id, group_id)")

# ── Step 2: copy all rows ─────────────────────────────────────────────────────
# Select only columns that exist in the LIVE table (avoiding missing cols).
# We use COALESCE for updated_at in case it was never added via ALTER TABLE.
# Detect which optional columns actually exist in the live table
cur = conn.execute("PRAGMA table_info(subscriptions)")
live_cols = {row[1] for row in cur.fetchall()}
print(f"Live columns: {sorted(live_cols)}")

# ── Step 2b: check for existing duplicates ────────────────────────────────────
dups = conn.execute("""
    SELECT user_id, group_id, COUNT(*) as cnt
    FROM subscriptions
    GROUP BY user_id, group_id
    HAVING cnt > 1
""").fetchall()
if dups:
    print(f"WARNING: {len(dups)} duplicate (user_id, group_id) pairs found — deduplicating first")
    for user_id, group_id, cnt in dups:
        rows = conn.execute("""
            SELECT id, status, created_at FROM subscriptions
            WHERE user_id=? AND group_id=?
            ORDER BY
                CASE status
                    WHEN 'active'   THEN 0
                    WHEN 'comped'   THEN 1
                    WHEN 'expired'  THEN 2
                    WHEN 'pending'  THEN 3
                    ELSE                 4
                END,
                id DESC
        """, (user_id, group_id)).fetchall()
        keep_id = rows[0][0]
        drop_ids = [r[0] for r in rows[1:]]
        print(f"  user={user_id} group={group_id}: keeping id={keep_id} "
              f"(status={rows[0][1]}), dropping ids={drop_ids}")
        for drop_id in drop_ids:
            conn.execute("DELETE FROM subscriptions WHERE id=?", (drop_id,))
    conn.commit()
    deduped_count = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
    print(f"After dedup: {deduped_count} rows (removed {old_count - deduped_count} duplicates)")
    old_count = deduped_count
else:
    print("No duplicates found — clean copy.")

has_updated_at       = "updated_at"       in live_cols
has_amount_paid      = "amount_paid_so_far" in live_cols

updated_at_expr  = "COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)"  if has_updated_at  else "COALESCE(created_at, CURRENT_TIMESTAMP)"
amount_paid_expr = "COALESCE(amount_paid_so_far, 0)"                       if has_amount_paid else "0"

conn.execute(f"""
INSERT INTO subscriptions_new
    (id, user_id, group_id, status, price_locked_in, vault_address,
     start_date, next_renewal_date, last_payment_tx_hash, reminder_sent_at,
     frozen_at, required_nano_amount, amount_paid_so_far,
     updated_at, created_at)
SELECT
     id, user_id, group_id, status, price_locked_in, vault_address,
     start_date, next_renewal_date, last_payment_tx_hash, reminder_sent_at,
     frozen_at, required_nano_amount,
     {amount_paid_expr},
     {updated_at_expr},
     COALESCE(created_at, CURRENT_TIMESTAMP)
FROM subscriptions
""")
print("Copied rows into subscriptions_new")

# ── Step 3: verify counts match BEFORE dropping anything ──────────────────────
new_count = conn.execute("SELECT COUNT(*) FROM subscriptions_new").fetchone()[0]
print(f"Rows in subscriptions_new: {new_count}")

if new_count != old_count:
    conn.rollback()
    conn.close()
    print(f"ABORTED — row count mismatch: old={old_count} new={new_count}")
    sys.exit(1)

print(f"Row counts match ({old_count}). Safe to proceed.")

# ── Step 4 & 5: swap tables in a single transaction ───────────────────────────
conn.execute("DROP TABLE subscriptions")
conn.execute("ALTER TABLE subscriptions_new RENAME TO subscriptions")
conn.commit()
print("Dropped old table, renamed subscriptions_new → subscriptions")

# ── Verify ────────────────────────────────────────────────────────────────────
final_count = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='subscriptions'").fetchone()[0]
conn.close()

print()
print(f"Final row count: {final_count}")
print()
print("=== Final DDL ===")
print(ddl)

"""
One-shot migration: add columns introduced in Phase 2/3 to an existing database
that was created before those columns existed.

Safe to run multiple times — uses ALTER TABLE ... IF NOT EXISTS (SQLite 3.37+)
or catches OperationalError for older SQLite.
"""
import sqlite3
import os
import sys

db_path = os.getenv("DATABASE_PATH", "./data/renewise.db")
print(f"Migrating database: {db_path}")

conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Helper: add a column only if it doesn't exist yet
def add_column_if_missing(table: str, column: str, definition: str) -> None:
    cur.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if column not in existing:
        print(f"  Adding {table}.{column} ...")
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    else:
        print(f"  {table}.{column} already exists, skipping.")

# Ensure processed_tx_hashes table exists
cur.execute("""
CREATE TABLE IF NOT EXISTS processed_tx_hashes (
    tx_hash      TEXT PRIMARY KEY,
    processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")

# subscriptions columns added in Phase 2/3
add_column_if_missing("subscriptions", "vault_address",        "TEXT")
add_column_if_missing("subscriptions", "price_locked_in",      "REAL NOT NULL DEFAULT 0")
add_column_if_missing("subscriptions", "last_payment_tx_hash", "TEXT")
add_column_if_missing("subscriptions", "reminder_sent_at",     "DATETIME")
add_column_if_missing("subscriptions", "frozen_at",            "DATETIME")
add_column_if_missing("subscriptions", "next_renewal_date",    "DATETIME")
add_column_if_missing("subscriptions", "start_date",           "DATETIME")

conn.commit()
conn.close()
print("Migration complete.")

"""
FIX 2 — vault_registry / reminder_log in init_db() verification.

Steps:
  1. Rename the real DB out of the way
  2. Run init_db() from scratch
  3. Confirm all expected tables exist (including vault_registry, reminder_log)
  4. Confirm vault_registry has the right columns
  5. Restore the real DB
"""
import asyncio, os, shutil, sqlite3, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from renewise.config import DATABASE_PATH

BACKUP = DATABASE_PATH + ".fix2_backup"

async def run():
    # ── Step 1: back up real DB ───────────────────────────────────────────────
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    if os.path.exists(DATABASE_PATH):
        shutil.copy2(DATABASE_PATH, BACKUP)
        os.remove(DATABASE_PATH)
        print(f"Moved real DB to {BACKUP}")
    else:
        print("No existing DB — starting fresh anyway.")

    try:
        # ── Step 2: run init_db() on empty path ───────────────────────────────
        from renewise.db.schema import init_db
        await init_db()
        print("init_db() completed without error.")

        # ── Step 3: check all expected tables exist ───────────────────────────
        conn = sqlite3.connect(DATABASE_PATH)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}

        REQUIRED_TABLES = {
            "groups", "users", "subscriptions", "admin_audit_log",
            "processed_tx_hashes", "admin_suspensions", "platform_config",
            "recent_admin_grants", "banned_admins", "overpayment_refunds",
            "vault_registry", "reminder_log",                      # ← the fix
        }
        REQUIRED_INDEXES = {
            "idx_vault_registry_address", "idx_sub_renewal",       # ← the fix
        }

        print(f"\nTables found:  {sorted(tables)}")
        print(f"Indexes found: {sorted(i for i in indexes if not i.startswith('sqlite_'))}")

        missing_tables  = REQUIRED_TABLES  - tables
        missing_indexes = REQUIRED_INDEXES - indexes

        if missing_tables:
            print(f"\n❌ MISSING TABLES:  {missing_tables}")
        else:
            print(f"\n✅ All {len(REQUIRED_TABLES)} required tables present.")

        if missing_indexes:
            print(f"❌ MISSING INDEXES: {missing_indexes}")
        else:
            print(f"✅ Both watcher indexes present.")

        # ── Step 4: verify vault_registry columns ─────────────────────────────
        cols = {r[1] for r in conn.execute("PRAGMA table_info(vault_registry)").fetchall()}
        expected = {"vault_address", "subscription_id", "user_id", "group_id", "created_at"}
        if expected == cols:
            print(f"✅ vault_registry columns correct: {sorted(cols)}")
        else:
            print(f"❌ vault_registry columns mismatch. Got {cols}, expected {expected}")

        # Verify groups has chat_type
        gcols = {r[1] for r in conn.execute("PRAGMA table_info(groups)").fetchall()}
        assert "chat_type" in gcols, "chat_type column missing from groups!"
        print(f"✅ groups.chat_type column present.")

        conn.close()
        assert not missing_tables and not missing_indexes, "Fix incomplete"

    finally:
        # ── Step 5: restore real DB ───────────────────────────────────────────
        os.remove(DATABASE_PATH)
        if os.path.exists(BACKUP):
            shutil.move(BACKUP, DATABASE_PATH)
            print(f"\nReal DB restored from {BACKUP}.")

asyncio.run(run())

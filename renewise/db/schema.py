import aiosqlite
from renewise.config import DATABASE_PATH
import os

CREATE_GROUPS = """
CREATE TABLE IF NOT EXISTS groups (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_chat_id      INTEGER UNIQUE NOT NULL,
    admin_telegram_id     INTEGER NOT NULL,
    chat_type             TEXT NOT NULL DEFAULT 'group'
                          CHECK(chat_type IN ('group','supergroup','channel')),
    chat_title            TEXT,
    price                 REAL NOT NULL DEFAULT 0,
    currency              TEXT NOT NULL DEFAULT 'GRAM',
    price_usd_cents       INTEGER NOT NULL DEFAULT 0,
    billing_interval_days INTEGER NOT NULL DEFAULT 30,
    payout_wallet_address TEXT,
    wallet_passcode_hash  TEXT,
    buyer_fee_bps         INTEGER,
    admin_fee_bps         INTEGER,
    invite_link           TEXT,
    status                TEXT NOT NULL DEFAULT 'active'
                          CHECK(status IN ('active','paused','frozen','suspended')),
    created_at            DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_RECENT_ADMIN_GRANTS = """
CREATE TABLE IF NOT EXISTS recent_admin_grants (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_chat_id  INTEGER NOT NULL,
    chat_title        TEXT NOT NULL,
    chat_type         TEXT NOT NULL,
    from_user_id      INTEGER NOT NULL,
    can_invite_users  INTEGER NOT NULL DEFAULT 0,
    can_manage_chat   INTEGER NOT NULL DEFAULT 0,
    can_post_messages INTEGER NOT NULL DEFAULT 0,
    granted_at        DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id  INTEGER UNIQUE NOT NULL,
    first_name        TEXT,
    username          TEXT,
    terms_accepted_at DATETIME,
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_SUBSCRIPTIONS = """
CREATE TABLE IF NOT EXISTS subscriptions (
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
"""

CREATE_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id         INTEGER REFERENCES groups(id),
    action           TEXT NOT NULL,
    actor_telegram_id INTEGER NOT NULL,
    details          TEXT,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_PROCESSED_TX = """
CREATE TABLE IF NOT EXISTS processed_tx_hashes (
    tx_hash          TEXT PRIMARY KEY,
    sub_id           INTEGER,
    processed_at     DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_ADMIN_SUSPENSIONS = """
CREATE TABLE IF NOT EXISTS admin_suspensions (
    admin_telegram_id INTEGER PRIMARY KEY,
    suspended_at      DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_PLATFORM_CONFIG = """
CREATE TABLE IF NOT EXISTS platform_config (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    payments_paused BOOLEAN NOT NULL DEFAULT 0,
    paused_at       DATETIME,
    paused_by       INTEGER
)
"""

CREATE_BANNED_ADMINS = """
CREATE TABLE IF NOT EXISTS banned_admins (
    telegram_id INTEGER PRIMARY KEY,
    banned_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    banned_by   INTEGER,
    reason      TEXT
)
"""

CREATE_VAULT_REGISTRY = """
CREATE TABLE IF NOT EXISTS vault_registry (
    vault_address   TEXT PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    user_id         INTEGER NOT NULL REFERENCES users(id),
    group_id        INTEGER NOT NULL REFERENCES groups(id),
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_REMINDER_LOG = """
CREATE TABLE IF NOT EXISTS reminder_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    renewal_cycle   TEXT NOT NULL,
    sent_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
    message_id      INTEGER,
    UNIQUE(subscription_id, renewal_cycle)
)
"""

CREATE_IDX_VAULT = """
CREATE INDEX IF NOT EXISTS idx_vault_registry_address
    ON vault_registry(vault_address)
"""

CREATE_IDX_SUB_RENEWAL = """
CREATE INDEX IF NOT EXISTS idx_sub_renewal
    ON subscriptions(next_renewal_date, status)
"""


CREATE_PENDING_WALLET_CHANGES = """
CREATE TABLE IF NOT EXISTS pending_wallet_changes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id            INTEGER NOT NULL REFERENCES groups(id),
    old_wallet_address  TEXT,
    new_wallet_address  TEXT NOT NULL,
    requested_by        INTEGER NOT NULL,
    requested_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    activates_at        DATETIME NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','applied','cancelled')),
    cancelled_at        DATETIME,
    cancelled_by        INTEGER
)
"""

CREATE_IDX_PENDING_WALLET = """
CREATE INDEX IF NOT EXISTS idx_pending_wallet_changes_group_status
    ON pending_wallet_changes(group_id, status)
"""

CREATE_OVERPAYMENT_REFUNDS = """
CREATE TABLE IF NOT EXISTS overpayment_refunds (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id  INTEGER NOT NULL REFERENCES subscriptions(id),
    user_id          INTEGER NOT NULL REFERENCES users(id),
    group_id         INTEGER NOT NULL REFERENCES groups(id),
    tx_hash          TEXT NOT NULL,
    overpaid_nano    INTEGER NOT NULL,
    refund_nano      INTEGER NOT NULL,
    refund_usd       REAL NOT NULL,
    refund_wallet    TEXT,
    status           TEXT NOT NULL DEFAULT 'pending_wallet'
                     CHECK(status IN ('pending_wallet','pending_send','sent','cancelled')),
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    resolved_at      DATETIME
)
"""

CREATE_PLATFORMS = """
CREATE TABLE IF NOT EXISTS platforms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_telegram_id INTEGER NOT NULL,
    platform_name TEXT NOT NULL,
    publishable_key_live TEXT UNIQUE,
    secret_key_live_hash TEXT,
    publishable_key_test TEXT UNIQUE NOT NULL,
    secret_key_test TEXT NOT NULL,
    wallet_address TEXT,
    wallet_passcode_hash TEXT,
    buyer_fee_bps INTEGER,
    admin_fee_bps INTEGER,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_PLATFORM_CHARGES = """
CREATE TABLE IF NOT EXISTS platform_charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_id INTEGER NOT NULL REFERENCES platforms(id),
    external_reference TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('test','live')),
    amount_usd_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','completed','expired','failed')),
    vault_address TEXT,
    payment_url TEXT,
    buyer_fee_bps INTEGER,
    platform_fee_bps INTEGER,
    tx_hash TEXT,
    required_nano_amount INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME,
    UNIQUE(platform_id, external_reference)
)
"""

CREATE_WEBHOOK_ENDPOINTS = """
CREATE TABLE IF NOT EXISTS webhook_endpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_id INTEGER NOT NULL REFERENCES platforms(id),
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_WEBHOOK_DELIVERIES = """
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_endpoint_id INTEGER NOT NULL REFERENCES webhook_endpoints(id),
    charge_id INTEGER NOT NULL REFERENCES platform_charges(id),
    status TEXT NOT NULL CHECK(status IN ('success','failed','retrying')),
    response_status_code INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_PLATFORM_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS platform_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_id INTEGER REFERENCES platforms(id),
    action TEXT NOT NULL,
    actor_telegram_id INTEGER NOT NULL,
    details TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_IDX_PLATFORM_AUDIT = """
CREATE INDEX IF NOT EXISTS idx_pal_platform ON platform_audit_log(platform_id)
"""

async def init_db() -> None:
    from renewise.config import USE_POSTGRES, DATABASE_URL
    import os

    if USE_POSTGRES:
        # ── PostgreSQL / Neon path ────────────────────────────────────────────
        from renewise.db.connection import _db

        # Translate CREATE TABLE statements for PostgreSQL:
        #   INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL PRIMARY KEY (or BIGSERIAL)
        #   DATETIME  → TIMESTAMPTZ
        #   BOOLEAN   → BOOLEAN  (already valid in PG)
        #   REAL      → DOUBLE PRECISION
        #   TEXT CHECK(... IN (...)) → same (TEXT with CHECK is valid in PG)

        pg_stmts = [
            """
            CREATE TABLE IF NOT EXISTS groups (
                id                    SERIAL PRIMARY KEY,
                telegram_chat_id      BIGINT UNIQUE NOT NULL,
                admin_telegram_id     BIGINT NOT NULL,
                chat_type             TEXT NOT NULL DEFAULT 'group'
                                      CHECK(chat_type IN ('group','supergroup','channel')),
                chat_title            TEXT,
                price                 DOUBLE PRECISION NOT NULL DEFAULT 0,
                currency              TEXT NOT NULL DEFAULT 'GRAM',
                price_usd_cents       INTEGER NOT NULL DEFAULT 0,
                billing_interval_days INTEGER NOT NULL DEFAULT 30,
                payout_wallet_address TEXT,
                wallet_passcode_hash  TEXT,
                buyer_fee_bps         INTEGER,
                admin_fee_bps         INTEGER,
                invite_link           TEXT,
                status                TEXT NOT NULL DEFAULT 'active'
                                      CHECK(status IN ('active','paused','frozen','suspended')),
                created_at            TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS recent_admin_grants (
                id                SERIAL PRIMARY KEY,
                telegram_chat_id  BIGINT NOT NULL,
                chat_title        TEXT NOT NULL,
                chat_type         TEXT NOT NULL,
                from_user_id      BIGINT NOT NULL,
                can_invite_users  INTEGER NOT NULL DEFAULT 0,
                can_manage_chat   INTEGER NOT NULL DEFAULT 0,
                can_post_messages INTEGER NOT NULL DEFAULT 0,
                granted_at        TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS users (
                id                SERIAL PRIMARY KEY,
                telegram_user_id  BIGINT UNIQUE NOT NULL,
                first_name        TEXT,
                username          TEXT,
                terms_accepted_at TIMESTAMPTZ,
                created_at        TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id                   SERIAL PRIMARY KEY,
                user_id              INTEGER NOT NULL REFERENCES users(id),
                group_id             INTEGER NOT NULL REFERENCES groups(id),
                status               TEXT NOT NULL DEFAULT 'pending'
                                     CHECK(status IN ('pending','active','comped','expired','cancelled')),
                price_locked_in      DOUBLE PRECISION NOT NULL DEFAULT 0,
                vault_address        TEXT,
                start_date           TIMESTAMPTZ,
                next_renewal_date    TIMESTAMPTZ,
                last_payment_tx_hash TEXT,
                reminder_sent_at     TIMESTAMPTZ,
                frozen_at            TIMESTAMPTZ,
                required_nano_amount BIGINT,
                amount_paid_so_far   BIGINT DEFAULT 0,
                updated_at           TIMESTAMPTZ DEFAULT NOW(),
                created_at           TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_id, group_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id                INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
                group_id          INTEGER REFERENCES groups(id),
                action            TEXT NOT NULL,
                actor_telegram_id BIGINT NOT NULL,
                details           TEXT,
                created_at        TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS processed_tx_hashes (
                tx_hash      TEXT PRIMARY KEY,
                sub_id       INTEGER,
                processed_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS admin_suspensions (
                admin_telegram_id BIGINT PRIMARY KEY,
                suspended_at      TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_config (
                id                   INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
                payments_paused      BOOLEAN NOT NULL DEFAULT FALSE,
                paused_at            TIMESTAMPTZ,
                paused_by            BIGINT,
                global_buyer_fee_bps INTEGER,
                global_admin_fee_bps INTEGER
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS banned_admins (
                telegram_id BIGINT PRIMARY KEY,
                banned_at   TIMESTAMPTZ DEFAULT NOW(),
                banned_by   BIGINT,
                reason      TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS vault_registry (
                vault_address   TEXT PRIMARY KEY,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
                user_id         INTEGER NOT NULL REFERENCES users(id),
                group_id        INTEGER NOT NULL REFERENCES groups(id),
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS reminder_log (
                id              INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
                renewal_cycle   TEXT NOT NULL,
                sent_at         TIMESTAMPTZ DEFAULT NOW(),
                message_id      BIGINT,
                UNIQUE(subscription_id, renewal_cycle)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pending_wallet_changes (
                id                  INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
                group_id            INTEGER NOT NULL REFERENCES groups(id),
                old_wallet_address  TEXT,
                new_wallet_address  TEXT NOT NULL,
                requested_by        BIGINT NOT NULL,
                requested_at        TIMESTAMPTZ DEFAULT NOW(),
                activates_at        TIMESTAMPTZ NOT NULL,
                status              TEXT NOT NULL DEFAULT 'pending'
                                    CHECK(status IN ('pending','applied','cancelled')),
                cancelled_at        TIMESTAMPTZ,
                cancelled_by        BIGINT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS overpayment_refunds (
                id               INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
                subscription_id  INTEGER NOT NULL REFERENCES subscriptions(id),
                user_id          INTEGER NOT NULL REFERENCES users(id),
                group_id         INTEGER NOT NULL REFERENCES groups(id),
                tx_hash          TEXT NOT NULL,
                overpaid_nano    BIGINT NOT NULL,
                refund_nano      BIGINT NOT NULL,
                refund_usd       DOUBLE PRECISION NOT NULL,
                refund_wallet    TEXT,
                status           TEXT NOT NULL DEFAULT 'pending_wallet'
                                 CHECK(status IN ('pending_wallet','pending_send','sent','cancelled')),
                created_at       TIMESTAMPTZ DEFAULT NOW(),
                resolved_at      TIMESTAMPTZ
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platforms (
                id                   SERIAL PRIMARY KEY,
                owner_telegram_id    BIGINT NOT NULL,
                platform_name        TEXT NOT NULL,
                publishable_key_live TEXT UNIQUE,
                secret_key_live_hash TEXT,
                publishable_key_test TEXT UNIQUE NOT NULL,
                secret_key_test      TEXT NOT NULL,
                wallet_address       TEXT,
                wallet_passcode_hash TEXT,
                buyer_fee_bps        INTEGER,
                admin_fee_bps        INTEGER,
                status               TEXT NOT NULL DEFAULT 'active'
                                     CHECK(status IN ('active','revoked')),
                created_at           TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_charges (
                id                   SERIAL PRIMARY KEY,
                platform_id          INTEGER NOT NULL REFERENCES platforms(id),
                external_reference   TEXT NOT NULL,
                mode                 TEXT NOT NULL CHECK(mode IN ('test','live')),
                amount_usd_cents     INTEGER NOT NULL,
                status               TEXT NOT NULL DEFAULT 'pending'
                                     CHECK(status IN ('pending','completed','expired','failed')),
                vault_address        TEXT,
                payment_url          TEXT,
                buyer_fee_bps        INTEGER,
                platform_fee_bps     INTEGER,
                tx_hash              TEXT,
                required_nano_amount BIGINT,
                created_at           TIMESTAMPTZ DEFAULT NOW(),
                completed_at         TIMESTAMPTZ,
                UNIQUE(platform_id, external_reference)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS webhook_endpoints (
                id                   SERIAL PRIMARY KEY,
                platform_id          INTEGER NOT NULL REFERENCES platforms(id),
                url                  TEXT NOT NULL,
                secret               TEXT NOT NULL,
                active               BOOLEAN NOT NULL DEFAULT TRUE,
                created_at           TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                id                   SERIAL PRIMARY KEY,
                webhook_endpoint_id  INTEGER NOT NULL REFERENCES webhook_endpoints(id),
                charge_id            INTEGER NOT NULL REFERENCES platform_charges(id),
                status               TEXT NOT NULL CHECK(status IN ('success','failed','retrying')),
                response_status_code INTEGER,
                created_at           TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_audit_log (
                id                INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
                platform_id       INTEGER REFERENCES platforms(id),
                action            TEXT NOT NULL,
                actor_telegram_id BIGINT NOT NULL,
                details           TEXT,
                created_at        TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_vault_registry_address ON vault_registry(vault_address)",
            "CREATE INDEX IF NOT EXISTS idx_sub_renewal ON subscriptions(next_renewal_date, status)",
            "CREATE INDEX IF NOT EXISTS idx_pending_wallet_changes_group_status ON pending_wallet_changes(group_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_pal_platform ON platform_audit_log(platform_id)",
        ]

        async with _db() as db:
            for stmt in pg_stmts:
                await db.execute(stmt)
            # Seed platform_config singleton
            await db.execute(
                "INSERT INTO platform_config (id, payments_paused) "
                "OVERRIDING SYSTEM VALUE VALUES (1, FALSE) ON CONFLICT DO NOTHING"
            )
            # Seed fee defaults
            buyer_bps_default = int(os.getenv("BUYER_FEE_BPS", "200"))
            admin_bps_default = int(os.getenv("ADMIN_FEE_BPS", "330"))
            await db.execute(
                "UPDATE platform_config "
                "SET global_buyer_fee_bps = COALESCE(global_buyer_fee_bps, $1), "
                "    global_admin_fee_bps = COALESCE(global_admin_fee_bps, $2) "
                "WHERE id = 1",
                buyer_bps_default, admin_bps_default,
            )

            # ── Postgres migrations (idempotent) ──────────────────────────────
            # Each statement is wrapped in a DO-EXCEPTION block so re-running on
            # a database that has already been migrated is completely safe.
            pg_migrations = [
                # Make live key columns nullable (opt-in live key generation)
                """
                DO $$ BEGIN
                    ALTER TABLE platforms ALTER COLUMN publishable_key_live DROP NOT NULL;
                EXCEPTION WHEN others THEN NULL; END; $$;
                """,
                """
                DO $$ BEGIN
                    ALTER TABLE platforms ALTER COLUMN secret_key_live_hash DROP NOT NULL;
                EXCEPTION WHEN others THEN NULL; END; $$;
                """,
                # Rename secret_key_test_hash → secret_key_test (stores raw plaintext)
                """
                DO $$ BEGIN
                    ALTER TABLE platforms RENAME COLUMN secret_key_test_hash TO secret_key_test;
                EXCEPTION WHEN others THEN NULL; END; $$;
                """,
                # ── Column additions (skipped if column already exists) ────────
                # platform_charges.payment_url — stores full ton:// deep-link with StateInit
                "DO $$ BEGIN ALTER TABLE platform_charges ADD COLUMN payment_url TEXT; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                # groups — fee overrides, passkey, chat_type, price_usd_cents
                "DO $$ BEGIN ALTER TABLE groups ADD COLUMN buyer_fee_bps INTEGER; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                "DO $$ BEGIN ALTER TABLE groups ADD COLUMN admin_fee_bps INTEGER; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                "DO $$ BEGIN ALTER TABLE groups ADD COLUMN price_usd_cents INTEGER NOT NULL DEFAULT 0; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                "DO $$ BEGIN ALTER TABLE groups ADD COLUMN chat_title TEXT; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                "DO $$ BEGIN ALTER TABLE groups ADD COLUMN invite_link TEXT; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                "DO $$ BEGIN ALTER TABLE groups ADD COLUMN chat_type TEXT NOT NULL DEFAULT 'group'; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                "DO $$ BEGIN ALTER TABLE groups ADD COLUMN wallet_passcode_hash TEXT; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                # platforms — wallet passkey, fee overrides
                "DO $$ BEGIN ALTER TABLE platforms ADD COLUMN wallet_address TEXT; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                "DO $$ BEGIN ALTER TABLE platforms ADD COLUMN wallet_passcode_hash TEXT; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                "DO $$ BEGIN ALTER TABLE platforms ADD COLUMN buyer_fee_bps INTEGER; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                "DO $$ BEGIN ALTER TABLE platforms ADD COLUMN admin_fee_bps INTEGER; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                # platform_config — global fee defaults
                "DO $$ BEGIN ALTER TABLE platform_config ADD COLUMN global_buyer_fee_bps INTEGER; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                "DO $$ BEGIN ALTER TABLE platform_config ADD COLUMN global_admin_fee_bps INTEGER; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                # subscriptions — payment tracking columns
                "DO $$ BEGIN ALTER TABLE subscriptions ADD COLUMN required_nano_amount BIGINT; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                "DO $$ BEGIN ALTER TABLE subscriptions ADD COLUMN amount_paid_so_far BIGINT DEFAULT 0; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                "DO $$ BEGIN ALTER TABLE subscriptions ADD COLUMN updated_at TIMESTAMPTZ DEFAULT NOW(); EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                # users — ToS acceptance
                "DO $$ BEGIN ALTER TABLE users ADD COLUMN terms_accepted_at TIMESTAMPTZ; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
                # processed_tx_hashes — subscription link
                "DO $$ BEGIN ALTER TABLE processed_tx_hashes ADD COLUMN sub_id INTEGER; EXCEPTION WHEN duplicate_column THEN NULL; END; $$;",
            ]
            for mig in pg_migrations:
                await db.execute(mig)

        return

    # ── SQLite path (unchanged) ───────────────────────────────────────────────
    import aiosqlite
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        for stmt in (
            CREATE_GROUPS, CREATE_USERS, CREATE_SUBSCRIPTIONS, CREATE_AUDIT_LOG,
            CREATE_PROCESSED_TX, CREATE_ADMIN_SUSPENSIONS, CREATE_PLATFORM_CONFIG,
            CREATE_RECENT_ADMIN_GRANTS, CREATE_BANNED_ADMINS, CREATE_OVERPAYMENT_REFUNDS,
            # Wallet change protection table (delay-and-notify)
            CREATE_PENDING_WALLET_CHANGES,
            CREATE_PLATFORMS, CREATE_PLATFORM_CHARGES, CREATE_WEBHOOK_ENDPOINTS, CREATE_WEBHOOK_DELIVERIES, CREATE_PLATFORM_AUDIT_LOG,
            # Watcher tables — must come after CREATE_SUBSCRIPTIONS (FK dependency)
            CREATE_VAULT_REGISTRY, CREATE_REMINDER_LOG,
            CREATE_IDX_VAULT, CREATE_IDX_SUB_RENEWAL, CREATE_IDX_PENDING_WALLET, CREATE_IDX_PLATFORM_AUDIT,
        ):
            await db.execute(stmt)
        await db.execute(
            "INSERT INTO platform_config (id, payments_paused) "
            "SELECT 1, 0 WHERE NOT EXISTS (SELECT 1 FROM platform_config WHERE id=1)"
        )

        # Migrations — each wrapped individually so one failure doesn't block others
        for migration in (
            "ALTER TABLE groups ADD COLUMN buyer_fee_bps INTEGER",
            "ALTER TABLE groups ADD COLUMN admin_fee_bps INTEGER",
            "ALTER TABLE groups ADD COLUMN price_usd_cents INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE groups ADD COLUMN chat_title TEXT",
            "ALTER TABLE groups ADD COLUMN invite_link TEXT",
            # ── Global fee defaults on the platform_config singleton row ───────
            # Added here so they exist before the seed UPDATE below.
            "ALTER TABLE platform_config ADD COLUMN global_buyer_fee_bps INTEGER",
            "ALTER TABLE platform_config ADD COLUMN global_admin_fee_bps INTEGER",
            "ALTER TABLE subscriptions ADD COLUMN required_nano_amount INTEGER",
            "ALTER TABLE subscriptions ADD COLUMN amount_paid_so_far INTEGER DEFAULT 0",
            "ALTER TABLE subscriptions ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP",
            # ── Terms of Service acceptance ────────────────────────────────────
            "ALTER TABLE users ADD COLUMN terms_accepted_at DATETIME",
            # ── chat_type on groups (channel vs group/supergroup) ───────────────
            "ALTER TABLE groups ADD COLUMN chat_type TEXT NOT NULL DEFAULT 'group'",
            # ── sub_id on processed_tx_hashes (Fix 1 — schema/watcher drift) ──
            "ALTER TABLE processed_tx_hashes ADD COLUMN sub_id INTEGER",
            # ── Platform columns (may be missing on pre-platform DBs) ──────────
            "ALTER TABLE platforms ADD COLUMN wallet_address TEXT",
            "ALTER TABLE platforms ADD COLUMN wallet_passcode_hash TEXT",
            "ALTER TABLE platforms ADD COLUMN publishable_key_live TEXT",
            "ALTER TABLE platforms ADD COLUMN secret_key_live_hash TEXT",
            # ── Group wallet passkey (protect payout wallet changes) ───────────
            "ALTER TABLE groups ADD COLUMN wallet_passcode_hash TEXT",
            # ── Per-platform fee overrides ─────────────────────────────────────
            "ALTER TABLE platforms ADD COLUMN buyer_fee_bps INTEGER",
            "ALTER TABLE platforms ADD COLUMN admin_fee_bps INTEGER",
        ):
            try:
                await db.execute(migration)
            except aiosqlite.OperationalError:
                pass  # column/table already exists — safe to ignore

        # ── Platform tables (idempotent CREATE TABLE IF NOT EXISTS) ───────────
        # Run separately because these are full DDL statements, not ALTER TABLE.
        # Safe to re-run — IF NOT EXISTS makes them no-ops on existing DBs.
        for stmt in (
            CREATE_PLATFORMS,
            CREATE_PLATFORM_CHARGES,
            CREATE_WEBHOOK_ENDPOINTS,
            CREATE_WEBHOOK_DELIVERIES,
            CREATE_PLATFORM_AUDIT_LOG,
        ):
            try:
                await db.execute(stmt)
            except aiosqlite.OperationalError:
                pass

        # Seed env defaults into id=1 row only when the operator hasn't yet
        # set an explicit override (i.e. columns are still NULL after migration).
        buyer_bps_default = int(os.getenv("BUYER_FEE_BPS", "200"))
        admin_bps_default = int(os.getenv("ADMIN_FEE_BPS", "330"))
        await db.execute(
            "UPDATE platform_config "
            "SET global_buyer_fee_bps = COALESCE(global_buyer_fee_bps, ?), "
            "    global_admin_fee_bps = COALESCE(global_admin_fee_bps, ?) "
            "WHERE id = 1",
            (buyer_bps_default, admin_bps_default),
        )

        # ── Data-quality fix: zero out stale groups.price values ──────────────
        # groups.price was written at paywall-creation time as a GRAM amount
        # (or a raw USD float in some flows). It was never updated as the
        # exchange rate moved, so any code reading it got a wrong GRAM value.
        # Now that price_usd_cents is the authoritative source we reset price=0
        # on every row that has a valid USD price so it can never be misread.
        # join_request.py and all display code now derive GRAM live from
        # price_usd_cents + CoinGecko, so price=0 is never shown to users.
        await db.execute(
            "UPDATE groups SET price = 0 WHERE price_usd_cents > 0 AND price != 0"
        )

        await db.commit()


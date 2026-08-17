"""
Seed a test subscription in the database so the watcher knows which vault
address to watch. This mirrors the params used in scripts/deploy_testnet.py.

Run ONCE before triggering a payment:
    python scripts/seed_test_subscription.py
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from dotenv import load_dotenv
load_dotenv()

from pytoniq_core import Address
from renewise.config import LOG_ADDRESS
from renewise.ton.vault import VaultParams, _load_code_cell, compute_vault_address
from renewise.db import queries
from renewise.db.schema import init_db
from renewise.watcher import db as watcher_db


async def main():
    admin_wallet_str    = os.getenv("ADMIN_ADDRESS")
    platform_wallet_str = os.getenv("PLATFORM_ADDRESS")

    if not admin_wallet_str or not platform_wallet_str:
        print("ERROR: ADMIN_ADDRESS and PLATFORM_ADDRESS must be set in .env")
        return

    admin_addr    = Address(admin_wallet_str)
    platform_addr = Address(platform_wallet_str)
    log_addr      = Address(LOG_ADDRESS) if LOG_ADDRESS else platform_addr

    # These must match deploy_testnet.py exactly
    price_nano       = int(0.14902 * 1e9)
    buyer_fee_bps    = 200
    admin_fee_bps    = 330
    subscription_id  = 9999

    params = VaultParams(
        admin_wallet=admin_addr,
        platform_wallet=platform_addr,
        log_address=log_addr,
        price=price_nano,
        buyer_fee_bps=buyer_fee_bps,
        admin_fee_bps=admin_fee_bps,
        subscription_id=subscription_id,
    )

    code_cell     = _load_code_cell()
    vault_address = compute_vault_address(params, code_cell)
    vault_addr_str = vault_address.to_str(is_url_safe=True, is_bounceable=True)

    print(f"Vault address: {vault_addr_str}")

    # Step 1: Ensure core schema is applied (creates subscriptions, users, groups tables)
    await init_db()

    # Step 2: Ensure watcher tables exist (vault_registry, reminder_log, etc.)
    await watcher_db.migrate()

    # Step 3: Seed group, user, subscription
    TEST_CHAT_ID  = -100999999999
    TEST_ADMIN_ID = 9999999
    TEST_USER_ID  = 9999998

    group_id = await queries.upsert_group(TEST_CHAT_ID, TEST_ADMIN_ID)
    user_id  = await queries.upsert_user(TEST_USER_ID)

    sub_id = await queries.create_subscription(
        user_id=user_id,
        group_id=group_id,
        price_locked_in=price_nano,
        vault_address=vault_addr_str,
    )

    print(f"Seeded subscription: group_id={group_id}, user_id={user_id}, sub_id={sub_id}")

    # Step 4: Register in vault_registry so tasks.process_payment can look it up
    await watcher_db.register_vault(
        vault_address=vault_addr_str,
        subscription_id=sub_id,
        user_id=user_id,
        group_id=group_id,
    )
    print(f"Registered vault in vault_registry: {vault_addr_str}")
    print()
    print("Ready! The watcher will detect transactions on this vault.")
    print("Trigger a payment with:  python scripts/deploy_testnet.py")


if __name__ == "__main__":
    asyncio.run(main())

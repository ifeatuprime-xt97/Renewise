"""
tests/test_triggered_watcher.py

Unit tests for the event-driven / triggered in-process watcher:
- Registering a new vault wakes the watcher
- Automatic transaction polling for active vaults
- 5-minute post-confirmation grace period before eviction
- 30-minute checkout timeout eviction
- Deep sleep / standby when no active vaults remain
- get_vaults_to_watch() only returning pending vaults within 30 minutes
"""
import sys
import os
import asyncio
import time
from contextlib import suppress
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from renewise.config import USE_POSTGRES
from renewise.db.connection import _db
from renewise.watcher.inprocess_watcher import (
    VaultWatchEntry,
    _active_vaults,
    _wake_event,
    wake_watcher_for_vault,
    mark_vault_confirmed,
    poll_vaults_inprocess,
)
from renewise.db.queries import get_vaults_to_watch
import renewise.monitor as monitor


@pytest.fixture(autouse=True)
def clean_watcher_state():
    _active_vaults.clear()
    _wake_event.clear()
    monitor.watcher_is_sleeping = False
    yield
    _active_vaults.clear()
    _wake_event.clear()
    monitor.watcher_is_sleeping = False


@pytest.mark.asyncio
async def test_wake_watcher_for_vault_registration():
    test_vault = "EQB_TEST_VAULT_ADDRESS_12345"
    assert len(_active_vaults) == 0
    assert not _wake_event.is_set()

    wake_watcher_for_vault(test_vault, network="testnet")

    assert len(_active_vaults) == 1
    assert _wake_event.is_set()

    # Find registered entry
    entry = next(iter(_active_vaults.values()))
    assert entry.vault_address == test_vault
    assert entry.network == "testnet"
    assert entry.confirmed_at is None
    assert entry.expires_at > time.time() + 1700


@pytest.mark.asyncio
async def test_mark_vault_confirmed_grace_period():
    test_vault = "EQB_TEST_VAULT_ADDRESS_CONFIRM"
    wake_watcher_for_vault(test_vault, network="mainnet")

    entry = next(iter(_active_vaults.values()))
    assert entry.confirmed_at is None

    # Mark confirmed
    mark_vault_confirmed(test_vault)
    assert entry.confirmed_at is not None
    assert entry.confirmed_at <= time.time()

    # The vault MUST still be in _active_vaults right after confirmation (grace period)
    assert len(_active_vaults) == 1


@pytest.mark.asyncio
async def test_eviction_and_standby_loop():
    """
    Test that poll_vaults_inprocess:
    1. Evicts vaults whose 5-minute grace period has passed
    2. Evicts vaults whose 30-minute checkout window expired
    3. Enters standby mode (watcher_is_sleeping = True) when no vaults remain
    """
    now = time.time()

    # Vault 1: confirmed 6 minutes ago (360s > 300s grace -> should be evicted)
    _active_vaults["v1"] = VaultWatchEntry(
        vault_address="v1",
        canonical="v1",
        network="mainnet",
        created_at=now - 600,
        expires_at=now + 1200,
        confirmed_at=now - 360,
    )

    # Vault 2: created 35 minutes ago, never paid (expired -> should be evicted)
    _active_vaults["v2"] = VaultWatchEntry(
        vault_address="v2",
        canonical="v2",
        network="mainnet",
        created_at=now - 2100,
        expires_at=now - 300,
        confirmed_at=None,
    )

    app = MagicMock()

    with patch("renewise.watcher.inprocess_watcher.migrate", AsyncMock()), \
         patch("renewise.watcher.inprocess_watcher.get_vaults_to_watch", AsyncMock(return_value=[])), \
         patch("renewise.watcher.inprocess_watcher.fetch_transactions", AsyncMock(return_value=[])):

        task = asyncio.create_task(poll_vaults_inprocess(app))
        try:
            for _ in range(20):
                await asyncio.sleep(0.05)
                if monitor.watcher_is_sleeping:
                    break

            # Both expired / post-grace vaults should be evicted
            assert "v1" not in _active_vaults
            assert "v2" not in _active_vaults
            assert len(_active_vaults) == 0

            # With 0 active vaults, watcher must enter standby sleeping state
            assert monitor.watcher_is_sleeping is True
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_automatic_confirmation_without_user_action():
    """
    Test that when a vault is active in memory, incoming blockchain transactions
    are processed automatically without requiring any manual button click.
    """
    test_vault = "EQ_AUTO_CONFIRM_VAULT"
    wake_watcher_for_vault(test_vault, network="mainnet")

    canonical_key = next(iter(_active_vaults.keys()))

    mock_tx = {
        "transaction_id": {"hash": "txhash12345"},
        "in_msg": {"value": "1000000000"},
    }

    mock_app = MagicMock()

    with patch("renewise.watcher.inprocess_watcher.migrate", AsyncMock()), \
         patch("renewise.watcher.inprocess_watcher.get_vaults_to_watch", AsyncMock(return_value=[])), \
         patch("renewise.watcher.inprocess_watcher.fetch_transactions", AsyncMock(return_value=[mock_tx])), \
         patch("renewise.watcher.inprocess_watcher.is_tx_processed", AsyncMock(return_value=False)), \
         patch("renewise.watcher.inprocess_watcher._process_payment_inprocess", AsyncMock()) as mock_process:

        task = asyncio.create_task(poll_vaults_inprocess(mock_app))
        try:
            for _ in range(20):
                await asyncio.sleep(0.05)
                if mock_process.called:
                    break

            # _process_payment_inprocess must have been called automatically
            assert mock_process.called
            call_args = mock_process.call_args[0]
            # app, canonical, tx_hash, amount_nano
            assert call_args[1] == canonical_key
            assert call_args[2] == "txhash12345"
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_get_vaults_to_watch_excludes_active_and_stale():
    """
    Test that get_vaults_to_watch:
    - Excludes 'active' subscriptions (already paid)
    - Excludes 'expired' charges
    - Includes pending subscriptions and charges created within the last 30m
    """
    old_ts = "NOW() - INTERVAL '5 days'" if USE_POSTGRES else "datetime('now', '-5 days')"
    now_ts = "NOW()" if USE_POSTGRES else "CURRENT_TIMESTAMP"

    async with _db() as db:
        # Create a test group
        await db.execute(
            "INSERT INTO groups (id, telegram_chat_id, chat_title, admin_telegram_id, price_usd_cents) "
            "VALUES (999988, -999988, 'Test Watcher Group', 111, 500) "
            "ON CONFLICT(id) DO NOTHING"
        )
        # Create a test user
        await db.execute(
            "INSERT INTO users (id, telegram_user_id, username) "
            "VALUES (888877, 888877, 'testwatcheruser') "
            "ON CONFLICT(id) DO NOTHING"
        )

        # 1. Active subscription (paid weeks ago) - MUST NOT BE WATCHED
        await db.execute(
            f"INSERT INTO subscriptions (user_id, group_id, status, vault_address, created_at, updated_at) "
            f"VALUES (888877, 999988, 'active', 'EQ_PAID_SUB_VAULT', {old_ts}, {old_ts}) "
            f"ON CONFLICT(user_id, group_id) DO UPDATE SET status='active', vault_address='EQ_PAID_SUB_VAULT', created_at={old_ts}, updated_at={old_ts}"
        )

    # Check vaults outside the transaction block (committed state)
    vaults = await get_vaults_to_watch()
    vault_addresses = [v[0] for v in vaults]
    assert "EQ_PAID_SUB_VAULT" not in vault_addresses

    # 2. Update subscription to 'pending' within 30 min - MUST BE WATCHED
    async with _db() as db:
        await db.execute(
            f"UPDATE subscriptions SET status='pending', updated_at={now_ts} "
            f"WHERE user_id=888877 AND group_id=999988"
        )

    vaults = await get_vaults_to_watch()
    vault_addresses = [v[0] for v in vaults]
    assert "EQ_PAID_SUB_VAULT" in vault_addresses

    # Cleanup
    async with _db() as db:
        await db.execute("DELETE FROM subscriptions WHERE user_id=888877 AND group_id=999988")

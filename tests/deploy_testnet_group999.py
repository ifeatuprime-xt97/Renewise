import os
import sys
import asyncio
import sqlite3
from dotenv import load_dotenv

# Ensure the root directory is in the python path so it can find 'renewise'
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Load env before importing other modules
load_dotenv()

from pytoniq_core import Address
from pytoniq import LiteBalancer, WalletV4R2

from renewise.config import PLATFORM_WALLET, LOG_ADDRESS, DATABASE_PATH, BUYER_FEE_BPS as GLOBAL_BUYER_FEE_BPS, ADMIN_FEE_BPS as GLOBAL_ADMIN_FEE_BPS
from renewise.ton.vault import (
    VaultParams, 
    _load_code_cell, 
    compute_vault_address, 
    _build_state_init, 
    _build_pay_body,
    required_payment_nano
)

async def main():
    print("=== Renewise Testnet Deployment & Payment Test (Group 999) ===")
    mnemonic_str = os.getenv("DEPLOYER_MNEMONIC")
    if not mnemonic_str:
        print("ERROR: DEPLOYER_MNEMONIC not found in .env")
        return

    mnemonic = mnemonic_str.split()
    admin_wallet_str = os.getenv("ADMIN_ADDRESS")
    platform_wallet_str = os.getenv("PLATFORM_ADDRESS")

    if not admin_wallet_str or not platform_wallet_str:
        print("ERROR: ADMIN_ADDRESS and PLATFORM_ADDRESS must be set in .env")
        return

    # Fetch Group 999 Config from DB
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, price, buyer_fee_bps, admin_fee_bps FROM groups WHERE id=999").fetchone()
    conn.close()

    if not row:
        print("ERROR: Group 999 not found in DB")
        return

    group_data = dict(row)
    print(f"Group Data: {group_data}")

    # Use group settings or global defaults
    price_nano = int(group_data["price"] * 1e9)
    buyer_fee_bps = group_data["buyer_fee_bps"] if group_data["buyer_fee_bps"] is not None else GLOBAL_BUYER_FEE_BPS
    admin_fee_bps = group_data["admin_fee_bps"] if group_data["admin_fee_bps"] is not None else GLOBAL_ADMIN_FEE_BPS
    subscription_id = group_data["id"]

    print(f"Using Price: {price_nano / 1e9} TON")
    print(f"Using Buyer Fee BPS: {buyer_fee_bps}")
    print(f"Using Admin Fee BPS: {admin_fee_bps}")

    # Initialize LiteBalancer for testnet
    print("Connecting to TON testnet...")
    provider = LiteBalancer.from_testnet_config(trust_level=2)
    await provider.start_up()

    try:
        # Create wallet from mnemonic
        wallet = await WalletV4R2.from_mnemonic(provider, mnemonic)
        print(f"Deployer Wallet Address: {wallet.address.to_str(is_url_safe=True)}")
        
        account_state = await provider.get_account_state(wallet.address)
        balance = account_state.balance
        print(f"Deployer Balance: {balance / 1e9} TON")

        if balance < int(0.5 * 1e9):
            print("WARNING: Low balance on deployer wallet, payment might fail.")

        admin_addr = Address(admin_wallet_str)
        platform_addr = Address(platform_wallet_str)
        log_addr = Address(LOG_ADDRESS) if LOG_ADDRESS else platform_addr

        params = VaultParams(
            admin_wallet=admin_addr,
            platform_wallet=platform_addr,
            log_address=log_addr,
            price=price_nano,
            buyer_fee_bps=buyer_fee_bps,
            admin_fee_bps=admin_fee_bps,
            subscription_id=subscription_id
        )

        code_cell = _load_code_cell()
        vault_address = compute_vault_address(params, code_cell)

        from pytoniq_core import begin_cell
        from renewise.ton.vault import _build_data_cell
        
        data_cell = _build_data_cell(params)
        state_init_cell = (
            begin_cell()
            .store_bit(0)
            .store_bit(0)
            .store_bit(1)
            .store_ref(code_cell)
            .store_bit(1)
            .store_ref(data_cell)
            .store_bit(0)
            .end_cell()
        )
        
        print("\n--- Python Hashes ---")
        print(f"Code Hash     : {code_cell.hash.hex()}")
        print(f"Data Hash     : {data_cell.hash.hex()}")
        print(f"StateInit Hash: {state_init_cell.hash.hex()}")
        print("---------------------\n")
        
        print(f"\nVault Address: {vault_address.to_str(is_url_safe=True, is_bounceable=True)}")
        print("Fetching initial balances...")
        
        admin_balance_before = (await provider.get_account_state(admin_addr)).balance
        platform_balance_before = (await provider.get_account_state(platform_addr)).balance
        
        vault_balance_before = (await provider.get_account_state(vault_address)).balance
        print(f"Admin Balance Before:    {admin_balance_before / 1e9:.9f} TON")
        print(f"Platform Balance Before: {platform_balance_before / 1e9:.9f} TON")
        print(f"Vault Balance Before:    {vault_balance_before / 1e9:.9f} TON")

        # Prepare the deploy & pay message
        required_nano = required_payment_nano(params)
        print(f"\nSending {required_nano / 1e9} TON to Vault...")
        
        state_init = _build_state_init(params, code_cell)
        pay_body = _build_pay_body()

        # Check if the deployer wallet contract is itself deployed yet.
        try:
            seqno = await wallet.get_seqno()
        except Exception:
            seqno = -1  # uninitialized

        if seqno == -1:
            print("Deployer wallet is uninitialized — deploying it via send_init_external()...")
            await wallet.send_init_external()
            print("Wallet deploy tx sent. Waiting 15 seconds...")
            await asyncio.sleep(15)

        # Capture Vault's last transaction hash before we send to detect when our tx lands
        _, vault_shard_before = await provider.raw_get_account_state(vault_address)
        last_tx_hash_before = vault_shard_before.last_trans_hash if vault_shard_before else b""

        # Send transaction (add 0.02 TON to cover forward fees so the vault receives >= required)
        send_amount = required_nano + int(0.02 * 1e9)
        print(f"Sending {send_amount / 1e9} TON from deployer to vault (includes 0.02 TON extra for forward fees)...")
        await wallet.transfer(
            destination=vault_address,
            amount=send_amount,
            body=pay_body,
            state_init=state_init
        )

        print("Transaction sent! Polling vault for confirmation (max 60 seconds)...")
        
        # Poll vault for new transaction
        confirmed = False
        vault_txs = []
        for i in range(12):  # 12 * 5 = 60 seconds
            await asyncio.sleep(5)
            _, vault_shard_current = await provider.raw_get_account_state(vault_address)
            
            if vault_shard_current and vault_shard_current.last_trans_hash and vault_shard_current.last_trans_hash != last_tx_hash_before:
                print("\n[✔] New transaction detected on Vault!")
                # Get the actual transaction details
                vault_txs = await provider.get_transactions(vault_address, count=1)
                confirmed = True
                break
            
            print(f"Waiting... ({i*5+5}s)")

        if not confirmed:
            print("\n[!] Timeout: No new transaction detected on Vault after 60 seconds.")
        elif vault_txs:
            tx = vault_txs[0]
            tx_hash_hex = tx.cell.hash.hex()
            print(f"\n--- Transaction Details ---")
            print(f"Transaction Hash (Vault): {tx_hash_hex}")
            print(f"Tonscan Link: https://testnet.tonscan.org/tx/{tx_hash_hex}")
            
            if tx.description.compute_ph.type_ == 'vm':
                exit_code = tx.description.compute_ph.exit_code
                print(f"Compute Phase Exit Code: {exit_code}")
                if exit_code == 0:
                    print("Status: SUCCESS (Vault executed logic without throwing)")
                else:
                    print(f"Status: FAILED (Contract threw error {exit_code})")
            else:
                print("Compute Phase: Skipped / Not VM (likely a bounce or initialization without compute)")

            if tx.description.action is not None:
                print(f"Action Phase Result Code: {tx.description.action.result_code}")
                print(f"Out Messages Count: {tx.description.action.tot_msg_size.cells if hasattr(tx.description.action.tot_msg_size, 'cells') else 'unknown'}")

        print("\n---------------------------")

        # Check balances after
        deployer_balance_after = (await provider.get_account_state(wallet.address)).balance
        admin_balance_after = (await provider.get_account_state(admin_addr)).balance
        platform_balance_after = (await provider.get_account_state(platform_addr)).balance
        vault_balance_after = (await provider.get_account_state(vault_address)).balance
        
        print(f"\nDeployer Balance After: {deployer_balance_after / 1e9:.9f} TON (Change: {(deployer_balance_after - balance) / 1e9:.9f} TON)")
        print(f"Admin Balance After:    {admin_balance_after / 1e9:.9f} TON")
        print(f"Platform Balance After: {platform_balance_after / 1e9:.9f} TON")
        print(f"Vault Balance After:    {vault_balance_after / 1e9:.9f} TON")
        
        admin_diff = admin_balance_after - admin_balance_before
        platform_diff = platform_balance_after - platform_balance_before
        
        print(f"\nAdmin Received:    {admin_diff / 1e9:.9f} TON")
        print(f"Platform Received: {platform_diff / 1e9:.9f} TON")
        
        print("\nTestnet deployment and split test complete!")

    finally:
        await provider.close_all()

if __name__ == "__main__":
    asyncio.run(main())


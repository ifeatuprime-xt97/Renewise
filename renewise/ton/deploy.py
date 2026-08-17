"""
renewise/ton/deploy.py

Testnet deployment + end-to-end payment verification script.

What it does
────────────
1. Computes the vault address deterministically (no network call).
2. Deploys the vault by sending the first payment (StateInit + Pay body in one tx).
3. Waits for the transaction to confirm on testnet (~15 s).
4. Reads admin and platform wallet balances before and after.
5. Prints a summary confirming the split worked end-to-end.

Prerequisites
─────────────
1. Build the contract:
       cd contracts && npm install && npm run build

2. Install Python deps:
       pip install -r requirements.txt

3. Configure .env (copy from .env.example):
       DEPLOYER_MNEMONIC   — 24-word mnemonic for a testnet wallet
       ADMIN_ADDRESS       — testnet TON address for admin share
       PLATFORM_ADDRESS    — testnet TON address for platform fee
       LOG_ADDRESS         — testnet address for PaymentLog messages
       TONCENTER_API_KEYS  — optional, avoids rate limits (comma-separated for multi-key)

4. Fund the deployer wallet with testnet TON:
       https://t.me/testgiver_ton_bot

5. Run:
       python -m renewise.ton.deploy

Testnet endpoint: https://testnet.toncenter.com/api/v2/jsonRPC
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from pytoniq import LiteBalancer, WalletV4R2
from pytoniq_core import Address, begin_cell

from renewise.ton.vault import (
    VaultParams,
    build_payment_link,
    required_payment_nano,
    expected_admin_amount,
    expected_platform_amount,
    MIN_GAS_RESERVE_NANO,
    _load_code_cell,
)

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

TESTNET_CONFIG = "https://ton.org/testnet-global.config.json"

PRICE         = 100_000_000   # 0.1 TON — small amount for testnet demo
BUYER_FEE_BPS = 200
ADMIN_FEE_BPS = 330
SUB_ID        = 1


# ── Helpers ───────────────────────────────────────────────────────────────────

def _nano_to_ton(nano: int) -> str:
    return f"{nano / 1e9:.9f}".rstrip("0").rstrip(".")


async def _get_balance(client: LiteBalancer, addr: Address) -> int:
    try:
        state = await client.get_account_state(addr)
        return state.balance
    except Exception:
        return 0


async def _wait_for_seqno_change(
    client: LiteBalancer,
    wallet: WalletV4R2,
    old_seqno: int,
    timeout: int = 90,
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(3)
        try:
            new_seqno = await wallet.get_seqno()
            if new_seqno != old_seqno:
                return
        except Exception:
            pass
    raise TimeoutError("Timed out waiting for transaction confirmation")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    mnemonic_str = os.environ.get("DEPLOYER_MNEMONIC", "")
    if not mnemonic_str:
        raise EnvironmentError("DEPLOYER_MNEMONIC not set in .env")

    admin_addr    = Address(os.environ["ADMIN_ADDRESS"])
    platform_addr = Address(os.environ["PLATFORM_ADDRESS"])
    log_addr      = Address(os.environ["LOG_ADDRESS"])

    mnemonic = mnemonic_str.split()
    if len(mnemonic) != 24:
        raise ValueError(f"DEPLOYER_MNEMONIC must be 24 words, got {len(mnemonic)}")

    # Load compiled contract code
    code_cell = _load_code_cell()

    from renewise.config import TRIGGER_WALLET
    if not TRIGGER_WALLET:
        print("ERROR: TRIGGER_WALLET is not set in config/env")
        return

    trigger_addr = Address(TRIGGER_WALLET)

    params = VaultParams(
        admin_wallet=admin_addr,
        platform_wallet=platform_addr,
        log_address=log_addr,
        trigger_wallet=trigger_addr,
        price=PRICE,
        buyer_fee_bps=BUYER_FEE_BPS,
        admin_fee_bps=ADMIN_FEE_BPS,
        subscription_id=SUB_ID,
    )

    # 1. Compute vault address (pure off-chain, instant)
    link = build_payment_link(params, code_cell=code_cell)
    vault_addr = Address(link.vault_address)

    print(f"\nVault address (deterministic): {link.vault_address}")
    print(f"Required payment:              {_nano_to_ton(link.required_nano)} TON")

    # 2. Connect to testnet via LiteClient
    print("\nConnecting to TON testnet…")
    client = LiteBalancer.from_testnet_config(trust_level=2)
    await client.start_up()

    # 3. Set up deployer wallet
    wallet = await WalletV4R2.from_mnemonic(client, mnemonic)
    deployer_bal = await _get_balance(client, wallet.address)
    print(f"Deployer:  {wallet.address.to_str(is_bounceable=False)}")
    print(f"Balance:   {_nano_to_ton(deployer_bal)} TON")

    if deployer_bal < 300_000_000:  # < 0.3 TON
        raise ValueError(
            "Deployer balance too low. Fund via https://t.me/testgiver_ton_bot"
        )

    # 4. Snapshot balances before
    admin_before    = await _get_balance(client, admin_addr)
    platform_before = await _get_balance(client, platform_addr)
    vault_before    = await _get_balance(client, vault_addr)
    print(f"\nBefore payment:")
    print(f"  Admin balance:    {_nano_to_ton(admin_before)} TON")
    print(f"  Platform balance: {_nano_to_ton(platform_before)} TON")

    # 5. Build and send the payment message (StateInit + Pay body)
    from pytoniq_core import StateInit, Cell
    state_init = StateInit.deserialize(
        Cell.one_from_boc(link.state_init_boc).begin_parse()
    )
    pay_body = Cell.one_from_boc(link.body_boc)

    old_seqno = await wallet.get_seqno()

    overpayment = 200_000_000
    total_send = link.required_nano + overpayment
    print(f"\nSending {_nano_to_ton(total_send)} TON to vault (intentionally overpaying by 0.2 TON)…")
    print(f"  price={_nano_to_ton(PRICE)} + buyer_fee={_nano_to_ton(PRICE * BUYER_FEE_BPS // 10000)} + gas_reserve=0.05 + overpayment=0.2")

    await wallet.transfer(
        destination=vault_addr,
        amount=total_send,
        state_init=state_init,
        body=pay_body,
    )

    # 6. Wait for confirmation
    print("\nWaiting for confirmation (~15 s)…")
    await _wait_for_seqno_change(client, wallet, old_seqno)
    await asyncio.sleep(5)  # extra settle time for outgoing messages

    # 7. Read balances after
    admin_after    = await _get_balance(client, admin_addr)
    platform_after = await _get_balance(client, platform_addr)
    vault_after    = await _get_balance(client, vault_addr)

    admin_received    = admin_after - admin_before
    platform_received = platform_after - platform_before
    vault_delta       = vault_after - vault_before  # should be: overpayment + gas_reserve - gas_used

    exp_admin    = expected_admin_amount(PRICE, ADMIN_FEE_BPS)
    exp_platform = expected_platform_amount(PRICE, BUYER_FEE_BPS, ADMIN_FEE_BPS)
    # Expected vault delta: overpayment + gas_reserve (minus tiny gas_used ~0.001)
    exp_vault_delta_max = overpayment + MIN_GAS_RESERVE_NANO + 5_000_000

    # 8. Print summary
    sep = "─" * 54
    print(f"\n{sep}")
    print("  TESTNET SPLIT RESULT")
    print(sep)
    print(f"  Price:              {_nano_to_ton(PRICE)} TON")
    print(f"  Buyer fee (2%):     {_nano_to_ton(PRICE * BUYER_FEE_BPS // 10000)} TON")
    print(f"  Admin fee (3.3%):   {_nano_to_ton(PRICE * ADMIN_FEE_BPS // 10000)} TON")
    print(sep)
    print(f"  Admin received:     {_nano_to_ton(admin_received)} TON  (expected ~{_nano_to_ton(exp_admin)})")
    print(f"  Platform received:  {_nano_to_ton(platform_received)} TON  (expected ~{_nano_to_ton(exp_platform)})")
    print(f"  Vault delta:        +{_nano_to_ton(vault_delta)} TON  (overpayment + gas reserve)")
    print(sep)

    ok = (admin_received > 0 and platform_received > 0
          and 0 < vault_delta <= exp_vault_delta_max)
    print(f"\n  {'✅ Split verified end-to-end!' if ok else '❌ Split verification FAILED — check balances above'}")

    await client.close_all()
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

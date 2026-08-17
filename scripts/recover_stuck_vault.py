"""
scripts/recover_stuck_vault.py

Diagnose and recover TON stuck in a PaymentVault contract.

The money gets stuck when:
  - The vault address is non-bounceable (is_bounceable=False in ton_link)
  - The Pay{} require() fails (e.g. amount < required) -> no bounce -> stuck
  - Or the user sent plain TON without the Pay{} opcode body

This script:
  1. Looks up the vault + subscription from the DB by Telegram user ID
  2. Queries TonCenter to show vault balance + transaction history
  3. Explains what went wrong
  4. Generates a recovery Tonkeeper deep-link with the CORRECT amount
  5. Optionally sends Pay{} programmatically from a hot wallet (if DEPLOYER_MNEMONIC is set)

Usage:
    python scripts/recover_stuck_vault.py <telegram_user_id>

    # To also trigger the Pay{} recovery transaction:
    DEPLOYER_MNEMONIC="word1 word2 ... word24" python scripts/recover_stuck_vault.py <telegram_user_id>
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Make sure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import aiohttp
import aiosqlite

from renewise.config import DATABASE_PATH, PLATFORM_WALLET, LOG_ADDRESS, TRIGGER_WALLET
from renewise.ton.vault import VaultParams, PAY_OPCODE
from pytoniq_core import Address, Cell, StateInit, begin_cell
from renewise.watcher.config import TONCENTER_BASE_URL, TONCENTER_API_KEYS

def _load_old_code_cell() -> Cell:
    boc_path = Path(__file__).parent.parent / "contracts" / "build" / "PaymentVault_PaymentVault.old.code.boc"
    if not boc_path.exists():
        # Fallback to current boc if old doesn't exist
        boc_path = Path(__file__).parent.parent / "contracts" / "build" / "PaymentVault_PaymentVault.code.boc"
    return Cell.one_from_boc(boc_path.read_bytes())

def _build_old_data_cell(p: VaultParams) -> Cell:
    b_1 = (
        begin_cell()
        .store_address(p.trigger_wallet)
        .store_coins(p.price)
        .store_uint(p.buyer_fee_bps, 16)
        .store_uint(p.admin_fee_bps, 16)
        .store_uint(p.subscription_id, 64)
        .store_coins(0)
        .end_cell()
    )
    return (
        begin_cell()
        .store_address(p.admin_wallet)
        .store_address(p.platform_wallet)
        .store_address(p.log_address)
        .store_ref(b_1)
        .end_cell()
    )


def _nano_to_ton(nano: int) -> str:
    return f"{nano / 1e9:.9f}".rstrip("0").rstrip(".")


async def _toncenter_get(session: aiohttp.ClientSession, endpoint: str, params: dict) -> dict:
    url = f"{TONCENTER_BASE_URL}/{endpoint}"
    headers = {}
    if TONCENTER_API_KEYS:
        headers["X-API-Key"] = TONCENTER_API_KEYS[0]
    async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        resp.raise_for_status()
        return await resp.json()


async def get_vault_balance(session: aiohttp.ClientSession, address: str) -> int:
    """Return vault balance in nanoTON, or 0 if undeployed."""
    try:
        data = await _toncenter_get(session, "getAddressBalance", {"address": address})
        if data.get("ok"):
            return int(data.get("result", 0))
    except Exception as e:
        print(f"  [warn] Could not fetch balance: {e}")
    return 0


async def get_vault_state(session: aiohttp.ClientSession, address: str) -> str:
    """Return 'active', 'uninitialized', or 'frozen'."""
    try:
        data = await _toncenter_get(session, "getAddressInformation", {"address": address})
        if data.get("ok"):
            return data["result"].get("state", "unknown")
    except Exception as e:
        print(f"  [warn] Could not fetch state: {e}")
    return "unknown"


async def get_vault_transactions(session: aiohttp.ClientSession, address: str, limit: int = 10) -> list:
    """Return the last N transactions for this vault address."""
    try:
        data = await _toncenter_get(session, "getTransactions", {"address": address, "limit": limit})
        if data.get("ok"):
            return data.get("result", [])
    except Exception as e:
        print(f"  [warn] Could not fetch transactions: {e}")
    return []


async def lookup_user_vault(telegram_user_id: int) -> dict | None:
    """
    Look up the most recent subscription + vault address for a Telegram user ID.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute(
            "SELECT id FROM users WHERE telegram_user_id=?",
            (telegram_user_id,),
        )
        user_row = await cur.fetchone()
        if not user_row:
            return None
        db_user_id = user_row["id"]

        cur = await db.execute(
            """
            SELECT s.id, s.group_id, s.status, s.required_nano_amount,
                   s.price_locked_in,
                   g.telegram_chat_id, g.chat_title, g.payout_wallet_address,
                   g.price_usd_cents, g.billing_interval_days,
                   g.buyer_fee_bps, g.admin_fee_bps,
                   vr.vault_address
            FROM subscriptions s
            JOIN groups g ON g.id = s.group_id
            LEFT JOIN vault_registry vr
                   ON vr.user_id = s.user_id AND vr.group_id = s.group_id
            WHERE s.user_id = ?
            ORDER BY s.created_at DESC
            LIMIT 1
            """,
            (db_user_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return dict(row)


async def main(telegram_user_id: int) -> None:
    SEP = "-" * 60

    print(f"\n{SEP}")
    print(f"  VAULT RECOVERY DIAGNOSTIC")
    print(f"  Telegram User ID: {telegram_user_id}")
    print(SEP)

    # 1. DB lookup
    print("\n[1/5] Looking up subscription in database...")
    info = await lookup_user_vault(telegram_user_id)
    if not info:
        print("  ERROR: No subscription found for this user ID.")
        return

    vault_address = info.get("vault_address")
    sub_id        = info["id"]
    group_id      = info["group_id"]
    chat_title    = info["chat_title"] or f"Group {group_id}"
    sub_status    = info["status"]
    required_nano = info["required_nano_amount"]
    admin_wallet  = info["payout_wallet_address"]

    print(f"  Subscription ID : {sub_id}")
    print(f"  Group           : {chat_title}")
    print(f"  Sub status      : {sub_status}")
    print(f"  Vault address   : {vault_address or '(not registered)'}")
    print(f"  Required nano   : {_nano_to_ton(required_nano) if required_nano else 'unknown'} TON")

    if not vault_address:
        print("\n  ERROR: No vault address registered.")
        print("  Ask the user to request to join the group again to get a fresh link.")
        return

    if not admin_wallet:
        print("\n  ERROR: No payout wallet configured for this group.")
        return

    # 2. On-chain state
    print(f"\n[2/5] Querying TonCenter for vault state...")
    async with aiohttp.ClientSession() as session:
        vault_state   = await get_vault_state(session, vault_address)
        vault_balance = await get_vault_balance(session, vault_address)
        txs           = await get_vault_transactions(session, vault_address, limit=10)

    print(f"  Contract state  : {vault_state}")
    print(f"  Vault balance   : {_nano_to_ton(vault_balance)} TON  ({vault_balance} nanoTON)")

    # 3. Transaction history
    print(f"\n[3/5] Transaction history (newest first):")
    if not txs:
        print("  (no transactions found)")
    for i, tx in enumerate(txs[:5]):
        tx_id     = tx.get("transaction_id", {})
        tx_hash   = tx_id.get("hash", "?")[:16]
        in_msg    = tx.get("in_msg", {})
        in_value  = int(in_msg.get("value", 0))
        out_msgs  = tx.get("out_msgs", [])
        fee       = int(tx.get("fee", 0))
        compute   = tx.get("description", {}).get("compute_ph", {})
        exit_code = compute.get("exit_code", "?")
        aborted   = compute.get("aborted", False)

        status_icon = "FAIL" if aborted else "OK  "
        print(f"  [{status_icon}] tx[{i}] hash=...{tx_hash}  in={_nano_to_ton(in_value)} TON  "
              f"fee={_nano_to_ton(fee)} TON  exit_code={exit_code}  out_msgs={len(out_msgs)}")

    # 4. Diagnosis
    print(f"\n[4/5] Diagnosis:")

    if vault_state == "uninitialized" and vault_balance > 0:
        print("  STUCK (Case A): TON was sent to vault BEFORE contract was deployed.")
        print("  The vault contract does not exist yet at this address.")
        print("  FIX: Send the original Tonkeeper deep link (StateInit + Pay body).")
        print("  This will deploy the contract AND trigger the split in one tx.")
    elif vault_state == "active" and vault_balance > 0:
        has_out_msgs = any(len(tx.get("out_msgs", [])) > 0 for tx in txs[:3])
        if not has_out_msgs:
            print("  STUCK (Case B): Contract deployed, but Pay{} opcode was never received.")
            print("  Plain TON arrived without the message body — no split was triggered.")
            print("  FIX: Send a NEW message WITH the Pay{} body to trigger the split.")
        else:
            print("  STUCK (Case C): Pay{} was triggered but residual balance remains.")
            print("  The split ran but something left dust behind. Likely OK if small amount.")
    elif vault_state == "active" and vault_balance == 0:
        print("  Vault is EMPTY — funds were split correctly.")
        print("  If subscription is not active yet, check the watcher log or run /ive_paid.")
        return
    else:
        print(f"  Unknown state ({vault_state}, balance={vault_balance}). Manual check needed.")

    print(f"\n  Total stuck: {_nano_to_ton(vault_balance)} TON")

    # 5. Recovery options
    print(f"\n[5/5] Recovery options:")

    # Regenerate the correct payment link
    try:
        from renewise.db.queries import get_global_fees
        global_buyer_bps, global_admin_bps = await get_global_fees()
        buyer_bps = info["buyer_fee_bps"] if info["buyer_fee_bps"] is not None else global_buyer_bps
        admin_bps = info["admin_fee_bps"]  if info["admin_fee_bps"]  is not None else global_admin_bps

        from pytoniq_core import Address

        # Back-calculate the base price from required_nano:
        # required = price + price*buyer_bps/10000 + gas_reserve
        # => price = (required - gas_reserve) / (1 + buyer_bps/10000)
        OLD_GAS_RESERVE = 20_000_000  # 0.02 TON (old contract value)
        if required_nano and required_nano > OLD_GAS_RESERVE:
            est_price = int((required_nano - OLD_GAS_RESERVE) / (1 + buyer_bps / 10000))
        else:
            # Fallback: use vault balance minus gas
            est_price = max(0, vault_balance - OLD_GAS_RESERVE)

        old_code_cell = _load_old_code_cell()
        target_hash = Address(vault_address).hash_part
        price_nano = est_price
        
        # Search around the estimate for the exact price that matches the vault hash
        found = False
        for offset in range(-5, 6):
            test_price = est_price + offset
            if test_price < 0: continue
            
            p = VaultParams(
                admin_wallet    = Address(admin_wallet),
                platform_wallet = Address(PLATFORM_WALLET),
                log_address     = Address(LOG_ADDRESS),
                trigger_wallet  = Address(TRIGGER_WALLET),
                price           = test_price,
                buyer_fee_bps   = buyer_bps,
                admin_fee_bps   = admin_bps,
                subscription_id = sub_id,
            )
            old_data_cell = _build_old_data_cell(p)
            state_init_cell = (
                begin_cell()
                .store_bit(0).store_bit(0).store_bit(1).store_ref(old_code_cell)
                .store_bit(1).store_ref(old_data_cell).store_bit(0)
                .end_cell()
            )
            if state_init_cell.hash == target_hash:
                price_nano = test_price
                found = True
                break
                
        if not found:
            print("  [warn] Could not perfectly match the vault hash with back-calculation.")

        params = VaultParams(
            admin_wallet    = Address(admin_wallet),
            platform_wallet = Address(PLATFORM_WALLET),
            log_address     = Address(LOG_ADDRESS),
            trigger_wallet  = Address(TRIGGER_WALLET),
            price           = price_nano,
            buyer_fee_bps   = buyer_bps,
            admin_fee_bps   = admin_bps,
            subscription_id = sub_id,
        )
        
        # Build the exact old StateInit that matches the stuck contract
        old_code_cell = _load_old_code_cell()
        old_data_cell = _build_old_data_cell(params)
        
        state_init_cell = (
            begin_cell()
            .store_bit(0)
            .store_bit(0)
            .store_bit(1)
            .store_ref(old_code_cell)
            .store_bit(1)
            .store_ref(old_data_cell)
            .store_bit(0)
            .end_cell()
        )
        old_vault_address = Address((0, state_init_cell.hash))
        old_vault_address.is_bounceable = True
        addr_str = old_vault_address.to_str(is_bounceable=True, is_url_safe=True)
        
        import base64
        body_boc = begin_cell().store_uint(PAY_OPCODE, 32).end_cell().to_boc()
        init_boc = state_init_cell.to_boc()
        
        body_b64 = base64.urlsafe_b64encode(body_boc).decode().rstrip("=")
        init_b64 = base64.urlsafe_b64encode(init_boc).decode().rstrip("=")
        
        required_nano_amount = price_nano + (price_nano * buyer_bps // 10000) + OLD_GAS_RESERVE
        
        tonkeeper_link = (
            f"https://app.tonkeeper.com/transfer/{addr_str}"
            f"?amount={required_nano_amount}"
            f"&bin={body_b64}"
            f"&init={init_b64}"
        )
        
        print(f"\n  [Option A] Send this link to the user in Telegram:")
        print(f"  {tonkeeper_link}")
        print(f"\n  Required amount : {_nano_to_ton(required_nano_amount)} TON")
        print(f"  NOTE: If vault already has balance, user still needs to send full amount.")
        print(f"  The vault splits context().value (the NEW message value), not its stored balance.")
    except Exception as e:
        print(f"\n  [warn] Could not regenerate payment link: {e}")

    # Programmatic recovery
    mnemonic_str = os.getenv("DEPLOYER_MNEMONIC", "")
    if mnemonic_str and vault_state == "active":
        print(f"\n  [Option B] Programmatic Pay{{}} trigger from hot wallet:")
        await _send_pay_opcode(vault_address, mnemonic_str, required_nano or vault_balance)
    elif vault_state == "active":
        print(f"\n  [Option B] Programmatic recovery (from hot wallet):")
        print(f"  Set DEPLOYER_MNEMONIC=\"word1 ... word24\" and re-run this script.")
        print(f"  Amount to send: {_nano_to_ton(required_nano or vault_balance)} TON")

    print(f"\n{SEP}")
    network = "testnet." if "testnet" in TONCENTER_BASE_URL else ""
    print(f"  View vault on TonScan:")
    print(f"  https://{network}tonscan.org/address/{vault_address}")
    print(SEP)


async def _send_pay_opcode(vault_address: str, mnemonic_str: str, amount_nano: int) -> None:
    """Send a Pay{} message to an already-deployed vault from a hot wallet."""
    try:
        from pytoniq import LiteBalancer, WalletV4R2
        from pytoniq_core import Address, begin_cell

        mnemonic = mnemonic_str.strip().split()
        if len(mnemonic) != 24:
            print(f"  ERROR: DEPLOYER_MNEMONIC must be 24 words, got {len(mnemonic)}.")
            return

        is_testnet = "testnet" in TONCENTER_BASE_URL
        config_url = ("https://ton.org/testnet-global.config.json"
                      if is_testnet else "https://ton.org/global.config.json")

        print(f"  Connecting to TON {'testnet' if is_testnet else 'mainnet'}...")
        client = LiteBalancer.from_config(config_url, trust_level=2)
        await client.start_up()

        wallet     = await WalletV4R2.from_mnemonic(client, mnemonic)
        wallet_bal = (await client.get_account_state(wallet.address)).balance
        print(f"  Hot wallet : {wallet.address.to_str(is_bounceable=False)}")
        print(f"  Balance    : {_nano_to_ton(wallet_bal)} TON")

        if wallet_bal < amount_nano + 50_000_000:
            print(f"  ERROR: Hot wallet balance too low.")
            print(f"  Need at least {_nano_to_ton(amount_nano + 50_000_000)} TON.")
            await client.close()
            return

        pay_body = begin_cell().store_uint(PAY_OPCODE, 32).end_cell()
        vault    = Address(vault_address)

        print(f"  Sending {_nano_to_ton(amount_nano)} TON + Pay{{}} opcode to vault...")
        await wallet.transfer(
            destination = vault,
            amount      = amount_nano,
            body        = pay_body,
            bounce      = False,
        )
        print(f"  OK: Pay{{}} message sent. Wait ~15s then check TonScan.")
        await client.close()

    except ImportError:
        print("  ERROR: pytoniq not installed. Run: pip install pytoniq")
    except Exception as e:
        print(f"  ERROR: Programmatic send failed: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/recover_stuck_vault.py <telegram_user_id>")
        sys.exit(1)

    try:
        user_id = int(sys.argv[1])
    except ValueError:
        print(f"Error: '{sys.argv[1]}' is not a valid Telegram user ID (must be an integer).")
        sys.exit(1)

    asyncio.run(main(user_id))

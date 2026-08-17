"""
Send a second Pay{} message to the ALREADY-DEPLOYED vault to test
if the split fires when it's not a fresh deploy.
If it works, the issue is deploy-on-first-message balance.
"""
import os, sys, asyncio, time, aiohttp, urllib.parse
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

from pytoniq import LiteBalancer, WalletV4R2
from pytoniq_core import Address, Cell
from renewise.ton.vault import VaultParams, build_payment_link, _load_code_cell
from renewise.config import PLATFORM_WALLET, LOG_ADDRESS, TRIGGER_WALLET, BUYER_FEE_BPS, ADMIN_FEE_BPS

# Use the most recent vault — already deployed with latest build
VAULT = "EQDkk9uBDp6BqvAFOR-wchG7LOjozbBvWktM-QEaluwo3n6_"

PRICE  = 100_000_000
SUB_ID = 1  # deploy.py always uses SUB_ID=1

ADMIN_ADDR    = os.environ["ADMIN_ADDRESS"]
API_KEY       = os.getenv("TONCENTER_API_KEY", "")

async def get_balance(s, addr):
    async with s.get("https://testnet.toncenter.com/api/v2/getAddressBalance",
                     params={"address": addr},
                     headers={"X-API-Key": API_KEY} if API_KEY else {},
                     timeout=aiohttp.ClientTimeout(total=10)) as r:
        return int((await r.json()).get("result", 0))

async def main():
    mnemonic = os.environ["DEPLOYER_MNEMONIC"].split()
    admin_addr    = Address(ADMIN_ADDR)
    platform_addr = Address(PLATFORM_WALLET)

    code_cell = _load_code_cell()
    params = VaultParams(
        admin_wallet    = admin_addr,
        platform_wallet = platform_addr,
        log_address     = Address(LOG_ADDRESS),
        trigger_wallet  = Address(TRIGGER_WALLET),
        price           = PRICE,
        buyer_fee_bps   = BUYER_FEE_BPS,
        admin_fee_bps   = ADMIN_FEE_BPS,
        subscription_id = SUB_ID,
    )
    link = build_payment_link(params, code_cell=code_cell)

    # Confirm the vault address matches
    print(f"Computed vault: {link.vault_address}")
    print(f"Target vault  : {VAULT}")
    print(f"Match         : {link.vault_address == VAULT}")
    if link.vault_address != VAULT:
        print("ERROR: vault address mismatch — check SUB_ID")
        return
    print()

    client = LiteBalancer.from_testnet_config(trust_level=2)
    await client.start_up()

    wallet = await WalletV4R2.from_mnemonic(client, mnemonic)

    # Snapshot before
    async with aiohttp.ClientSession() as s:
        admin_before    = await get_balance(s, ADMIN_ADDR)
        platform_before = await get_balance(s, PLATFORM_WALLET)
        vault_before    = await get_balance(s, VAULT)
    print(f"Vault balance (before): {vault_before/1e9:.6f} TON")
    print(f"Admin before          : {admin_before/1e9:.6f} TON")
    print(f"Platform before       : {platform_before/1e9:.6f} TON")
    print()

    # Send Pay{} WITHOUT StateInit — contract already deployed
    pay_body = Cell.one_from_boc(link.body_boc)
    old_seqno = await wallet.get_seqno()
    send_amount = link.required_nano

    print(f"Sending {send_amount/1e9:.6f} TON Pay{{}} to already-deployed vault (NO StateInit)...")
    await wallet.transfer(
        destination=Address(VAULT),
        amount=send_amount,
        body=pay_body,
        # NO state_init — contract already exists
    )

    print("Waiting for confirmation...")
    deadline = time.time() + 90
    while time.time() < deadline:
        await asyncio.sleep(3)
        try:
            if await wallet.get_seqno() != old_seqno:
                print("Confirmed.")
                break
        except Exception:
            pass
    await asyncio.sleep(8)

    # Check results
    async with aiohttp.ClientSession() as s:
        admin_after    = await get_balance(s, ADMIN_ADDR)
        platform_after = await get_balance(s, PLATFORM_WALLET)
        vault_after    = await get_balance(s, VAULT)

    admin_recv    = admin_after - admin_before
    platform_recv = platform_after - platform_before
    exp_admin    = PRICE - (PRICE * int(os.getenv("ADMIN_FEE_BPS","330")) // 10000)
    exp_platform = (PRICE * int(os.getenv("BUYER_FEE_BPS","200")) // 10000) + (PRICE * int(os.getenv("ADMIN_FEE_BPS","330")) // 10000)

    sep = "-" * 58
    print()
    print(sep)
    print("  SECOND PAY TEST (already-deployed vault, no StateInit)")
    print(sep)
    print(f"  Admin received    : {admin_recv/1e9:.6f} TON  (expected ~{exp_admin/1e9:.6f})")
    print(f"  Platform received : {platform_recv/1e9:.6f} TON  (expected ~{exp_platform/1e9:.6f})")
    print(f"  Vault after       : {vault_after/1e9:.6f} TON")
    print(sep)
    ok = admin_recv > 1_000_000 and platform_recv > 1_000_000
    print(f"  Split fired: {'YES — this is a deploy-on-first-message bug' if ok else 'NO — different issue'}")
    print(sep)

    await client.close_all()

asyncio.run(main())

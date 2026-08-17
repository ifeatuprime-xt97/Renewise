"""
Read the contract's stored admin/platform addresses via config() getter
and compare them to .env values.
"""
import os, sys, asyncio, aiohttp, base64
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()
from pytoniq_core import Address, Cell

API_KEY = os.getenv("TONCENTER_API_KEY", "")
BASE_V2 = "https://testnet.toncenter.com/api/v2"
VAULT   = "EQDkk9uBDp6BqvAFOR-wchG7LOjozbBvWktM-QEaluwo3n6_"

async def main():
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    async with aiohttp.ClientSession() as s:
        # config() getter returns a tuple — use the raw stack
        async with s.post(f"{BASE_V2}/runGetMethod",
                          json={"address": VAULT, "method": "config", "stack": []},
                          headers=headers,
                          timeout=aiohttp.ClientTimeout(total=10)) as r:
            result = (await r.json()).get("result", {})

    stack = result.get("stack", [])
    exit_code = result.get("exit_code")
    print(f"config() exit_code: {exit_code}")
    print(f"stack length      : {len(stack)}")
    print()

    # Stack items are [type, value] pairs
    # Expected order from VaultConfig: admin_wallet, platform_wallet, log_address,
    # trigger_wallet, price, buyer_fee_bps, admin_fee_bps, subscription_id
    labels = ["admin_wallet", "platform_wallet", "log_address", "trigger_wallet",
              "price", "buyer_fee_bps", "admin_fee_bps", "subscription_id"]
    for i, item in enumerate(stack):
        label = labels[i] if i < len(labels) else f"item_{i}"
        typ   = item[0] if isinstance(item, (list,tuple)) and len(item)>0 else "?"
        val   = item[1] if isinstance(item, (list,tuple)) and len(item)>1 else item
        if typ == "cell":
            # Address stored as cell — decode it
            try:
                cell_bytes = base64.b64decode(str(val) + "==")
                cell = Cell.one_from_boc(cell_bytes)
                sl   = cell.begin_parse()
                addr = sl.load_address()
                print(f"  {label}: {addr}")
            except Exception as e:
                print(f"  {label}: [cell decode error: {e}]  raw={str(val)[:40]}")
        elif typ == "num":
            n = int(str(val), 16) if str(val).startswith("0x") else int(str(val))
            print(f"  {label}: {n}  ({n/1e9:.9f} TON)" if i==4 else f"  {label}: {n}")
        else:
            print(f"  {label}: [{typ}] {val}")

    print()
    print("=== .env values ===")
    print(f"  ADMIN_ADDRESS  : {os.getenv('ADMIN_ADDRESS')}")
    print(f"  PLATFORM_WALLET: {os.getenv('PLATFORM_WALLET')}")
    print(f"  LOG_ADDRESS    : {os.getenv('LOG_ADDRESS')}")
    print(f"  TRIGGER_WALLET : {os.getenv('TRIGGER_WALLET')}")

asyncio.run(main())

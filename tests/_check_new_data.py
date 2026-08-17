"""Quick check of the latest vault's on-chain data."""
import os, sys, asyncio, aiohttp, base64
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()
from pytoniq_core import Cell, Address

API_KEY = os.getenv("TONCENTER_API_KEY", "")
BASE_V2 = "https://testnet.toncenter.com/api/v2"
VAULT   = "EQB-lqALJaM7Wull0VHlg2fHC-cqd34s6NkXPxP1SpiGndQS"

async def main():
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{BASE_V2}/getAddressInformation",
                         params={"address": VAULT}, headers=headers,
                         timeout=aiohttp.ClientTimeout(total=10)) as r:
            info = (await r.json()).get("result", {})

    data_b64 = info.get("data", "")
    bal = int(info.get("balance", 0))
    state = info.get("state")
    print(f"State  : {state}")
    print(f"Balance: {bal/1e9:.9f} TON")
    print()

    if data_b64:
        cell = Cell.one_from_boc(base64.b64decode(data_b64 + "=="))
        sl   = cell.begin_parse()
        print(f"Root cell bits: {sl.remaining_bits}  refs: {sl.remaining_refs}")

        # Parse as init layout
        try:
            admin    = sl.load_address()
            platform = sl.load_address()
            log      = sl.load_address()
            print(f"  admin_wallet   : {admin}")
            print(f"  platform_wallet: {platform}")
            print(f"  log_address    : {log}")
        except Exception as e:
            print(f"  Address parse error: {e}")
            return

        b1 = sl.load_ref().begin_parse()
        try:
            trigger = b1.load_address()
            print(f"  trigger_wallet : {trigger}")
            # Try reading price as Int(257) — which is 257 bits
            price = b1.load_int(257)
            bfbps = b1.load_int(257)
            print(f"  price (Int257) : {price}")
            print(f"  buyer_fee_bps  : {bfbps}")
            b2 = b1.load_ref().begin_parse()
            afbps = b2.load_int(257)
            sub   = b2.load_int(257)
            print(f"  admin_fee_bps  : {afbps}")
            print(f"  subscription_id: {sub}")
        except Exception as e:
            print(f"  b_1 parse error: {e}")
            # Try alternative: varuint16
            b1 = sl  # won't work but shows error
    else:
        print("No data cell")

asyncio.run(main())

"""
Check the runtime state of the latest vault via getAddressInformation data field.
The runtime storage ($PaymentVault$_load) uses:
  b_0: 1 bit (Tact init flag) | ref(b_1)
  b_1: admin_wallet | platform_wallet | log_address | ref(b_2)  (or different layout)

Actually let's just read the raw data and decode it per the $PaymentVault$_load layout.
"""
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
    if not data_b64:
        print("No data"); return

    cell = Cell.one_from_boc(base64.b64decode(data_b64 + "=="))
    sl   = cell.begin_parse()
    print(f"Root bits: {sl.remaining_bits}  refs: {sl.remaining_refs}")

    # Runtime layout from FunC:
    # b_0: admin_wallet | platform_wallet | log_address | ref(b_1)
    # b_1: trigger_wallet | price(varuint16) | buyer_fee_bps(16) | admin_fee_bps(16) | subscription_id(64) | overage_held(varuint16) | accumulated_paid(varuint16)
    # But root only has 7 bits — meaning Tact added a 1-bit "initialized" flag at the start
    # plus 2 bits for addr_none for admin (addr_none = 2 bits "00") x3 = 6 bits + 1 = 7 bits?
    # Actually: addr_none = 0b00 = 2 bits. Three addr_none = 6 bits + 1 init bit = 7 bits!

    # The Tact "not initialized" flag (1 bit, value=0) + 3x addr_none (2 bits each) = 7 bits
    # When reading runtime storage, if Tact sees init_flag=0, addresses are null (addr_none)

    # Let's read raw
    raw_bits = []
    sl2 = cell.begin_parse()
    try:
        # Read all bits
        bit_count = sl2.remaining_bits
        raw_int = sl2.load_uint(bit_count)
        raw_bits_str = bin(raw_int)[2:].zfill(bit_count)
        print(f"Raw bits (all {bit_count}): {raw_bits_str}")
    except Exception as e:
        print(f"Error reading bits: {e}")

    # Try runtime load: first addr, second addr, third addr, then ref
    sl3 = cell.begin_parse()
    for label in ["admin_wallet", "platform_wallet", "log_address"]:
        try:
            addr = sl3.load_address()
            print(f"  {label}: {addr}")
        except Exception as e:
            print(f"  {label}: {e}  (bits left: {sl3.remaining_bits})")

    # Ref
    if sl3.remaining_refs > 0:
        b1 = sl3.load_ref().begin_parse()
        print(f"  b_1 bits: {b1.remaining_bits}  refs: {b1.remaining_refs}")
        try:
            trigger  = b1.load_address()
            price    = b1.load_coins()  # varuint16
            bfbps    = b1.load_uint(16)
            afbps    = b1.load_uint(16)
            sub      = b1.load_uint(64)
            overage  = b1.load_coins()
            accum    = b1.load_coins()
            print(f"  trigger_wallet  : {trigger}")
            print(f"  price           : {price}")
            print(f"  buyer_fee_bps   : {bfbps}")
            print(f"  admin_fee_bps   : {afbps}")
            print(f"  subscription_id : {sub}")
            print(f"  overage_held    : {overage}  ({overage/1e9:.6f} TON)")
            print(f"  accumulated_paid: {accum}")
        except Exception as e:
            print(f"  b_1 parse error: {e}")

asyncio.run(main())

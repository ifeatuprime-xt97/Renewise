"""
Decode the actual init data cell stored in the vault and compare to
what vault.py builds. Find why addresses come out as addr_none.
"""
import os, sys, base64, asyncio, aiohttp
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()
from pytoniq_core import Address, Cell, begin_cell

API_KEY = os.getenv("TONCENTER_API_KEY", "")
BASE_V2 = "https://testnet.toncenter.com/api/v2"
VAULT   = "EQDkk9uBDp6BqvAFOR-wchG7LOjozbBvWktM-QEaluwo3n6_"

async def main():
    # Fetch the actual on-chain data cell
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{BASE_V2}/getAddressInformation",
                         params={"address": VAULT},
                         headers=headers,
                         timeout=aiohttp.ClientTimeout(total=10)) as r:
            info = (await r.json()).get("result", {})

    data_b64 = info.get("data", "")
    print(f"On-chain data cell (b64): {data_b64[:80]}...")
    print()

    if data_b64:
        data_bytes = base64.b64decode(data_b64 + "==")
        cell = Cell.one_from_boc(data_bytes)
        sl   = cell.begin_parse()
        print(f"Total bits in data cell: {sl.remaining_bits}")
        print(f"Total refs in data cell: {sl.remaining_refs}")
        print()

        # Try to read according to vault.py's _build_data_cell layout:
        # b_0: admin_wallet | platform_wallet | log_address | ref(b_1)
        # b_1: trigger_wallet | price(coins) | buyer_fee_bps(16) | admin_fee_bps(16) | subscription_id(64) | overage_held(coins) | accumulated_paid(coins)
        print("=== Parsing b_0 (root cell) ===")
        try:
            admin = sl.load_address()
            print(f"  admin_wallet   : {admin}")
        except Exception as e:
            print(f"  admin_wallet   : ERROR {e}  (remaining_bits={sl.remaining_bits})")

        try:
            platform = sl.load_address()
            print(f"  platform_wallet: {platform}")
        except Exception as e:
            print(f"  platform_wallet: ERROR {e}")

        try:
            log = sl.load_address()
            print(f"  log_address    : {log}")
        except Exception as e:
            print(f"  log_address    : ERROR {e}")

        # ref b_1
        try:
            b1 = sl.load_ref().begin_parse()
            print()
            print("=== Parsing b_1 (ref cell) ===")
            trigger  = b1.load_address()
            price    = b1.load_coins()
            bfbps    = b1.load_uint(16)
            afbps    = b1.load_uint(16)
            sub_id   = b1.load_uint(64)
            overage  = b1.load_coins()
            accum    = b1.load_coins()
            print(f"  trigger_wallet  : {trigger}")
            print(f"  price           : {price}  ({price/1e9:.6f} TON)")
            print(f"  buyer_fee_bps   : {bfbps}")
            print(f"  admin_fee_bps   : {afbps}")
            print(f"  subscription_id : {sub_id}")
            print(f"  overage_held    : {overage}  ({overage/1e9:.6f} TON)")
            print(f"  accumulated_paid: {accum}")
        except Exception as e:
            print(f"  b_1 parse error: {e}")

    print()
    # Now build the expected data cell from vault.py and show what IT produces
    from renewise.ton.vault import VaultParams, _build_data_cell
    from renewise.config import PLATFORM_WALLET, LOG_ADDRESS, TRIGGER_WALLET, BUYER_FEE_BPS, ADMIN_FEE_BPS

    params = VaultParams(
        admin_wallet    = Address(os.environ["ADMIN_ADDRESS"]),
        platform_wallet = Address(PLATFORM_WALLET),
        log_address     = Address(LOG_ADDRESS),
        trigger_wallet  = Address(TRIGGER_WALLET),
        price           = 100_000_000,
        buyer_fee_bps   = BUYER_FEE_BPS,
        admin_fee_bps   = ADMIN_FEE_BPS,
        subscription_id = 1,
    )
    expected = _build_data_cell(params)
    print(f"=== Expected data cell from vault.py ===")
    print(f"Bits: {expected.begin_parse().remaining_bits}  Refs: {expected.begin_parse().remaining_refs}")
    sl2 = expected.begin_parse()
    print(f"  admin_wallet   : {sl2.load_address()}")
    print(f"  platform_wallet: {sl2.load_address()}")
    print(f"  log_address    : {sl2.load_address()}")
    b2 = sl2.load_ref().begin_parse()
    print(f"  trigger_wallet  : {b2.load_address()}")
    print(f"  price           : {b2.load_coins()}")
    print(f"  buyer_fee_bps   : {b2.load_uint(16)}")
    print(f"  admin_fee_bps   : {b2.load_uint(16)}")
    print(f"  sub_id          : {b2.load_uint(64)}")

asyncio.run(main())

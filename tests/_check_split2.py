import os, sys, asyncio, aiohttp
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

API_KEY       = os.getenv("TONCENTER_API_KEY", "")
BASE_V2       = "https://testnet.toncenter.com/api/v2"
VAULT         = "EQBTUuOHtzblewkZr_x8K1F4jBqEqDTAL2z173aeNe_V0ZMt"
ADMIN_ADDR    = os.getenv("ADMIN_ADDRESS", "")
PLATFORM_ADDR = os.getenv("PLATFORM_WALLET", "")

PRICE         = 100_000_000
BUYER_FEE_BPS = int(os.getenv("BUYER_FEE_BPS", "200"))
ADMIN_FEE_BPS = int(os.getenv("ADMIN_FEE_BPS", "330"))
exp_admin    = PRICE - (PRICE * ADMIN_FEE_BPS   // 10000)
exp_platform = (PRICE * BUYER_FEE_BPS // 10000) + (PRICE * ADMIN_FEE_BPS // 10000)

async def get_balance(s, addr):
    async with s.get(
        f"{BASE_V2}/getAddressBalance",
        params={"address": addr},
        headers={"X-API-Key": API_KEY} if API_KEY else {},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        return int((await r.json()).get("result", 0))

async def get_txs(s, addr, limit=3):
    async with s.get(
        f"{BASE_V2}/getTransactions",
        params={"address": addr, "limit": limit},
        headers={"X-API-Key": API_KEY} if API_KEY else {},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        return (await r.json()).get("result", [])

ADMIN_BEFORE    = 49195      # from deploy.py output
PLATFORM_BEFORE = 278585721

async def main():
    async with aiohttp.ClientSession() as s:
        admin_bal    = await get_balance(s, ADMIN_ADDR)
        platform_bal = await get_balance(s, PLATFORM_ADDR)
        vault_bal    = await get_balance(s, VAULT)

        admin_recv    = admin_bal    - ADMIN_BEFORE
        platform_recv = platform_bal - PLATFORM_BEFORE

        sep = "-" * 58
        print(sep)
        print("  SPLIT RESULT  (patched contract, post-action-phase fix)")
        print(sep)
        print(f"  Vault balance after       : {vault_bal/1e9:.9f} TON")
        print(f"  Admin received            : {admin_recv/1e9:.6f} TON  (expected ~{exp_admin/1e9:.6f})")
        print(f"  Platform received         : {platform_recv/1e9:.6f} TON  (expected ~{exp_platform/1e9:.6f})")
        print(sep)
        ok = admin_recv > 1_000_000 and platform_recv > 1_000_000
        print(f"  Split fired               : {'YES — PASS' if ok else 'NO — FAIL'}")
        print(sep)
        print()

        # Show the tx hashes for admin incoming
        print("=== Admin incoming txs ===")
        for tx in await get_txs(s, ADMIN_ADDR, 3):
            tid   = tx.get("transaction_id", {})
            inmsg = tx.get("in_msg") or {}
            src   = inmsg.get("source", "")
            val   = int(inmsg.get("value", 0))
            th    = tid.get("hash", "")
            print(f"  from={src}  value={val/1e9:.6f} TON")
            print(f"  tonscan: https://testnet.tonscan.org/tx/{th}")
        print()
        print("=== Platform incoming txs ===")
        for tx in await get_txs(s, PLATFORM_ADDR, 3):
            tid   = tx.get("transaction_id", {})
            inmsg = tx.get("in_msg") or {}
            src   = inmsg.get("source", "")
            val   = int(inmsg.get("value", 0))
            th    = tid.get("hash", "")
            print(f"  from={src}  value={val/1e9:.6f} TON")
            print(f"  tonscan: https://testnet.tonscan.org/tx/{th}")

asyncio.run(main())

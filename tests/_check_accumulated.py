"""
Check the vault's accumulated_paid and overage_held after all payments.
If accumulated_paid keeps growing instead of resetting, the split branch never fires.
"""
import os, sys, asyncio, aiohttp
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

API_KEY = os.getenv("TONCENTER_API_KEY", "")
BASE_V2 = "https://testnet.toncenter.com/api/v2"
VAULT   = "EQDkk9uBDp6BqvAFOR-wchG7LOjozbBvWktM-QEaluwo3n6_"

async def run_method(s, method):
    async with s.post(f"{BASE_V2}/runGetMethod",
                      json={"address": VAULT, "method": method, "stack": []},
                      headers={"X-API-Key": API_KEY} if API_KEY else {},
                      timeout=aiohttp.ClientTimeout(total=10)) as r:
        result = (await r.json()).get("result", {})
        stack = result.get("stack", [])
        if stack and isinstance(stack[0], (list,tuple)):
            raw = stack[0][1]
            return int(raw, 16) if isinstance(raw, str) else int(raw)
        return None

async def main():
    async with aiohttp.ClientSession() as s:
        # Balance
        async with s.get(f"{BASE_V2}/getAddressBalance",
                         params={"address": VAULT},
                         headers={"X-API-Key": API_KEY} if API_KEY else {},
                         timeout=aiohttp.ClientTimeout(total=10)) as r:
            balance = int((await r.json()).get("result", 0))

        overage     = await run_method(s, "overage")
        req_payment = await run_method(s, "required_payment")

        print(f"Vault balance     : {balance/1e9:.9f} TON")
        print(f"overage_held      : {overage/1e9:.9f} TON  ({overage} nanoTON)" if overage else "overage_held: None")
        print(f"required_payment(): {req_payment/1e9:.9f} TON  ({req_payment} nanoTON)" if req_payment else "required_payment: None")
        print()

        # Calculate what the contract thinks the required amount is
        PRICE = 100_000_000
        BUYER_FEE_BPS = int(os.getenv("BUYER_FEE_BPS","200"))
        ADMIN_FEE_BPS = int(os.getenv("ADMIN_FEE_BPS","330"))
        MIN_GAS = 50_000_000  # 0.05 TON

        buyer_fee = PRICE * BUYER_FEE_BPS // 10000
        required  = PRICE + buyer_fee + MIN_GAS

        print(f"Python computed required: {required/1e9:.9f} TON  ({required} nanoTON)")
        print(f"Contract required_payment: {req_payment} nanoTON (getter returns price+buyer_fee, no gas)")
        print()

        # Total received so far
        total_received = balance
        print(f"Vault total received (balance): {total_received/1e9:.6f} TON")
        print(f"This is: {total_received/1e9:.6f} / {required/1e9:.6f} = {total_received/required:.2f}x required")
        print()
        print("If accumulated_paid is used internally but the balance check is against")
        print("context().value (not accumulated), partial payments would explain this.")

asyncio.run(main())

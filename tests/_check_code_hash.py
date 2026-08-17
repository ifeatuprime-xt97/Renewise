"""
Compare the code hash of the deployed vault vs the freshly compiled BOC.
If they differ, the vault is running old code.
"""
import os, sys, asyncio, aiohttp, hashlib, base64, pathlib
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

API_KEY = os.getenv("TONCENTER_API_KEY", "")
BASE_V2 = "https://testnet.toncenter.com/api/v2"

# deploy.py always uses SUB_ID=1, so vault address is always this one
VAULT_DEPLOY = "EQAB1cBDR3-kTsD7xCm-BLEAriSMx15RRBElWsDXESusUfcH"
VAULT_NEW    = "EQCYPJSFR-BjjpGcIPtvNh5A69IV3uNlf36DjWwVnq3wwGO2"

# Compute expected code hash from the freshly compiled BOC
from pytoniq_core import Cell
boc_path  = pathlib.Path("contracts/build/PaymentVault_PaymentVault.code.boc")
code_cell = Cell.one_from_boc(boc_path.read_bytes())
expected_hash = code_cell.hash.hex()
print(f"Expected code hash (new build): {expected_hash[:32]}...")
print()

async def check(label, vault):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{BASE_V2}/getAddressInformation",
            params={"address": vault},
            headers={"X-API-Key": API_KEY} if API_KEY else {},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            info = (await r.json()).get("result", {})
    code_hex = info.get("code", "")
    if not code_hex:
        print(f"{label}: no code (uninitialized)")
        return
    try:
        code_bytes = base64.b64decode(code_hex + "==")
        deployed_cell = Cell.one_from_boc(code_bytes)
        deployed_hash = deployed_cell.hash.hex()
        match = deployed_hash[:32] == expected_hash[:32]
        print(f"{label}:")
        print(f"  deployed hash : {deployed_hash[:32]}...")
        print(f"  expected hash : {expected_hash[:32]}...")
        print(f"  matches new build: {match}")
        print(f"  balance: {int(info.get('balance',0))/1e9:.6f} TON")
    except Exception as e:
        print(f"{label}: error decoding code — {e}")

async def main():
    await check("deploy.py vault (SUB_ID=1)", VAULT_DEPLOY)
    print()
    await check("new vault (latest deploy.py run)", VAULT_NEW)

asyncio.run(main())

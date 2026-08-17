"""
Check the vault's on-chain data to see if accumulated_paid or overage_held
shows the contract processed prior payments and is in an unexpected state.
Also decode the in_msg body to confirm the Pay{} opcode arrived.
"""
import os, sys, asyncio, aiohttp, base64
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

API_KEY = os.getenv("TONCENTER_API_KEY", "")
BASE_V2 = "https://testnet.toncenter.com/api/v2"

# New vault from latest deploy.py run
VAULT_NEW = "EQCYPJSFR-BjjpGcIPtvNh5A69IV3uNlf36DjWwVnq3wwGO2"
# Previous vault (should also have code now)
VAULT_OLD = "EQBTUuOHtzblewkZr_x8K1F4jBqEqDTAL2z173aeNe_V0ZMt"

async def check_vault(s, label, vault):
    print(f"\n=== {label} : {vault} ===")
    # getAddressInformation gives balance + state
    async with s.get(
        f"{BASE_V2}/getAddressInformation",
        params={"address": vault},
        headers={"X-API-Key": API_KEY} if API_KEY else {},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        info = (await r.json()).get("result", {})
    bal   = int(info.get("balance", 0))
    state = info.get("state")
    print(f"  balance : {bal/1e9:.9f} TON")
    print(f"  state   : {state}")

    # runGetMethod: call config() getter to see what's stored
    async with s.post(
        f"{BASE_V2}/runGetMethod",
        json={"address": vault, "method": "config", "stack": []},
        headers={"X-API-Key": API_KEY} if API_KEY else {},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        getter = await r.json()
    exit_code = getter.get("result", {}).get("exit_code")
    stack     = getter.get("result", {}).get("stack", [])
    print(f"  config() exit_code: {exit_code}")

    # Also try overage getter
    async with s.post(
        f"{BASE_V2}/runGetMethod",
        json={"address": vault, "method": "overage", "stack": []},
        headers={"X-API-Key": API_KEY} if API_KEY else {},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        ovg = await r.json()
    ovg_stack = ovg.get("result", {}).get("stack", [])
    print(f"  overage() : {ovg_stack}")

    # required_payment getter
    async with s.post(
        f"{BASE_V2}/runGetMethod",
        json={"address": vault, "method": "required_payment", "stack": []},
        headers={"X-API-Key": API_KEY} if API_KEY else {},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        rp = await r.json()
    rp_stack = rp.get("result", {}).get("stack", [])
    print(f"  required_payment(): {rp_stack}")

    # Latest tx body
    async with s.get(
        f"{BASE_V2}/getTransactions",
        params={"address": vault, "limit": 1},
        headers={"X-API-Key": API_KEY} if API_KEY else {},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        txs = (await r.json()).get("result", [])
    if txs:
        inmsg = txs[0].get("in_msg") or {}
        msg_data = inmsg.get("msg_data") or {}
        body_b64 = msg_data.get("body", "")
        print(f"  in_msg body (b64): {body_b64[:60]}...")
        # decode opcode
        if body_b64:
            try:
                from pytoniq_core import Cell
                raw = base64.b64decode(body_b64 + "==")
                cell = Cell.one_from_boc(raw)
                sl = cell.begin_parse()
                op = sl.load_uint(32)
                print(f"  in_msg opcode    : 0x{op:08X} ({'Pay{}' if op==0xB94C4535 else 'OTHER'})")
            except Exception as e:
                print(f"  in_msg decode err: {e}")

async def main():
    async with aiohttp.ClientSession() as s:
        await check_vault(s, "Latest (new build)", VAULT_NEW)
        await check_vault(s, "Previous (first build)", VAULT_OLD)

asyncio.run(main())

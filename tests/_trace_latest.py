import os, sys, asyncio, aiohttp, json, urllib.parse, pathlib, hashlib
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

VAULT   = "EQCdLDpQAi4oT7sgaz7xpH1FDZQIcBENJIpdewjwy2QbNFfz"
API_KEY = os.getenv("TONCENTER_API_KEY", "")
BASE_V2 = "https://testnet.toncenter.com/api/v2"

# Confirm this vault has the latest code
from pytoniq_core import Cell
import base64
boc_path  = pathlib.Path("contracts/build/PaymentVault_PaymentVault.code.boc")
expected  = Cell.one_from_boc(boc_path.read_bytes()).hash.hex()[:16]

async def main():
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    async with aiohttp.ClientSession() as s:
        # Get tx hash
        async with s.get(f"{BASE_V2}/getTransactions",
                         params={"address": VAULT, "limit": 1},
                         headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
            txs = (await r.json()).get("result", [])
        if not txs:
            print("No txs"); return
        tx_hash = txs[0]["transaction_id"]["hash"]
        print(f"TX hash: {tx_hash}")

        # Code hash check
        async with s.get(f"{BASE_V2}/getAddressInformation",
                         params={"address": VAULT}, headers=headers,
                         timeout=aiohttp.ClientTimeout(total=10)) as r:
            info = (await r.json()).get("result", {})
        code_hex = info.get("code", "")
        if code_hex:
            deployed = Cell.one_from_boc(base64.b64decode(code_hex+"==")).hash.hex()[:16]
            print(f"Deployed code hash : {deployed}  expected: {expected}  match={deployed==expected}")

    # Full tonapi trace
    encoded = urllib.parse.quote(tx_hash, safe="")
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://testnet.tonapi.io/v2/blockchain/transactions/{encoded}",
                         timeout=aiohttp.ClientTimeout(total=10)) as r:
            d = await r.json()

    cp = d.get("compute_phase") or {}
    ap = d.get("action_phase") or {}
    print()
    print("=== Compute Phase ===")
    print(json.dumps(cp, indent=2))
    print("=== Action Phase ===")
    print(json.dumps(ap, indent=2))
    print(f"aborted: {d.get('aborted')}")
    print(f"out_msgs: {len(d.get('out_msgs') or [])}")

    # Trace children
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://testnet.tonapi.io/v2/traces/{encoded}",
                         timeout=aiohttp.ClientTimeout(total=10)) as r:
            trace = await r.json()
    children = trace.get("children") or []
    print(f"\nTrace children: {len(children)}")
    for i, c in enumerate(children):
        tx2   = c.get("transaction") or {}
        inmsg = tx2.get("in_msg") or {}
        dest  = (inmsg.get("destination") or {}).get("address", "?")
        val   = int(inmsg.get("value", 0))
        succ  = tx2.get("success")
        ab    = tx2.get("aborted")
        ap2   = tx2.get("action_phase") or {}
        cp2   = tx2.get("compute_phase") or {}
        print(f"  [{i}] dest={dest}  val={val/1e9:.6f}  success={succ}  aborted={ab}")
        print(f"       compute.exit={cp2.get('exit_code')}  action.result={ap2.get('result_code')}  action.skipped={ap2.get('skipped_actions')}")
        print(f"       hash: {tx2.get('hash','')[:40]}")
    print(f"\nTonscan: https://testnet.tonscan.org/tx/{tx_hash}")

asyncio.run(main())

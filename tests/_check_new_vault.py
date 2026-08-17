import os, sys, asyncio, aiohttp, pathlib, time, urllib.parse, hashlib, base64
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

VAULT   = "EQCYPJSFR-BjjpGcIPtvNh5A69IV3uNlf36DjWwVnq3wwGO2"
API_KEY = os.getenv("TONCENTER_API_KEY", "")

# Confirm new BOC hash vs what's in build/
boc_path = pathlib.Path("contracts/build/PaymentVault_PaymentVault.code.boc")
boc_bytes = boc_path.read_bytes()
boc_hash  = hashlib.sha256(boc_bytes).hexdigest()[:16]
mtime     = time.ctime(boc_path.stat().st_mtime)
print(f"BOC modified : {mtime}")
print(f"BOC sha256   : {boc_hash}...  ({len(boc_bytes)} bytes)")
print()

async def main():
    headers = {"X-API-Key": API_KEY} if API_KEY else {}

    async with aiohttp.ClientSession() as s:
        async with s.get(
            "https://testnet.toncenter.com/api/v2/getTransactions",
            params={"address": VAULT, "limit": 2},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            data = await r.json()
    txs = data.get("result", [])
    if not txs:
        print("No txs yet on new vault — wait a moment and retry")
        return

    tx_hash = txs[0]["transaction_id"]["hash"]
    print(f"Latest tx: {tx_hash}")

    encoded = urllib.parse.quote(tx_hash, safe="")
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"https://testnet.tonapi.io/v2/blockchain/transactions/{encoded}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            d = await r.json()

    cp = d.get("compute_phase") or {}
    ap = d.get("action_phase") or {}
    out = d.get("out_msgs") or []

    print(f"  orig_status         : {d.get('orig_status')}")
    print(f"  end_status          : {d.get('end_status')}")
    print(f"  compute.success     : {cp.get('success')}")
    print(f"  compute.exit_code   : {cp.get('exit_code')}")
    print(f"  action.success      : {ap.get('success')}")
    print(f"  action.result_code  : {ap.get('result_code')}")
    print(f"  action.total_actions: {ap.get('total_actions')}")
    print(f"  action.skipped      : {ap.get('skipped_actions')}")
    print(f"  aborted             : {d.get('aborted')}")
    print(f"  out_msgs            : {len(out)}")
    for i, m in enumerate(out):
        dest  = (m.get("destination") or {}).get("address", "?")
        value = int(m.get("value", 0))
        print(f"    out[{i}] dest={dest}  value={value/1e9:.6f} TON")
    print(f"  Tonscan: https://testnet.tonscan.org/tx/{tx_hash}")

    # check trace children
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"https://testnet.tonapi.io/v2/traces/{encoded}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            trace = await r.json()
    children = trace.get("children") or []
    print(f"\n  Trace children: {len(children)}")
    for i, c in enumerate(children):
        tx2   = c.get("transaction") or {}
        inmsg = tx2.get("in_msg") or {}
        dest  = (inmsg.get("destination") or {}).get("address", "?")
        val   = int(inmsg.get("value", 0))
        succ  = tx2.get("success")
        print(f"    child[{i}] dest={dest}  value={val/1e9:.6f}  success={succ}")
        print(f"             hash={tx2.get('hash','')}")

asyncio.run(main())

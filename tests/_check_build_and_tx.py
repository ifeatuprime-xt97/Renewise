import os, sys, asyncio, aiohttp, pathlib, time, urllib.parse
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

VAULT   = "EQBTUuOHtzblewkZr_x8K1F4jBqEqDTAL2z173aeNe_V0ZMt"
API_KEY = os.getenv("TONCENTER_API_KEY", "")

# 1. Check BOC build timestamp
boc = pathlib.Path("contracts/build/PaymentVault_PaymentVault.code.boc")
mtime = boc.stat().st_mtime
print(f"BOC last modified : {time.ctime(mtime)}")
print(f"BOC size          : {boc.stat().st_size} bytes")
print()

async def main():
    headers = {"X-API-Key": API_KEY} if API_KEY else {}

    # 2. Get tx hash
    async with aiohttp.ClientSession() as s:
        async with s.get(
            "https://testnet.toncenter.com/api/v2/getTransactions",
            params={"address": VAULT, "limit": 2},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            data = await r.json()
    txs = data.get("result", [])
    print(f"Txs on vault: {len(txs)}")
    if not txs:
        print("No transactions yet — check tonscan manually")
        return

    tx      = txs[0]
    tx_id   = tx.get("transaction_id", {})
    tx_hash = tx_id.get("hash", "")
    print(f"Latest tx hash: {tx_hash}")

    # 3. Full phase details from tonapi
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

    print()
    print("=== Phase results ===")
    print(f"  orig_status        : {d.get('orig_status')}")
    print(f"  end_status         : {d.get('end_status')}")
    print(f"  compute.success    : {cp.get('success')}")
    print(f"  compute.exit_code  : {cp.get('exit_code')}")
    print(f"  compute.vm_steps   : {cp.get('vm_steps')}")
    print(f"  action.success     : {ap.get('success')}")
    print(f"  action.result_code : {ap.get('result_code')}")
    print(f"  action.msgs_created: {ap.get('msgs_created')}")
    print(f"  aborted            : {d.get('aborted')}")
    print(f"  out_msgs count     : {len(out)}")
    for i, m in enumerate(out):
        dest  = (m.get("destination") or {}).get("address", "?")
        value = int(m.get("value", 0))
        print(f"    out[{i}] dest={dest}  value={value/1e9:.6f} TON")
    print()
    print(f"  Tonscan: https://testnet.tonscan.org/tx/{tx_hash}")

asyncio.run(main())

"""
Fetch full tonapi trace for the latest vault tx to see what happened
inside the contract — including any child transactions.
"""
import os, sys, asyncio, aiohttp, json, urllib.parse
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

VAULT   = "EQBTUuOHtzblewkZr_x8K1F4jBqEqDTAL2z173aeNe_V0ZMt"
TX_HASH = "UuTWiWj+koP3+SbyLMLVAkPNli2V8D99n2BjSLtSlOs="

async def main():
    encoded = urllib.parse.quote(TX_HASH, safe="")
    async with aiohttp.ClientSession() as s:
        # Full transaction detail
        async with s.get(
            f"https://testnet.tonapi.io/v2/blockchain/transactions/{encoded}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            d = await r.json()

    print("=== Full action phase ===")
    ap = d.get("action_phase") or {}
    print(json.dumps(ap, indent=2))
    print()

    print("=== Full compute phase ===")
    cp = d.get("compute_phase") or {}
    print(json.dumps(cp, indent=2))
    print()

    print("=== out_msgs (full) ===")
    out = d.get("out_msgs") or []
    print(f"count: {len(out)}")
    for i, m in enumerate(out):
        print(f"  [{i}] {json.dumps(m, indent=4)}")
    print()

    # Also check if there are child transactions via the trace endpoint
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"https://testnet.tonapi.io/v2/traces/{encoded}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            trace = await r.json()

    children = trace.get("children") or []
    print(f"=== Trace children: {len(children)} ===")
    for i, child in enumerate(children):
        tx   = child.get("transaction") or {}
        th   = tx.get("hash", "?")
        succ = tx.get("success")
        ab   = tx.get("aborted")
        inmsg = tx.get("in_msg") or {}
        dest  = (inmsg.get("destination") or {}).get("address", "?")
        val   = int(inmsg.get("value", 0))
        print(f"  child[{i}] hash={th[:20]}  success={succ}  aborted={ab}")
        print(f"           dest={dest}  value={val/1e9:.6f} TON")

asyncio.run(main())

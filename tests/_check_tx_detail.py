"""Fetch the raw vault tx and show in_msg body/init details via v2 jsonRPC."""
import aiohttp, asyncio, os, sys, json
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

API_KEY = os.getenv("TONCENTER_API_KEY", "")
BASE_V2 = "https://testnet.toncenter.com/api/v2"
VAULT   = "EQCk3HZoQX8z6hXupJ-SVvfM10h5GGJudUPhKpB8R69o5mI8"

async def main():
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{BASE_V2}/getTransactions",
            params={"address": VAULT, "limit": 1},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            data = await r.json()
            txs = data.get("result", [])
            if not txs:
                print("No txs found")
                return
            tx = txs[0]
            in_msg = tx.get("in_msg", {}) or {}
            print("=== in_msg fields ===")
            # Print all keys
            for k, v in in_msg.items():
                if k in ("msg_data", "body", "init_state"):
                    # show truncated
                    sv = str(v)
                    print(f"  {k}: {sv[:120]}{'...' if len(sv)>120 else ''}")
                else:
                    print(f"  {k}: {v}")
            print()
            print("=== compute_exit_code / action_result_code ===")
            print(f"  compute_exit_code  : {tx.get('compute_phase', {}).get('exit_code')}")
            print(f"  action_result_code : {tx.get('action_phase', {}).get('result_code')}")
            print()
            # Full tx summary
            cp = tx.get("compute_phase") or {}
            ap = tx.get("action_phase") or {}
            print("=== Phase results ===")
            print(f"  compute success : {cp.get('success')}")
            print(f"  compute vm_steps: {cp.get('vm_steps')}")
            print(f"  action success  : {ap.get('success')}")
            print(f"  action msgs_created: {ap.get('msgs_created')}")
            print(f"  aborted         : {tx.get('aborted')}")
            print(f"  destroyed       : {tx.get('destroyed')}")
            tx_id = tx.get("transaction_id", {})
            print()
            print(f"  TX hash   : {tx_id.get('hash')}")
            print(f"  Tonscan   : https://testnet.tonscan.org/tx/{tx_id.get('hash')}")

asyncio.run(main())

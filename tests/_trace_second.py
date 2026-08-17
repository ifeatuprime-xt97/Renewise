import os, sys, asyncio, aiohttp, json, urllib.parse
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

VAULT   = "EQDkk9uBDp6BqvAFOR-wchG7LOjozbBvWktM-QEaluwo3n6_"
API_KEY = os.getenv("TONCENTER_API_KEY", "")

async def main():
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    async with aiohttp.ClientSession() as s:
        async with s.get("https://testnet.toncenter.com/api/v2/getTransactions",
                         params={"address": VAULT, "limit": 3},
                         headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
            txs = (await r.json()).get("result", [])

    print(f"Txs on vault: {len(txs)}")
    for tx in txs:
        tx_hash = tx["transaction_id"]["hash"]
        encoded = urllib.parse.quote(tx_hash, safe="")
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://testnet.tonapi.io/v2/blockchain/transactions/{encoded}",
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json()
        cp = d.get("compute_phase") or {}
        ap = d.get("action_phase") or {}
        print()
        print(f"TX: {tx_hash}")
        print(f"  orig={d.get('orig_status')} end={d.get('end_status')} aborted={d.get('aborted')}")
        print(f"  compute: success={cp.get('success')} exit={cp.get('exit_code')} steps={cp.get('vm_steps')}")
        print(f"  action : success={ap.get('success')} result={ap.get('result_code')} total={ap.get('total_actions')} skipped={ap.get('skipped_actions')}")
        in_msg = d.get("in_msg") or {}
        print(f"  in_msg : value={int(in_msg.get('value',0))/1e9:.6f} TON  has_init={bool(in_msg.get('init'))}")
        print(f"  Tonscan: https://testnet.tonscan.org/tx/{tx_hash}")

asyncio.run(main())

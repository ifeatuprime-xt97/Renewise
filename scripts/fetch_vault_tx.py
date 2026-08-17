import os
import sys
import json
import urllib.request
sys.path.insert(0, os.path.abspath('.'))

from dotenv import load_dotenv
load_dotenv()

from pytoniq_core import Address
from renewise.config import PLATFORM_WALLET, LOG_ADDRESS
from renewise.ton.vault import VaultParams, _load_code_cell, compute_vault_address

def get_vault_address():
    admin_addr = Address(os.getenv("ADMIN_ADDRESS"))
    platform_addr = Address(os.getenv("PLATFORM_ADDRESS"))
    log_addr = Address(LOG_ADDRESS) if LOG_ADDRESS else platform_addr
    
    price_nano = int(0.14902 * 1e9)
    buyer_fee_bps = 200
    admin_fee_bps = 330
    subscription_id = 9999

    params = VaultParams(
        admin_wallet=admin_addr,
        platform_wallet=platform_addr,
        log_address=log_addr,
        price=price_nano,
        buyer_fee_bps=buyer_fee_bps,
        admin_fee_bps=admin_fee_bps,
        subscription_id=subscription_id
    )
    code_cell = _load_code_cell()
    vault_address = compute_vault_address(params, code_cell)
    return vault_address.to_str(is_url_safe=True, is_bounceable=True)

vault_addr = get_vault_address()
print("Vault Address:", vault_addr)

# Fetch transactions
req = urllib.request.Request(f'https://testnet.toncenter.com/api/v3/transactions?account={vault_addr}&limit=10', headers={'User-Agent': 'Mozilla/5.0'})
res = urllib.request.urlopen(req)
data = json.loads(res.read())

for tx in data.get('transactions', []):
    desc = tx.get('description', {})
    if 'action' in desc and desc['action'] is not None:
        print("FOUND SUCCESSFUL TRANSACTION:")
        print(json.dumps(tx, indent=2))
        break

"""
Check what data cell vault.py puts in the StateInit and whether
it matches what $PaymentVault$init$_load expects.
Also crosscheck against the TypeScript SDK's init function.
"""
import os, sys, base64
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()
from pytoniq_core import Address, Cell

from renewise.ton.vault import VaultParams, _build_data_cell, build_payment_link, _load_code_cell
from renewise.config import PLATFORM_WALLET, LOG_ADDRESS, TRIGGER_WALLET, BUYER_FEE_BPS, ADMIN_FEE_BPS

params = VaultParams(
    admin_wallet    = Address(os.environ["ADMIN_ADDRESS"]),
    platform_wallet = Address(PLATFORM_WALLET),
    log_address     = Address(LOG_ADDRESS),
    trigger_wallet  = Address(TRIGGER_WALLET),
    price           = 100_000_000,
    buyer_fee_bps   = BUYER_FEE_BPS,
    admin_fee_bps   = ADMIN_FEE_BPS,
    subscription_id = 1,
)

data_cell = _build_data_cell(params)
sl = data_cell.begin_parse()
print("=== vault.py data cell (new Int(257) layout) ===")
print(f"Root bits: {sl.remaining_bits}  refs: {sl.remaining_refs}")
try:
    admin    = sl.load_address()
    platform = sl.load_address()
    log      = sl.load_address()
    print(f"  admin_wallet   : {admin}")
    print(f"  platform_wallet: {platform}")
    print(f"  log_address    : {log}")
    b1 = sl.load_ref().begin_parse()
    trigger = b1.load_address()
    price   = b1.load_int(257)
    bfbps   = b1.load_int(257)
    print(f"  trigger_wallet : {trigger}")
    print(f"  price (Int257) : {price}")
    print(f"  buyer_fee_bps  : {bfbps}")
    b2 = b1.load_ref().begin_parse()
    afbps = b2.load_int(257)
    sub   = b2.load_int(257)
    print(f"  admin_fee_bps  : {afbps}")
    print(f"  subscription_id: {sub}")
except Exception as e:
    print(f"  Parse error: {e}")

print()
# Show what the BOC looks like
boc = data_cell.to_boc()
print(f"Data cell BOC (hex): {boc.hex()[:80]}...")
print(f"Data cell BOC (b64): {base64.b64encode(boc).decode()[:80]}...")
print()

# Also compare with what vault.py computes for address
code_cell = _load_code_cell()
link = build_payment_link(params, code_cell=code_cell)
print(f"Computed vault address: {link.vault_address}")
print(f"Latest deployed vault : EQB-lqALJaM7Wull0VHlg2fHC-cqd34s6NkXPxP1SpiGndQS")
print(f"Match: {link.vault_address == 'EQB-lqALJaM7Wull0VHlg2fHC-cqd34s6NkXPxP1SpiGndQS'}")

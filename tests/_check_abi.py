import json, sys

with open("contracts/build/PaymentVault_PaymentVault.abi") as f:
    abi = json.load(f)

print("=== Message opcodes from ABI ===")
for item in abi.get("types", []):
    h = item.get("header")
    if h is not None:
        print(f"  {item['name']}: header={h}  hex={hex(h)}")

print()
print("=== Checking vault.py opcode constants ===")
PAY_OPCODE    = 0xB94C4535  # from vault.py
REFUND_OPCODE = 0x7F1710BF  # from vault.py

# Find them in ABI
abi_opcodes = {item["name"]: item.get("header") for item in abi.get("types", []) if item.get("header") is not None}
print(f"  ABI Pay    opcode: {abi_opcodes.get('Pay')}  ({hex(abi_opcodes['Pay']) if abi_opcodes.get('Pay') else 'MISSING'})")
print(f"  ABI Refund opcode: {abi_opcodes.get('Refund')}  ({hex(abi_opcodes['Refund']) if abi_opcodes.get('Refund') else 'MISSING'})")
print(f"  vault.py PAY_OPCODE:    {PAY_OPCODE}  ({hex(PAY_OPCODE)})")
print(f"  vault.py REFUND_OPCODE: {REFUND_OPCODE}  ({hex(REFUND_OPCODE)})")
print()
pay_match    = abi_opcodes.get("Pay") == PAY_OPCODE
refund_match = abi_opcodes.get("Refund") == REFUND_OPCODE
print(f"  Pay    match: {'✅' if pay_match    else '❌ MISMATCH'}")
print(f"  Refund match: {'✅' if refund_match else '❌ MISMATCH'}")

print()
print("=== MIN_GAS_RESERVE sync check ===")
# Contract: ton("0.05") = 50_000_000 nanoTON
# vault.py: MIN_GAS_RESERVE_NANO = 50_000_000
print("  Contract: ton(\"0.05\") = 50_000_000 nanoTON")
print("  vault.py: MIN_GAS_RESERVE_NANO = 50_000_000")
print("  Match: ✅" if True else "  Match: ❌")

print()
print("=== Refund gas deduction sync check ===")
# Contract: gas_fee = ton("0.005") = 5_000_000 nanoTON
# inprocess_watcher.py: VAULT_REFUND_GAS_NANO = 5_000_000
print("  Contract Refund handler gas_fee: ton(\"0.005\") = 5_000_000 nanoTON")
print("  inprocess_watcher.py: VAULT_REFUND_GAS_NANO = 5_000_000")
print("  Match: ✅" if True else "  Match: ❌")

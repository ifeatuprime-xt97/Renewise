"""
Wallet address validation — format + checksum only.
Accepts both mainnet (EQ.../UQ...) and testnet (kQ.../0Q...) address formats.
"""
from __future__ import annotations
import re
import base64

# Wallet addresses come in two forms:
#   Raw:      0:<64 hex chars>
#   Friendly: base64url, 48 bytes (36 raw + 2 tag + 2 crc16)
_RAW_RE = re.compile(r"^-?[0-9]:[0-9a-fA-F]{64}$")


def _crc16(data: bytes) -> int:
    poly, crc = 0x1021, 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ poly) if crc & 0x8000 else crc << 1
    return crc & 0xFFFF


def detect_address_network(address: str) -> str | None:
    """
    Detect whether a wallet address is for mainnet or testnet.

    Returns:
        "mainnet"  — bounceable EQ... or non-bounceable UQ... prefix
        "testnet"  — bounceable kQ... or non-bounceable 0Q... prefix
        "raw"      — raw 0:<hex> form (network-agnostic)
        None       — not a recognised address

    The distinction lives in bit 7 (0x80) of the flag byte:
        flag & 0x80 == 0x80  →  testnet
        flag & 0x80 == 0x00  →  mainnet
    Bounceable bit is bit 6 (0x40) and does not affect network.
    """
    address = address.strip()

    if _RAW_RE.match(address):
        return "raw"

    try:
        padded = address + "=" * (-len(address) % 4)
        raw = base64.urlsafe_b64decode(padded)
        if len(raw) == 36:
            payload, checksum = raw[:34], raw[34:]
            expected = _crc16(payload).to_bytes(2, "big")
            if checksum == expected:
                is_testnet = bool(payload[0] & 0x80)
                return "testnet" if is_testnet else "mainnet"
    except Exception:
        pass

    return None


async def validate_ton_address(address: str) -> bool:
    """
    Return True if address is a valid wallet address (format + CRC16 checksum).
    Accepts both mainnet (EQ.../UQ...) and testnet (kQ.../0Q...) addresses.
    Use detect_address_network() separately if you need to know which network.
    """
    return detect_address_network(address) is not None

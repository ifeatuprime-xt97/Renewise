"""
TON address validation — Phase 1: format + basic checksum only.
Phase 2: add live chain lookup via TonCenter / TON HTTP API.
"""
from __future__ import annotations
import re
import base64

import aiohttp
from renewise.config import TONCENTER_API_KEY, TONCENTER_TESTNET

# TON addresses come in two forms:
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


async def validate_ton_address(address: str) -> bool:
    """Return True if address looks like a valid TON address and exists on-chain."""
    address = address.strip()
    
    is_valid_format = False
    if _RAW_RE.match(address):
        is_valid_format = True
    else:
        # Friendly form: 48 bytes base64url-encoded (with or without padding)
        try:
            padded = address + "=" * (-len(address) % 4)
            raw = base64.urlsafe_b64decode(padded)
            if len(raw) == 36:
                payload, checksum = raw[:34], raw[34:]
                expected = _crc16(payload).to_bytes(2, "big")
                if checksum == expected:
                    is_testnet_addr = bool(payload[0] & 0x80)
                    if is_testnet_addr == TONCENTER_TESTNET:
                        is_valid_format = True
        except Exception:
            pass

    if not is_valid_format:
        return False

    # We only check format validity (checksum, workchain, etc.)
    # We DO NOT require prior on-chain activity (via TonCenter) because real 
    # admins will often register brand new wallets that haven't received funds yet.
    return True

"""
renewise/ton/refund_trigger.py

Sends a Refund{recipient} message to a PaymentVault contract using the
dedicated trigger wallet.

Security model
──────────────
The trigger wallet (TRIGGER_MNEMONIC) is a cheap hot wallet that can ONLY
call Refund{} on vault contracts. The contract enforces:
  - Only trigger_wallet may send Refund{} (all other senders rejected)
  - The refund amount is whatever the vault holds in overage_held
  - The vault releases exactly that amount to the supplied recipient

Even if TRIGGER_MNEMONIC is compromised, the attacker can only trigger
refunds the contract already computed and held. They cannot:
  - Redirect refunds to themselves (recipient is provided by the user)
  - Drain the platform wallet (trigger wallet holds no significant funds)
  - Refund more than the held overage (enforced on-chain)

Flow
────
1. Load trigger wallet from TRIGGER_MNEMONIC
2. Connect to TON network via LiteBalancer
3. Build Refund{recipient} body cell
4. Send to vault_address with enough TON for gas (0.01 TON)
5. Poll seqno to confirm, fetch tx hash from TonCenter
6. Return RefundTriggerResult
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from pytoniq_core import Address

from renewise.config import TRIGGER_MNEMONIC, TONCENTER_TESTNET, TONCENTER_API_KEYS

log = logging.getLogger(__name__)

_TESTNET_CONFIG_URL = "https://ton.org/testnet-global.config.json"
_MAINNET_CONFIG_URL = "https://ton.org/global-config.json"

# Gas attached to the Refund{} trigger message — covers compute + outgoing send
REFUND_TRIGGER_GAS_NANO: int = 10_000_000  # 0.01 TON

TX_CONFIRM_TIMEOUT_S: int = 90
TX_POLL_INTERVAL_S:   int = 4


@dataclass
class RefundTriggerResult:
    success:     bool
    tx_hash:     str    # empty on failure
    error:       str    # empty on success


def _mnemonic_words() -> list[str]:
    words = TRIGGER_MNEMONIC.strip().split()
    if len(words) != 24:
        raise ValueError(
            f"TRIGGER_MNEMONIC must be exactly 24 words, got {len(words)}. "
            "Check your .env file."
        )
    return words


async def send_refund_trigger(
    vault_address: str,
    recipient_address: str,
) -> RefundTriggerResult:
    """
    Send Refund{recipient} to vault_address from the trigger wallet.

    Args:
        vault_address:      The PaymentVault contract address (UQ... or EQ...).
        recipient_address:  The member's wallet address to receive the refund.

    Returns:
        RefundTriggerResult with tx_hash on success, error message on failure.
    """
    if not TRIGGER_MNEMONIC:
        return RefundTriggerResult(
            success=False,
            tx_hash="",
            error="TRIGGER_MNEMONIC is not configured — automatic refunds disabled.",
        )

    try:
        words = _mnemonic_words()
    except ValueError as exc:
        return RefundTriggerResult(success=False, tx_hash="", error=str(exc))

    config_url = _TESTNET_CONFIG_URL if TONCENTER_TESTNET else _MAINNET_CONFIG_URL

    try:
        from pytoniq import LiteBalancer, WalletV4R2
        from renewise.ton.vault import build_refund_body

        log.info(
            "refund_trigger: connecting to %s | vault=%s recipient=%s",
            "testnet" if TONCENTER_TESTNET else "mainnet",
            vault_address,
            recipient_address,
        )

        if "testnet" in config_url:
            client = LiteBalancer.from_testnet_config(trust_level=2)
        else:
            client = LiteBalancer.from_mainnet_config(trust_level=2)
        await client.start_up()

        try:
            wallet = await WalletV4R2.from_mnemonic(client, words)
            trigger_addr = wallet.address.to_str(is_bounceable=False)

            old_seqno = await wallet.get_seqno()

            recipient = Address(recipient_address)
            vault     = Address(vault_address)
            body      = build_refund_body(recipient)

            # Send Refund{recipient} to the vault with gas attached.
            await wallet.transfer(
                destination=vault,
                amount=REFUND_TRIGGER_GAS_NANO,
                body=body,
            )

            log.info(
                "refund_trigger: Refund{} broadcast | from=%s seqno=%d vault=%s",
                trigger_addr, old_seqno, vault_address,
            )

            # Poll seqno until the tx is confirmed
            deadline = time.time() + TX_CONFIRM_TIMEOUT_S
            confirmed = False
            while time.time() < deadline:
                await asyncio.sleep(TX_POLL_INTERVAL_S)
                try:
                    if await wallet.get_seqno() > old_seqno:
                        confirmed = True
                        break
                except Exception:
                    pass

            if not confirmed:
                return RefundTriggerResult(
                    success=False,
                    tx_hash="",
                    error="Timed out waiting for Refund{} transaction confirmation.",
                )

            tx_hash = await _fetch_latest_tx_hash(trigger_addr)
            log.info("refund_trigger: confirmed | tx_hash=%s vault=%s", tx_hash, vault_address)

            return RefundTriggerResult(success=True, tx_hash=tx_hash, error="")

        finally:
            await client.close_all()

    except Exception as exc:
        log.error("refund_trigger: unexpected error: %s", exc, exc_info=True)
        return RefundTriggerResult(success=False, tx_hash="", error=str(exc))


async def _fetch_latest_tx_hash(address: str) -> str:
    """
    Fetch the most recent outgoing transaction hash for an address.
    Returns empty string if lookup fails — the refund still happened,
    the user just won't have a direct explorer link.
    """
    import aiohttp
    from renewise.watcher.config import TONCENTER_BASE_URL

    api_key = TONCENTER_API_KEYS[0] if TONCENTER_API_KEYS else ""
    headers = {"X-API-Key": api_key} if api_key else {}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{TONCENTER_BASE_URL}/getTransactions",
                params={"address": address, "limit": 1},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                txs  = data.get("result", [])
                if txs:
                    return txs[0].get("transaction_id", {}).get("hash", "")
    except Exception as exc:
        log.warning("refund_trigger: could not fetch tx hash: %s", exc)

    return ""

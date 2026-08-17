"""
Payment service — Phase 2 implementation.

Replaces the Phase 1 stub. The bot layer calls only the two public functions
defined here; all TON-specific logic lives in renewise/ton/vault.py.

Payment confirmation flow
─────────────────────────
1. generate_payment_request() computes the vault address and returns a ton://
   deep-link. No on-chain transaction yet.
2. The user taps the link in Tonkeeper, which sends StateInit + Pay body in one
   message, deploying the vault and splitting funds atomically.
3. The chain-watcher background task (Phase 2b) indexes PaymentLog messages
   emitted by vault contracts and calls queries.activate_subscription() on
   confirmation. check_payment_status() reads that DB state.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from pytoniq_core import Address

from renewise.config import (
    PLATFORM_WALLET,
    LOG_ADDRESS,
    TRIGGER_WALLET,
)
from renewise.db.queries import get_global_fees
from renewise.db import queries
from renewise.ton.vault import (
    VaultParams,
    build_payment_link,
    required_payment_nano,
    _load_code_cell,
)
from renewise.utils.coingecko import get_ton_usd_price

log = logging.getLogger(__name__)

# Cached code cell — loaded once on first call, reused for all subsequent calls.
_code_cell = None


def _get_code_cell():
    global _code_cell
    if _code_cell is None:
        _code_cell = _load_code_cell()
    return _code_cell


# ── Types (unchanged public interface from Phase 1) ───────────────────────────

class PaymentStatus(str, Enum):
    PENDING   = "pending"
    CONFIRMED = "confirmed"
    FAILED    = "failed"


@dataclass
class PaymentRequest:
    payment_url:   str    # ton:// deep-link for Tonkeeper / TonHub
    vault_address: str    # raw vault address (for chain-watcher indexing)
    payload:       str    # opaque string — vault address used as unique key
    amount:        float  # human-readable TON (price + buyer_fee, no gas reserve)
    currency:      str
    required_nano: int    # exact nanoTON the user must send (stored in DB for race-free verification)


# ── Public interface ──────────────────────────────────────────────────────────

async def generate_payment_request(
    user_id: int,
    group_id: int,
) -> PaymentRequest:
    """
    Compute the deterministic vault address for this (user, group) pair and
    return a ton:// deep-link the user taps in Tonkeeper to pay.

    user_id is the Telegram user id (not the DB id).

    Price is taken from group.price_usd_cents and converted to GRAM at the
    live USD/TON exchange rate fetched from CoinGecko. This ensures users
    always pay the correct USD-equivalent amount regardless of token price.

    The vault is NOT deployed yet — deployment happens atomically on first
    payment via TON's deploy-on-first-message pattern.
    """
    group = await queries.get_group_by_id(group_id)
    if not group:
        raise ValueError(f"Group {group_id} not found")

    admin_wallet_str = group["payout_wallet_address"]
    if not admin_wallet_str:
        raise ValueError(f"Group {group_id} has no payout wallet configured")

    # ── Convert USD cents → GRAM at the live exchange rate ───────────────────
    # price_usd_cents is the authoritative USD price (e.g. 999 = $9.99).
    # get_ton_usd_price() returns the current market price of 1 TON in USD,
    # falling back to a cached value if the feed is stale (never raises).
    price_usd_cents: int = group["price_usd_cents"]
    price_usd: float = price_usd_cents / 100.0
    ton_usd_rate: float = await get_ton_usd_price()
    price_gram: float = price_usd / ton_usd_rate          # e.g. $9.99 / $2.00 = 4.995 GRAM
    price_nano: int = round(price_gram * 1_000_000_000)   # 4_995_000_000 nanoTON

    log.info(
        "generate_payment_request: user=%s group=%s price_usd=%.2f ton_rate=%.4f "
        "price_gram=%.6f price_nano=%d",
        user_id, group_id, price_usd, ton_usd_rate, price_gram, price_nano,
    )

    # Resolve DB user id to get the subscription row id
    user_row = await queries.get_user_by_telegram_id(user_id)
    db_user_id = user_row["id"] if user_row else None
    sub = await queries.get_subscription(db_user_id, group_id) if db_user_id else None
    subscription_id = sub["id"] if sub else 0

    # Use group-specific overrides if set, otherwise fallback to the live
    # global default from platform_config. get_global_fees() reads from DB
    # at call-time, so superadmin changes take effect without a restart.
    global_buyer_bps, global_admin_bps = await get_global_fees()
    group_buyer_bps = group["buyer_fee_bps"] if group["buyer_fee_bps"] is not None else global_buyer_bps
    group_admin_bps = group["admin_fee_bps"] if group["admin_fee_bps"] is not None else global_admin_bps

    params = VaultParams(
        admin_wallet=Address(admin_wallet_str),
        platform_wallet=Address(PLATFORM_WALLET),
        log_address=Address(LOG_ADDRESS),
        trigger_wallet=Address(TRIGGER_WALLET),
        price=price_nano,
        buyer_fee_bps=group_buyer_bps,
        admin_fee_bps=group_admin_bps,
        subscription_id=subscription_id,
    )

    try:
        link = build_payment_link(params, code_cell=_get_code_cell())
    except Exception as exc:
        log.error("Failed to build payment link for user=%s group=%s: %s", user_id, group_id, exc)
        raise

    # Register the vault address so the watcher can resolve it back to this subscription.
    # This is idempotent (INSERT OR IGNORE) so safe to call on every payment request.
    if db_user_id and subscription_id:
        # Store the exact required amount immediately — this MUST succeed.
        await queries.set_subscription_required_amount(subscription_id, link.required_nano)
        # Write vault_address into subscriptions so get_vaults_to_watch() finds it.
        # This is the column the polling loop reads; vault_registry is a secondary index.
        await queries.set_subscription_vault_address(subscription_id, link.vault_address)
        try:
            from renewise.watcher.db import register_vault
            await register_vault(
                vault_address=link.vault_address,
                subscription_id=subscription_id,
                user_id=db_user_id,
                group_id=group_id,
            )
            # Also register with the webhook server's polling loop (best-effort)
            import aiohttp as _aiohttp
            from renewise.watcher.config import WEBHOOK_HOST, WEBHOOK_PORT
            try:
                async with _aiohttp.ClientSession() as s:
                    await s.post(
                        f"http://{WEBHOOK_HOST}:{WEBHOOK_PORT}/register",
                        json={"vault_address": link.vault_address},
                        timeout=_aiohttp.ClientTimeout(total=2),
                    )
            except Exception:
                pass  # Webhook server may not be running in dev; non-fatal
        except Exception as reg_exc:
            log.warning("Vault registration failed (non-fatal): %s", reg_exc)

    buyer_fee_gram = price_gram * group_buyer_bps / 10000
    total_gram     = price_gram + buyer_fee_gram

    return PaymentRequest(
        payment_url=link.ton_deep_link,
        vault_address=link.vault_address,
        payload=link.vault_address,   # vault address is the unique payment identifier
        amount=total_gram,
        currency="TON",
        required_nano=link.required_nano,
    )


async def check_payment_status(user_id: int, group_id: int) -> PaymentStatus:
    """
    Check whether the subscription has been activated by the chain-watcher.

    user_id is the Telegram user id.
    The chain-watcher calls queries.activate_subscription() when it sees a
    confirmed PaymentLog from the vault. This function reads that DB state.
    """
    user_row = await queries.get_user_by_telegram_id(user_id)
    if not user_row:
        return PaymentStatus.PENDING

    sub = await queries.get_subscription(user_row["id"], group_id)
    if not sub:
        return PaymentStatus.PENDING
    if sub["status"] == "active":
        return PaymentStatus.CONFIRMED
    if sub["status"] in ("cancelled", "expired"):
        return PaymentStatus.FAILED
    return PaymentStatus.PENDING

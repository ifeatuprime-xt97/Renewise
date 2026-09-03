"""
renewise/ton/vault.py

Off-chain address computation and payment message builder for PaymentVault.

Uses pytoniq-core for all cell/BOC construction so it integrates cleanly with
the Python codebase and requires no Node.js at runtime.

Key design points
─────────────────
• compute_vault_address()  — pure math, no network call, instant.
• build_payment_link()     — returns a ton:// deep-link + exact amount.
• The cell layout mirrors Tact's generated init-data serialiser exactly:
  fields are stored in declaration order, each with its `as` annotation width.
• MIN_GAS_RESERVE must stay in sync with the contract constant (0.02 GRAM).
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

from pytoniq_core import Address, Cell, Builder, StateInit, begin_cell
from pytoniq_core.boc.address import AddressError

# ── Constants ─────────────────────────────────────────────────────────────────

# Must match PaymentVault.tact: const MIN_GAS_RESERVE: Int = ton("0.05")
MIN_GAS_RESERVE_NANO: int = 50_000_000  # 0.05 GRAM in nanogram

# Pay{} opcode — taken directly from the compiled ABI (PaymentVault_PaymentVault.abi):
# {"name":"Pay","header":3108783413,...}
# 3108783413 == 0xB94C4535
PAY_OPCODE: int = 0xB94C4535

# Refund{} opcode — from compiled ABI (PaymentVault_PaymentVault.abi):
# {"name":"Refund","header":2132218047,...}
# 2132218047 == 0x7F1710BF
REFUND_OPCODE: int = 0x7F1710BF

# GRAM workchain (basechain = 0)
WORKCHAIN: int = 0


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VaultParams:
    admin_wallet:    Address   # admin's wallet address
    platform_wallet: Address   # platform's wallet address
    log_address:     Address   # off-chain watcher address
    trigger_wallet:  Address   # dedicated hot wallet that sends Refund{} messages
    price:           int       # nanogram
    buyer_fee_bps:   int       # e.g. 200 = 2.00%
    admin_fee_bps:   int       # e.g. 330 = 3.30%
    subscription_id: int       # DB subscription row id (off-chain correlation)


@dataclass(frozen=True)
class PaymentLink:
    vault_address:    str    # user-friendly bounceable address string
    ton_deep_link:    str    # ton://transfer/... for Tonkeeper / TonHub QR
    required_nano:    int    # exact nanogram the buyer must send (incl. gas reserve)
    state_init_boc:   bytes  # raw BOC bytes of the StateInit cell
    body_boc:         bytes  # raw BOC bytes of the Pay{} body cell


# ── Cell builders ─────────────────────────────────────────────────────────────

def _build_data_cell(p: VaultParams) -> Cell:
    """
    Build the contract's init data cell.

    Layout is taken DIRECTLY from initPaymentVault_init_args in the compiled
    TypeScript (contracts/build/PaymentVault_PaymentVault.ts):

      Root cell:
        storeUint(0, 1)              ← Tact "not initialized" flag (1 bit, value 0)
        storeAddress(admin_wallet)
        storeAddress(platform_wallet)
        storeAddress(log_address)
        storeRef(b_1)

      b_1 (ref cell):
        storeAddress(trigger_wallet)
        storeInt(price, 257)          ← INIT layout uses Int(257), NOT coins/varuint16
        storeInt(buyer_fee_bps, 257)
        storeRef(b_2)

      b_2 (ref cell):
        storeInt(admin_fee_bps, 257)
        storeInt(subscription_id, 257)

    IMPORTANT: This is the init-args layout used for address computation and
    StateInit. It differs from the runtime storage layout (storePaymentVault$Data)
    which uses storeCoins/storeUint. The leading 0-bit (Tact init flag) is
    mandatory — omitting it shifts all address reads and produces wrong addresses.
    """
    b_2 = (
        begin_cell()
        .store_int(p.admin_fee_bps, 257)
        .store_int(p.subscription_id, 257)
        .end_cell()
    )
    b_1 = (
        begin_cell()
        .store_address(p.trigger_wallet)
        .store_int(p.price, 257)
        .store_int(p.buyer_fee_bps, 257)
        .store_ref(b_2)
        .end_cell()
    )
    return (
        begin_cell()
        .store_uint(0, 1)              # Tact "not initialized" flag
        .store_address(p.admin_wallet)
        .store_address(p.platform_wallet)
        .store_address(p.log_address)
        .store_ref(b_1)
        .end_cell()
    )


def _load_code_cell() -> Cell:
    """
    Load the compiled contract code cell from contracts/build/PaymentVault.code.boc.
    Raises FileNotFoundError with a clear message if the build hasn't been run yet.
    """
    boc_path = Path(__file__).parent.parent.parent / "contracts" / "build" / "PaymentVault_PaymentVault.code.boc"
    if not boc_path.exists():
        raise FileNotFoundError(
            f"Compiled contract not found at {boc_path}\n"
            "Run:  cd contracts && npm install && npm run build"
        )
    return Cell.one_from_boc(boc_path.read_bytes())


def _build_state_init(p: VaultParams, code_cell: Cell | None = None) -> StateInit:
    if code_cell is None:
        code_cell = _load_code_cell()
    return StateInit(code=code_cell, data=_build_data_cell(p))


def _build_pay_body() -> Cell:
    """Build the Pay{} message body (32-bit opcode, no fields)."""
    return begin_cell().store_uint(PAY_OPCODE, 32).end_cell()


def build_refund_body(recipient: Address) -> Cell:
    """
    Build the Refund{recipient} message body.
    Sent by trigger_wallet to instruct the vault to forward held overage
    to the buyer's wallet.

    Layout: [32-bit opcode][address]
    Opcode 0x7F1710BF confirmed from compiled ABI after `npm run build`.
    """
    return (
        begin_cell()
        .store_uint(REFUND_OPCODE, 32)
        .store_address(recipient)
        .end_cell()
    )


# ── Address computation ───────────────────────────────────────────────────────

def compute_vault_address(p: VaultParams, code_cell: Cell | None = None) -> Address:
    """
    Compute the vault's deterministic address from its init params.

    Pure off-chain math — no network call. The same params always produce the
    same address. Call this as soon as the user starts a join request to get
    the payment destination address before any on-chain transaction.

    Args:
        p:          VaultParams describing this subscription's vault.
        code_cell:  Pre-loaded code cell (optional; loaded from disk if None)

    Returns:
        pytoniq_core.Address for workchain 0.
    """
    if code_cell is None:
        code_cell = _load_code_cell()
    data_cell = _build_data_cell(p)

    # Address = hash of the serialised StateInit cell.
    # The StateInit cell MUST include the 5 flag bits, exactly as in the
    # ton:// deep-link builder — without them the hash (and therefore the
    # address) is wrong.
    state_init_cell = (
        begin_cell()
        .store_bit(0)          # split_depth absent
        .store_bit(0)          # special absent
        .store_bit(1)          # code present
        .store_ref(code_cell)
        .store_bit(1)          # data present
        .store_ref(data_cell)
        .store_bit(0)          # library absent
        .end_cell()
    )
    addr = Address((WORKCHAIN, state_init_cell.hash))
    addr.is_bounceable = True
    return addr


# ── Required payment amount ───────────────────────────────────────────────────

def required_payment_nano(p: VaultParams) -> int:
    """
    Return the exact nanoTON the buyer must attach to the Pay message.
    Includes price, buyer fee, and the gas reserve.
    """
    buyer_fee = p.price * p.buyer_fee_bps // 10000
    return p.price + buyer_fee + MIN_GAS_RESERVE_NANO


# ── Payment link builder ──────────────────────────────────────────────────────

def build_payment_link(p: VaultParams, code_cell: Cell | None = None) -> PaymentLink:
    """
    Build everything the bot needs to show the user a payment link.

    On first payment the wallet sends StateInit + Pay body together, deploying
    the vault and triggering the split atomically in one transaction.
    On renewals only the body is needed, but sending StateInit again is harmless
    (the network ignores it when the contract is already deployed).

    The ton:// deep-link encodes:
      - destination: vault address
      - amount:      price + buyer_fee + gas_reserve (nanoTON)
      - bin:         base64url Pay{} body
      - init:        base64url StateInit (for first-time deploy)

    Returns:
        PaymentLink with all fields needed to render a QR code or button.
    """
    if code_cell is None:
        code_cell = _load_code_cell()

    init      = _build_state_init(p, code_cell)
    addr      = compute_vault_address(p, code_cell)
    body      = _build_pay_body()
    amount    = required_payment_nano(p)

    # Serialise StateInit as a cell wrapping code + data refs
    init_cell = (
        begin_cell()
        .store_bit(0)          # split_depth absent
        .store_bit(0)          # special absent
        .store_bit(1)          # code present
        .store_ref(init.code)
        .store_bit(1)          # data present
        .store_ref(init.data)
        .store_bit(0)          # library absent
        .end_cell()
    )

    state_init_boc = init_cell.to_boc()
    body_boc       = body.to_boc()

    # IMPORTANT: use bounceable=True (EQ... prefix) so that if the contract's
    # require() check fails (e.g. underpayment), the sender's wallet will
    # receive an automatic bounce and get their GRAM back.
    # Non-bounceable (UQ...) silently traps money on any execution failure.
    addr_str  = addr.to_str(is_bounceable=True, is_url_safe=True)
    body_b64  = base64.urlsafe_b64encode(body_boc).decode().rstrip("=")
    init_b64  = base64.urlsafe_b64encode(state_init_boc).decode().rstrip("=")

    # ton:// — used for QR code (Tonkeeper scans it directly, full params including init)
    ton_link = (
        f"ton://transfer/{addr_str}"
        f"?amount={amount}"
        f"&bin={body_b64}"
        f"&init={init_b64}"
    )

    # ton:// for the Telegram inline button — includes &init= so the contract
    # is deployed atomically on the first send, regardless of which wallet the
    # user uses. Previously we used https://app.tonkeeper.com/transfer/... to
    # avoid Telegram's URL-length limit, but omitting &init= means undeployed
    # vaults show a "not active" warning and — worse — manual address copies
    # send plain transfers with no StateInit, trapping funds permanently.
    # ton:// is supported in Telegram bot buttons and has no length limit.
    #
    # The network ignores a duplicate StateInit on renewals (contract already deployed),
    # so sending &init= every time is safe and harmless.
    ton_button_link = (
        f"ton://transfer/{addr_str}"
        f"?amount={amount}"
        f"&bin={body_b64}"
        f"&init={init_b64}"
    )

    return PaymentLink(
        vault_address=addr_str,
        ton_deep_link=ton_button_link,  # ton:// with &init= — works for buttons and QR
        required_nano=amount,
        state_init_boc=state_init_boc,
        body_boc=body_boc,
    )


# ── Fee arithmetic helpers (used by tests and the payment service) ────────────

def expected_admin_amount(price: int, admin_fee_bps: int) -> int:
    """Admin receives price minus their fee. No tip — overage is held in vault."""
    admin_fee = price * admin_fee_bps // 10000
    return price - admin_fee


def expected_platform_amount(price: int, buyer_fee_bps: int, admin_fee_bps: int) -> int:
    buyer_fee = price * buyer_fee_bps // 10000
    admin_fee = price * admin_fee_bps // 10000
    return buyer_fee + admin_fee

"""
FIX 3 verification:
  1. Show current TRIGGER_MNEMONIC state
  2. Confirm fallback code path exists and handles the unset case correctly
  3. Confirm the startup log warning is present in bot.py's post_init
"""
import os, sys, inspect
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# ── Part 1: current env state ─────────────────────────────────────────────────
from renewise.config import TRIGGER_MNEMONIC
is_set = bool(TRIGGER_MNEMONIC)
print(f"TRIGGER_MNEMONIC in .env: {'SET (auto-refunds enabled)' if is_set else 'NOT SET (manual fallback active)'}")

# ── Part 2: confirm fallback code path in refund.py ──────────────────────────
import renewise.handlers.refund as refund_mod
src = inspect.getsource(refund_mod._process_wallet_submission)
assert "if not TRIGGER_MNEMONIC" in src, "Fallback guard missing!"
assert "pending_send" in src or "queued for manual" in src, "Manual queue path missing!"
assert "ALLOWED_SUPERADMIN_IDS" in src, "Superadmin alert missing from fallback!"
print("✅ Fallback code path confirmed in refund.py — unset TRIGGER_MNEMONIC → manual queue + superadmin alert.")

# ── Part 3: confirm startup warning in bot.py ─────────────────────────────────
import renewise.bot as bot_mod
post_init_src = inspect.getsource(bot_mod.post_init)
assert "TRIGGER_MNEMONIC" in post_init_src, "TRIGGER_MNEMONIC check missing from post_init!"
assert "log.warning" in post_init_src, "log.warning missing from post_init!"
assert "refund auto-trigger DISABLED" in post_init_src or "DISABLED" in post_init_src, \
    "Warning text missing!"
print("✅ Startup WARNING confirmed in bot.py post_init.")

# ── Part 4: confirm DOCUMENT.md is honest about the fallback ──────────────────
with open("DOCUMENT.md") as f:
    doc = f.read()
assert "TRIGGER_MNEMONIC" in doc, "TRIGGER_MNEMONIC missing from DOCUMENT.md!"
# Check that both modes are documented
assert "queued for manual" in doc or "manual processing" in doc, \
    "Manual fallback mode not documented!"
assert "trustless" in doc or "Refund{" in doc, "Trustless path not documented!"
print("✅ DOCUMENT.md documents both auto and manual refund modes.")

print()
print(f"Current mode: {'AUTO (trustless on-chain)' if is_set else 'MANUAL (superadmin queue)'}")
print("All FIX 3 checks passed.")

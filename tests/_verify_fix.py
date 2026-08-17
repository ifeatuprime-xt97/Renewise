import sys, inspect
sys.path.insert(0, '.')

from renewise.watcher.toncenter import call_get_method, fetch_transactions
from renewise.watcher.inprocess_watcher import _process_payment_inprocess
print("imports OK")

src = inspect.getsource(_process_payment_inprocess)
# Split at step 6 to only check the amount-verification section for the rate-fallback removal
src_before_step6 = src.split("Step 6")[0]

checks = [
    ("call_get_method used in watcher",              "call_get_method" in src),
    ("live rate fallback removed from amount check", "get_ton_usd_price" not in src_before_step6),
    ("partial check uses 'required is not None'",    "required is not None and new_total_paid < required" in src),
    ("overpayment check uses 'required is not None'","required is not None and new_total_paid > required" in src),
    ("group fetch still present",                    "get_group_by_id" in src),
]
all_ok = True
for label, ok in checks:
    status = "OK  " if ok else "FAIL"
    print(f"  {status}: {label}")
    if not ok:
        all_ok = False

print()
print("All checks passed." if all_ok else "SOME CHECKS FAILED.")

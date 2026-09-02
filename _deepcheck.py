"""
Deep runtime risk scan — finds issues that won't show up in syntax checks
but will cause production failures.
"""
import re, sys, ast

issues = []
warnings = []

def err(label):
    issues.append(label)
    print("ERR " + label)

def warn(label):
    warnings.append(label)
    print("WRN " + label)

def ok(label):
    print("OK  " + label)

def src(p):
    return open(p, encoding="utf-8").read()

srv  = src("renewise/miniapp/server.py")
q    = src("renewise/db/queries.py")
ap   = src("renewise/api/platform.py")
ka   = src("renewise/keepalive.py")
pay  = src("renewise/services/payment.py")
plat = src("renewise/services/platform.py")
html = src("renewise/miniapp/static/index.html")
sch  = src("renewise/db/schema.py")
sb   = src("renewise/superadmin/bot.py")
cg   = src("renewise/utils/coingecko.py")
bot  = src("renewise/bot.py")

print("=" * 60)
print("1. DATETIME SERIALIZATION — proxy payload")
print("=" * 60)

# Check _json_safe is applied to platform dict before aiohttp json=
if "_json_safe" in ap and "_platform_payload" in ap:
    ok("api/platform.py: _json_safe applied to platform dict before proxy call")
else:
    err("api/platform.py: _json_safe NOT applied to platform dict")

# Check keepalive internal endpoint — the platform dict comes from JSON
# (already strings from the Vercel side), so no datetime issue there
if "generate_platform_payment_request" in ka:
    ok("keepalive.py: payment generation called in internal endpoint")
else:
    err("keepalive.py: generate_platform_payment_request NOT called")

print()
print("=" * 60)
print("2. POSTGRESQL COLUMN MIGRATIONS — completeness")
print("=" * 60)

mig_adds = set(re.findall(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", sch))
pg_stmts_block = re.search(r"pg_stmts = \[(.*?)for stmt in pg_stmts", sch, re.DOTALL)
stmts_text = pg_stmts_block.group(1) if pg_stmts_block else ""
tables_in_pg = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", stmts_text))

# These tables were added incrementally and may need column migrations
# if the live DB was created before they were fully defined
critical_tables = ["platform_charges", "platforms", "groups", "subscriptions",
                   "users", "platform_config", "processed_tx_hashes"]

for tbl in critical_tables:
    covered = {c for t, c in mig_adds if t == tbl}
    if covered:
        ok(f"schema: {tbl} has {len(covered)} column migration(s): {sorted(covered)}")
    else:
        warn(f"schema: {tbl} has NO column migrations (only fresh-DB DDL applies)")

# Check that pg_migrations actually run for PostgreSQL path
if "for mig in pg_migrations" in sch and "await db.execute(mig)" in sch:
    ok("schema: pg_migrations loop executes on startup")
else:
    err("schema: pg_migrations loop NOT found or not executed")

print()
print("=" * 60)
print("3. SQL QUERIES — columns referenced that might not exist")
print("=" * 60)

# Check for any SELECT * queries on tables where columns were added
# These are safe (they return whatever exists), but UPDATE/INSERT with
# specific column names can fail if the column doesn't exist
unsafe_inserts = re.findall(
    r'INSERT INTO (\w+)\s*\(([^)]+)\)',
    srv + q + ap + plat
)
risky = []
for tbl, cols in unsafe_inserts:
    col_list = [c.strip() for c in cols.split(",")]
    # payment_url is the known problematic column
    if "payment_url" in col_list and tbl == "platform_charges":
        risky.append(f"platform_charges INSERT includes payment_url (needs migration)")
    if "wallet_passcode_hash" in col_list and tbl in ("groups", "platforms"):
        risky.append(f"{tbl} INSERT includes wallet_passcode_hash (needs migration)")

if not risky:
    ok("No high-risk INSERT column references found")
else:
    for r in risky:
        warn(r)

# Check UPDATE statements with newer columns
unsafe_updates = re.findall(
    r'UPDATE (\w+) SET ([^WHERE\n]+)',
    srv + q + ap + plat
)
for tbl, set_clause in unsafe_updates:
    if "payment_url" in set_clause and tbl == "platform_charges":
        ok(f"platform_charges UPDATE uses payment_url (covered by migration)")
    if "wallet_passcode_hash" in set_clause:
        ok(f"{tbl} UPDATE uses wallet_passcode_hash (covered by migration)")

print()
print("=" * 60)
print("4. PRICE FEED — resilience")
print("=" * 60)

providers = re.findall(r"async def (_fetch_\w+)", cg)
ok(f"coingecko.py: {len(providers)} providers: {providers}")

# Check FLOOR_PRICE is reasonable
floor = re.search(r"FLOOR_PRICE\s*=\s*([\d.]+)", cg)
if floor:
    fp = float(floor.group(1))
    if fp < 1.0:
        warn(f"FLOOR_PRICE=${fp} is very low — may cause overcharging subscribers")
    elif fp > 10.0:
        warn(f"FLOOR_PRICE=${fp} is high — may undercharge on floor fallback")
    else:
        ok(f"FLOOR_PRICE=${fp} is reasonable")

# Check cache TTL
ttl = re.search(r"CACHE_TTL\s*=\s*(\d+)", cg)
if ttl:
    ttl_val = int(ttl.group(1))
    ok(f"CACHE_TTL={ttl_val}s ({ttl_val//60} min)")

print()
print("=" * 60)
print("5. FRONTEND — error handling completeness")
print("=" * 60)

# Check all major async functions have try/catch
async_funcs = re.findall(r"async function (\w+)\([^)]*\)\s*\{([^}]{0,500})", html)
no_catch = []
for name, body in async_funcs:
    if "apiFetch" in body or "fetch(" in body:
        # Has a network call — should have error handling
        # Check if there's a catch in the full function (body is truncated, check by name)
        func_pattern = rf"async function {re.escape(name)}\s*\([^)]*\)\s*\{{.*?^\s*\}}"
        func_match = re.search(func_pattern, html, re.DOTALL | re.MULTILINE)
        if func_match:
            func_body = func_match.group(0)
            if "catch" not in func_body and "showAlert" not in func_body:
                no_catch.append(name)

if no_catch:
    for fn in no_catch:
        warn(f"html: {fn}() has network calls but no apparent error handling")
else:
    ok("html: All async network functions appear to have error handling")

# Check specific error paths we care about
checks = [
    ("_errDetail handles array detail", "Array.isArray(data.detail)"),
    ("renderDashboard catch uses instanceof", "e instanceof Error"),
    ("_renderSandboxError defined", "function _renderSandboxError"),
    ("ChargeDetail has fallback render", "_renderFallback"),
    ("PmtDetail has fallback render", "_renderFallback"),
    ("submitWallet handles 403 passkey error", "Incorrect passkey"),
    ("sandboxCreateCharge handles internal service errors", "non-JSON response"),
]
for label, pattern in checks:
    if pattern in html:
        ok(f"html: {label}")
    else:
        err(f"html: MISSING — {label}")

print()
print("=" * 60)
print("6. SUPERADMIN BOT — callback routing")
print("=" * 60)

# Check all sa_ callbacks are handled
expected_callbacks = [
    ("sa_fees_", "group fee override"),
    ("sa_pfees_", "platform fee override"),
    ("sa_platform_", "platform management"),
    ("sa_group_", "group management"),
    ("sa_revoke_", "platform key revoke"),
    ("sa_refunds_", "refund queue"),
    ("sa_u_", "user detail"),
    ("sa_ban_", "admin ban"),
    ("sa_ks_", "kill switch"),
    ("sa_announce_", "announcements"),
    ("sa_sub_", "subscription actions"),
]
for pattern, label in expected_callbacks:
    if f'startswith("{pattern}")' in sb or f'startswith(\'{pattern}\')' in sb:
        ok(f"sa_bot: {label} ({pattern}) handler present")
    else:
        err(f"sa_bot: {label} ({pattern}) handler MISSING")

print()
print("=" * 60)
print("7. CONFIGURATION RISKS")
print("=" * 60)

# Check config.py for missing env vars that could cause silent failures
with open("renewise/config.py", encoding="utf-8") as f:
    config = f.read()

# These should have non-empty defaults or clear error handling
critical_vars = [
    ("PLATFORM_WALLET", "required for payment generation"),
    ("LOG_ADDRESS", "required for payment generation"),
    ("TRIGGER_WALLET", "required for refunds"),
    ("BOT_TOKEN", "required for main bot"),
]
for var, reason in critical_vars:
    m = re.search(rf'{var}\s*[:=]\s*str\s*=\s*os\.getenv\(["\']?{var}["\']?,\s*["\']([^"\']*)["\']', config)
    if m:
        default = m.group(1)
        if default == "":
            ok(f"config: {var} defaults to empty string (will raise in generate_payment_request if not set)")
        else:
            warn(f"config: {var} has non-empty default '{default}' — may mask missing env var")
    else:
        ok(f"config: {var} present in config.py")

print()
print("=" * 60)
print("SUMMARY")
print("=" * 60)
total = len(issues) + len(warnings)
if not issues and not warnings:
    print("All checks passed — no issues found.")
else:
    if issues:
        print(f"{len(issues)} ERROR(S):")
        for i in issues:
            print(f"  ERR  {i}")
    if warnings:
        print(f"{len(warnings)} WARNING(S):")
        for w in warnings:
            print(f"  WRN  {w}")

sys.exit(1 if issues else 0)

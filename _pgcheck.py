"""
Cross-check: find columns that exist in the PostgreSQL DDL (pg_stmts CREATE TABLE)
but are NOT covered by pg_migrations ADD COLUMN, meaning they'd be missing from
existing live databases that pre-date the column addition.

Also checks for other potential deployment issues.
"""
import re, sys

with open("renewise/db/schema.py", encoding="utf-8") as f:
    s = f.read()

# ── 1. Extract columns added via pg_migrations ────────────────────────────────
pg_mig_block = re.search(r"pg_migrations = \[(.*?)for mig in pg_migrations", s, re.DOTALL)
mig_text = pg_mig_block.group(1) if pg_mig_block else ""
mig_adds = {(t, c) for t, c in re.findall(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", mig_text)}

# Also collect columns that exist in the base pg_stmts DDL
# (CREATE TABLE IF NOT EXISTS handles these for fresh DBs)
pg_stmts_block = re.search(r"pg_stmts = \[(.*?)for stmt in pg_stmts", s, re.DOTALL)
stmts_text = pg_stmts_block.group(1) if pg_stmts_block else ""

# Extract table -> set of column names from PG DDL
table_cols = {}
for tbl_match in re.finditer(
    r'CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\)\s*"""',
    stmts_text, re.DOTALL
):
    tbl = tbl_match.group(1)
    body = tbl_match.group(2)
    cols = []
    for line in body.strip().split("\n"):
        line = line.strip().strip(",")
        if not line or line.startswith("--") or line.startswith("UNIQUE") \
                or line.startswith("CHECK") or line.startswith("CONSTRAINT") \
                or line.startswith("PRIMARY"):
            continue
        col = line.split()[0] if line.split() else ""
        if col and col.isidentifier():
            cols.append(col)
    table_cols[tbl] = set(cols)

print("=" * 60)
print("Tables in pg_stmts DDL:")
for tbl, cols in sorted(table_cols.items()):
    print(f"  {tbl}: {len(cols)} columns -> {sorted(cols)}")

print()
print("=" * 60)
print("Columns added via pg_migrations ALTER TABLE:")
for t, c in sorted(mig_adds):
    print(f"  {t}.{c}")

# ── 2. SQLite vs PostgreSQL DDL comparison ────────────────────────────────────
# Find SQLite CREATE TABLE statements
sqlite_table_cols = {}
for tbl_match in re.finditer(
    r'CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\)\s*"""',
    s[:s.find("async def init_db")], re.DOTALL
):
    tbl = tbl_match.group(1)
    body = tbl_match.group(2)
    cols = []
    for line in body.strip().split("\n"):
        line = line.strip().strip(",")
        if not line or line.startswith("--") or line.startswith("UNIQUE") \
                or line.startswith("CHECK") or line.startswith("CONSTRAINT") \
                or line.startswith("PRIMARY"):
            continue
        col = line.split()[0] if line.split() else ""
        if col and col.isidentifier():
            cols.append(col)
    sqlite_table_cols[tbl] = set(cols)

print()
print("=" * 60)
print("Columns in SQLite DDL but NOT in pg_stmts DDL AND not in pg_migrations:")
issues = []
for tbl, sqlite_cols in sorted(sqlite_table_cols.items()):
    pg_cols = table_cols.get(tbl, set())
    mig_cols = {c for t, c in mig_adds if t == tbl}
    all_pg_known = pg_cols | mig_cols
    missing = sqlite_cols - all_pg_known
    if missing:
        issues.append((tbl, missing))
        print(f"  MISSING in PG: {tbl}.{sorted(missing)}")

if not issues:
    print("  None — all SQLite columns are covered in PG DDL or migrations.")

# ── 3. Check for other non-serializable types in proxy payload ─────────────────
print()
print("=" * 60)
print("Checking api/platform.py proxy payload for serialization issues:")
with open("renewise/api/platform.py", encoding="utf-8") as f:
    ap = f.read()
if "_json_safe" in ap:
    print("  OK: _json_safe helper is present in proxy payload")
else:
    print("  ERR: _json_safe helper MISSING — datetime objects will cause TypeError")
    issues.append(("api/platform.py", {"_json_safe missing"}))

# ── 4. Check all places that call coingecko.get_ton_usd_price ─────────────────
print()
print("=" * 60)
print("Price feed providers in coingecko.py:")
with open("renewise/utils/coingecko.py", encoding="utf-8") as f:
    cg = f.read()
providers = re.findall(r"async def (_fetch_\w+)", cg)
print(f"  {providers}")
if len(providers) < 3:
    print("  WARNING: fewer than 3 providers — rate limiting could cause failures")
else:
    print(f"  OK: {len(providers)} providers configured")

# ── 5. Check _errDetail handles array detail fields ────────────────────────────
print()
print("=" * 60)
print("Checking _errDetail in index.html for array handling:")
with open("renewise/miniapp/static/index.html", encoding="utf-8") as f:
    html = f.read()
if "Array.isArray(data.detail)" in html:
    print("  OK: _errDetail handles FastAPI array validation errors")
else:
    print("  ERR: _errDetail does NOT handle array detail fields")
    issues.append(("index.html", {"_errDetail array handling missing"}))

print()
print("=" * 60)
total_issues = len(issues)
print(f"{'No issues found.' if not total_issues else str(total_issues) + ' issue(s) found.'}")
sys.exit(1 if total_issues else 0)

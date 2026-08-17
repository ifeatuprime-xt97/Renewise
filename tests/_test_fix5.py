"""
FIX 5 verification:
  1. /healthz endpoint exists in server.py
  2. It executes a real SELECT 1 against the live DB and returns {"status":"ok"}
  3. Confirm 503 path code is present
"""
import asyncio, sys, inspect
search_replace

# ── Part 1: endpoint is registered ───────────────────────────────────────────
import renewise.miniapp.server as srv_mod

routes = [r.path for r in srv_mod.app.routes]
assert "/healthz" in routes, f"/healthz not found in routes: {routes}"
print(f"✅ /healthz registered. All routes: {[r for r in routes if not r.startswith('/static')]}")

# ── Part 2: invoke the handler directly (no HTTP server needed) ───────────────
async def run():
    result = await srv_mod.healthz()
    print(f"✅ healthz() returned: {result}")
    assert result == {"status": "ok"}, f"Expected ok, got {result}"

    # ── Part 3: 503 path present in source ───────────────────────────────────
    src = inspect.getsource(srv_mod.healthz)
    assert "503" in src, "503 status missing from healthz!"
    assert "SELECT 1" in src, "SELECT 1 missing from healthz!"
    print("✅ 503 error path and SELECT 1 DB check confirmed in source.")

asyncio.run(run())
print("\nAll FIX 5 (Part 1) checks passed.")

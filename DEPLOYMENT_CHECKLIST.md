# ReneWise Deployment Checklist — Payment Confirmation Fix

## Problem Summary
- Payments were staying "pending" after successful blockchain transactions
- Watcher was starting but immediately crashing due to duplicate bot instances
- Old code still running on Render (poll interval showing 15s instead of 5s)

## Root Causes Found
1. **Multiple bot instances running** — causing 409 Conflict errors that crash the entire service
2. **Environment variable not set** — `POLL_INTERVAL_SECONDS` still at default 15s instead of 5s
3. **Old deployment still active** — latest code changes not deployed to Render

## Fixes Applied

### 1. Startup Cleanup Script ✅ (Just Committed)
**File:** `run.py`
**What it does:** Automatically clears pending Telegram updates on startup, which forcibly terminates any old instances before starting the new one.

```python
async def clear_telegram_updates(token, bot_name):
    """Forces old instances to stop by claiming the update stream"""
    # Gets pending updates and acknowledges them
    # Old instances immediately get 409 Conflict and terminate
```

This prevents the duplicate instance problem automatically on every deployment.

---

## Deployment Steps (DO THIS NOW)

### Step 1: Deploy Latest Code to Render
The code has been pushed to GitHub. Render should auto-deploy, but verify:

1. Go to Render Dashboard → `renewise-bot` service
2. Check if deployment is in progress
3. If not, click **"Manual Deploy"** → **"Deploy latest commit"**
4. Wait for build to complete (~2-3 minutes)

### Step 2: Set Environment Variable
**IMPORTANT:** Set this BEFORE the service starts, or it will use the default 15s.

1. Go to Render Dashboard → `renewise-bot` service → **Environment** tab
2. Find or add: `POLL_INTERVAL_SECONDS`
3. Set value to: **`5`**
4. Click **Save**
5. Service will auto-restart with new value

### Step 3: Verify Clean Startup
Watch the logs in real-time:

1. Go to **Logs** tab
2. Look for these lines (should appear within 30 seconds):

**✅ SUCCESS INDICATORS:**
```
✓ Cleared X pending update(s) for main bot — old instances terminated
✓ Cleared X pending update(s) for superadmin bot — old instances terminated
In-process watcher started (poll interval=5s)
inprocess watcher: polling X vault(s)
```

**❌ FAILURE INDICATORS (these should NOT appear):**
```
❌ Conflict: terminated by other getUpdates request
❌ poll interval=15s  (means env var not set)
```

### Step 4: Test Payment Flow
Once logs look clean:

1. Create a new test charge via the miniapp
2. Make a payment from Tonkeeper
3. Watch the logs for:
   ```
   inprocess: NEW transaction detected - tx=... vault=... amount=...
   get_platform_charge_by_vault: looking up vault=...
   FOUND charge_id=X
   inprocess_platform: charge X status updated to completed
   ```
4. Check miniapp — status should change from "pending" to "completed" within 5 seconds

---

## Expected Timeline

| Event | Time |
|-------|------|
| Push code to GitHub | ✅ Done |
| Render auto-deploy starts | ~30 seconds after push |
| Build completes | ~2-3 minutes |
| Service restarts | Immediate after build |
| Clean startup (no conflicts) | Within 30 seconds |
| Watcher starts polling | Immediate |
| First poll cycle | 5 seconds after startup |
| Payment detection (after you pay) | Within 5 seconds of blockchain confirmation |

---

## Troubleshooting

### If you still see "Conflict" errors:
1. **Check instance count:** Dashboard → Settings → ensure "Instances" = **1**
2. **Force clean restart:** Dashboard → "Suspend Service" → wait 10 seconds → "Resume Service"
3. **Check for stuck processes:** Sometimes Render leaves zombie processes. Suspend + Resume always fixes this.

### If watcher doesn't poll:
1. **Check logs for errors** in the watcher startup
2. **Verify env var:** Look for `poll interval=5s` (not 15s) in startup log
3. **Check TonCenter API key:** Verify `TONCENTER_API_KEYS` is set in environment

### If payments still don't confirm:
1. **Verify vault address format** — should match exactly between DB and watcher
2. **Check miniapp logs** on Vercel for API errors
3. **Use debug endpoint:** `GET /api/developer/debug/charges` to see recent charges
4. **Manual recheck:** `POST /api/developer/charges/{id}/recheck` to force recheck

---

## Files Modified (Summary)

1. **`run.py`** — Added `clear_telegram_updates()` function to kill old instances on startup

Previously modified (already deployed in earlier sessions):
- `renewise/db/queries.py` — Enhanced logging in `get_platform_charge_by_vault()`
- `renewise/watcher/inprocess_watcher.py` — Added "NEW transaction detected" log
- `renewise/miniapp/server.py` — Added recheck and debug endpoints
- `renewise/miniapp/static/index.html` — Native wallet links, recheck button
- `renewise/miniapp/static/checkout.html` — Wallet warning message
- `renewise/handlers/join_request.py` — Friendlier payment messages
- `renewise/bot.py` — Friendlier renewal messages

---

## Configuration Check

Before declaring victory, verify these environment variables on Render:

**Required:**
- `BOT_TOKEN` ✓
- `SUPERADMIN_BOT_TOKEN` ✓
- `DATABASE_URL` (Neon connection string) ✓
- `PLATFORM_WALLET` ✓
- `TRIGGER_WALLET` ✓
- `TRIGGER_MNEMONIC` ✓
- `TONCENTER_API_KEYS` ✓
- `TONCENTER_TESTNET=false` ✓

**Should Update NOW:**
- `POLL_INTERVAL_SECONDS=5` ⚠️ **SET THIS**

**Optional but Recommended:**
- `INTERNAL_API_SECRET` (shared with Vercel miniapp)
- `KEEP_ALIVE_URLS` (to ping miniapp too)

---

## Next Steps After Deployment

1. ✅ Deploy code to Render
2. ✅ Set `POLL_INTERVAL_SECONDS=5` 
3. ✅ Verify clean startup (no Conflict errors)
4. ✅ Test payment flow end-to-end
5. 📋 Monitor for 24 hours to ensure stability
6. 📋 Update `.env.example.production` to document `POLL_INTERVAL_SECONDS=5` as the recommended value

---

## Success Criteria

✅ **No 409 Conflict errors in logs**
✅ **Watcher shows `poll interval=5s` on startup**
✅ **Regular polling logs every 5 seconds**
✅ **Payment detection within 5-10 seconds of blockchain confirmation**
✅ **Miniapp status updates from "pending" → "completed" automatically**
✅ **No manual "Check for Payment" clicks needed** (but button still works as fallback)

---

## Contact

If issues persist after following this checklist:
1. Share the **full startup logs** (first 100 lines after deployment)
2. Share the **payment test logs** (when you make a test payment)
3. Check the **Render service dashboard** for any health check failures

---

*Last updated: 2026-09-03*
*Commit: e5dc327*

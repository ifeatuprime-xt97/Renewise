# Payment Detection Test Plan

## Changes Deployed

1. ✅ Startup cleanup script - terminates old bot instances automatically
2. ✅ Poll interval changed from 15s → 5s
3. ✅ Verbose polling logs - now logs every poll cycle with vault count
4. ✅ Detailed charge lookup logging - shows exact address comparisons

## What to Watch in Render Logs

After the deployment completes (2-3 minutes), you should see:

### 1. Clean Startup (Every Time)
```
✓ No pending updates for main bot — clean start
✓ No pending updates for superadmin bot — clean start
In-process watcher started (poll interval=5s)
```

### 2. Active Polling (Every 5 Seconds)
```
inprocess watcher: polling 12 vault(s)
inprocess watcher: polling 12 vault(s)
inprocess watcher: polling 12 vault(s)
... (repeats every 5 seconds)
```

### 3. Payment Detection (When You Pay)

**Expected sequence:**
```
inprocess: NEW transaction detected - tx=XXX vault=EQ... amount=666666667
inprocess: no vault registry for EQ..., checking platform_charges
get_platform_charge_by_vault: looking up vault=EQ... (length=48)
get_platform_charge_by_vault: FOUND charge_id=36
inprocess_platform: tx=XXX charge_id=36 amount=666666667 status=pending
inprocess_platform: marking charge 36 as completed (tx=XXX)
inprocess_platform: charge 36 status updated to completed
inprocess_platform: charge 36 completed successfully
```

**If address mismatch (BAD):**
```
inprocess: NEW transaction detected - tx=XXX vault=EQ... amount=666666667
inprocess: no vault registry for EQ..., checking platform_charges
get_platform_charge_by_vault: looking up vault=EQ... (length=48)
get_platform_charge_by_vault: NOT FOUND for vault=EQ...
get_platform_charge_by_vault: 1 pending charges in DB:
  id=36 vault=UQ... no match (len=48)  ← Different format!
```

## Test Procedure

### Step 1: Wait for Deployment
1. Render will auto-deploy the new code (takes 2-3 min)
2. Watch for "Your service is live 🎉"
3. Verify you see the polling logs every 5 seconds

### Step 2: Create Test Charge
1. Go to your miniapp: https://renewise.vercel.app
2. Create a new test charge for $1.00
3. Note the charge ID from the URL (e.g., `/checkout/36`)

### Step 3: Make Payment
1. Click "Open Tonkeeper" button
2. Approve the payment in your wallet
3. **Immediately switch to Render logs** (within 5 seconds)

### Step 4: Watch Logs
Within 5-10 seconds of payment, you should see:
- `NEW transaction detected`
- `FOUND charge_id=X`
- `charge X status updated to completed`

### Step 5: Verify Frontend
- Vercel checkout page should update to "Payment Successful!" within 3-5 seconds
- Status should change from "Awaiting Payment" to completed

## Expected Outcomes

✅ **SUCCESS:**
- Logs show "FOUND charge_id=X"
- Logs show "status updated to completed"
- Frontend updates automatically
- No need to click "Check for Payment"

❌ **FAILURE (Address Mismatch):**
- Logs show "NOT FOUND for vault=..."
- Logs show pending charges with different address format (UQ vs EQ)
- Frontend stays stuck on "Awaiting Payment"

## If Address Mismatch Occurs

The logs will show exactly which address format is in the DB vs which the watcher is seeing.

Example:
```
Watcher sees:  EQBcEYbIDgs1Q4bD39eBfrXaRCsMEZInLbma4ZpPdhEyN6F3
Database has:  UQBcEYbIDgs1Q4bD39eBfrXaRCsMEZInLbma4ZpPdhEyN6F3
                ^^                                               ^^
              Different prefixes = address mismatch!
```

**Solution if this happens:**
1. Normalize addresses in the charge creation code
2. OR normalize addresses in the watcher lookup
3. We'll implement whichever fix is needed based on the logs

## Troubleshooting

### If polling logs don't appear:
- Check the watcher actually started: look for "In-process watcher started"
- Check for errors after that line
- Restart the Render service

### If payment detection doesn't happen:
- Verify the vault address in the database: `SELECT vault_address FROM platform_charges WHERE id=X`
- Verify the transaction on blockchain: https://tonscan.org/address/VAULT_ADDRESS
- Compare address formats character by character

### If frontend doesn't update:
- Check Vercel is actually polling: open browser devtools → Network tab
- Should see requests to `/api/public/checkout/X` every 3 seconds
- Verify the API is returning the correct status

## Debug Endpoints

If needed, you can manually check:

```bash
# Check charge status
curl https://renewise-bot.onrender.com/api/public/checkout/36

# Manual recheck (force watcher to check this charge now)
curl -X POST https://renewise-bot.onrender.com/api/developer/charges/36/recheck

# See all recent charges
curl https://renewise-bot.onrender.com/api/developer/debug/charges
```

## Success Criteria

- [  ] Deployment completes without errors
- [  ] Polling logs appear every 5 seconds
- [  ] No 409 Conflict errors after initial startup
- [  ] Payment detected within 5-10 seconds of blockchain confirmation
- [  ] Frontend updates automatically without manual intervention
- [  ] End-to-end flow works consistently on repeated tests

---

**Ready to test!** Wait for Render deployment, then follow steps 2-5 above. 🚀

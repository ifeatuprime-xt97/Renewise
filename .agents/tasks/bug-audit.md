# ReneWise Bug Audit Report

## Executive Summary

13 bugs found across the audited files, ranging from a runtime crash (CRITICAL) to minor inconsistencies (LOW). The most urgent issue is a **schema mismatch that will crash `queries.is_tx_processed()` on every call in the SQLite (dev) environment**, and a **2-tuple unpack of a 3-tuple in `watcher.py`** that will crash the watcher loop. Several HIGH-severity logic bugs exist around the upsert flow, the `cb_grant_confirm` path after a permission failure, and the freeze/delete flow interaction. The recently-added `pw:check_now` button in the missing-permissions DM is present and correct in both `chat_member.py` and `create_paywall.py`, and `get_groups_for_admin` correctly filters to `status IN ('active','paused')`.

---

## Bug Inventory

### BUG-01 `is_partial` column missing from SQLite `processed_tx_hashes` schema
**File:** `renewise/db/schema.py` `CREATE_PROCESSED_TX` (line 88–93)  
**File:** `renewise/db/queries.py` `is_tx_processed()` (line ~828)  
**Severity:** CRITICAL

**What the bug is:**  
The SQLite `CREATE_PROCESSED_TX` DDL in `schema.py` defines `processed_tx_hashes` with only two columns `tx_hash` and `sub_id`. The `is_partial` column does not exist in this table definition. However, `queries.is_tx_processed()` queries with `WHERE tx_hash=$1 AND is_partial=0`, and `watcher/db.py::mark_tx_processed()` writes with `INSERT INTO processed_tx_hashes (tx_hash, sub_id, is_partial) VALUES (...)`.

The `watcher/db.py::migrate()` function does add `is_partial` via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS is_partial` for SQLite, but the SQLite `IF NOT EXISTS` syntax on `ADD COLUMN` requires SQLite ≥ 3.37 (released Dec 2021). On older SQLite versions this migration will silently fail without the `IF NOT EXISTS` guard (which itself is valid from 3.37). If `migrate()` ever runs before `init_db()`, or if the DB was created before `watcher/db.py::migrate()` was added, the column will be absent and every `is_tx_processed()` call crashes with `OperationalError: table processed_tx_hashes has no column named is_partial`.

**Impact:**  
Every payment confirmation check fails. Users who have paid cannot be admitted. The bot crashes silently on every `cb_ive_paid` call. This is a dev/SQLite-only crash but is immediate and total.

**Fix:**  
Add `is_partial INTEGER NOT NULL DEFAULT 0` to `CREATE_PROCESSED_TX` in `schema.py`. Also add the migration entry to the SQLite ALTER TABLE list in `init_db()` mirroring what `watcher/db.py` already does.

---

### BUG-02 `watcher.py` unpacks 3-tuples from `get_vaults_to_watch()` as 2-tuples
**File:** `renewise/watcher/watcher.py` (line 74)  
**Severity:** CRITICAL

**What the bug is:**  
`queries.get_vaults_to_watch()` returns `list[tuple[str, str, float]]` a 3-tuple of `(vault_address, network, last_activity_epoch)`. But `watcher.py` line 74 unpacks it as:
```python
for vault_address, _network in vaults:
```
This is a 2-tuple unpack. Python will raise `ValueError: too many values to unpack (expected 2)` on the first iteration, crashing the watcher polling loop entirely.

**Impact:**  
The watcher loop crashes immediately on startup and never processes any payments. No subscriptions are ever activated by the background watcher (the in-process watcher in `inprocess_watcher.py` uses the correct 3-tuple unpack `for vault_address, network, last_activity in vaults` but `watcher.py` is the separate polling-mode watcher).

**Fix:**
```python
for vault_address, _network, _last_activity in vaults:
```

---

### BUG-03 `cb_grant_confirm` returns `STEP_INSTRUCTIONS` but the button says "I've Done It" → `pw:check_now`, creating a no-exit loop when permissions fail
**File:** `renewise/handlers/create_paywall.py` `cb_grant_confirm()` (line ~345–368)  
**Severity:** HIGH

**What the bug is:**  
When `cb_grant_confirm` is called after admin taps "Yes, set up paywall" but the grant is now missing permissions (someone demoted the bot between the DM check and the confirm tap), it edits the message to show the missing-permissions error with `_instructions_kb()` (which has "✅ I've Done It" → `pw:check_now`) and returns `STEP_INSTRUCTIONS`.

However, `cb_grant_confirm` is registered as an **entry point** of the ConversationHandler with pattern `^pw:grant_confirm:\d+$`. When it returns `STEP_INSTRUCTIONS`, the ConversationHandler correctly enters state `STEP_INSTRUCTIONS`. But the "I've Done It" button fires `pw:check_now` → `cb_check_now`, which re-queries `recent_admin_grants`. If those grants still show missing permissions, the user is shown the same error and sent back through `cb_grant_confirm` (which is also registered in `STEP_DETECT` states, not `STEP_INSTRUCTIONS`). The net effect: "I've Done It" → `cb_check_now` runs and calls `_present_single_grant` which does **not** re-check the DB grant via `_conn`, so a re-entry through `cb_grant_confirm` refetches fresh data, but `cb_check_now` only looks at `recent_admin_grants`, not at whether permissions have truly been updated. The flow works but has subtle state confusion.

The more concrete issue: if `cb_grant_confirm` is called **outside** a ConversationHandler (direct `pw:grant_confirm:N` callback from an old DM message after bot restart), returning `STEP_INSTRUCTIONS` from an entry_point context is valid, but the user is now in a `ConversationHandler` session with no record of the wizard state (`_w(ctx)` is `{}`), so if they then click anything related to the wizard, `_w(ctx).get("chat_id")` returns `None` and subsequent steps like `cb_confirm_chat` silently fail with no error.

**Impact:**  
User gets a confusing no-progress loop when trying to re-confirm after a permissions error. Wizard state is empty.

**Fix:**  
In `cb_grant_confirm` permission-failure path, also populate `_w(ctx)` with the grant data so subsequent wizard steps have context. Alternatively, send a fresh `/createpaywall` instruction rather than `_instructions_kb()`.

---

### BUG-04 `upsert_group` in `chat_member.py` (all-permissions path) creates a `status='active'` group before `activate_paywall` is called
**File:** `renewise/handlers/chat_member.py` (line ~103)  
**File:** `renewise/db/queries.py` `upsert_group()` (line ~57)  
**Severity:** HIGH

**What the bug is:**  
When all permissions are present, `handle_my_chat_member` calls `queries.upsert_group(...)`. The `upsert_group` function inserts with the column defaults, and the `groups` schema has `status TEXT NOT NULL DEFAULT 'active'`. So a group is created with `status='active'` immediately, before any price, wallet, or billing interval has been set (price defaults to 0, wallet is NULL).

This means after the bot is added as admin with all permissions, **the group immediately appears in "My Networks" via `get_groups_for_admin`** with `status='active'` and no wallet address. If a user at that moment requests to join the group, `handle_join_request` will find the group, see `status='active'`, and attempt to generate a payment with `price_usd_cents=0` which would compute a zero-value payment.

The recently-fixed flow now does `upsert_group` only when permissions are all present, but the status-active-before-setup problem remains.

**Impact:**  
Incomplete groups (no price, no wallet) can appear in "My Networks" and accept join requests generating zero-value payment requests. Data integrity issue.

**Fix:**  
`upsert_group` when called from `chat_member.py` should insert with `status='pending_setup'` (or any non-active/non-paused value that `get_groups_for_admin` filters out). Only `activate_paywall` should transition to `status='active'`. The schema CHECK constraint would need to be extended to include `'pending_setup'`.

---

### BUG-05 Frozen group not shown in My Networks but the freeze/demotion path does not DM the admin for already-active groups
**File:** `renewise/handlers/chat_member.py` bot-removal path (line ~187–220)  
**Severity:** HIGH

**What the bug is:**  
The fix to `get_groups_for_admin` correctly filters to `status IN ('active','paused')`, which excludes `'frozen'` groups. This is correct behavior for the main list. However, the freeze path at line ~192 has a special-case: if `group["status"] == "paused"`, it returns early without calling `freeze_group` or sending the warning DM (intentional it's the delete flow).

The problem is the condition is **only checking for `'paused'`**. If the group was already `'frozen'` (e.g. bot was removed twice once it froze, then re-added, then removed again), `group["status"]` would be `'frozen'`, the `if group["status"] == "paused": return` check is NOT met, `freeze_group` is called again (idempotent, no harm) but the **warning DM is sent again** to `group["admin_telegram_id"]`. This is a minor duplicate-DM issue, not a crash.

More importantly: if the group is frozen and then the admin re-adds the bot (the bot gets promoted again), `handle_my_chat_member` calls `upsert_group` again which does an `ON CONFLICT DO UPDATE SET admin_telegram_id=..., chat_type=...`. The `upsert_group` UPDATE clause does **not** reset `status` so the group remains `'frozen'` after re-add. The admin then goes through the wizard, reaches `cb_final_confirm`, which calls `activate_paywall` that does `SET status='active'`. This path works correctly. However, if the admin tries `/menu` or "My Networks" between re-add and completing the wizard, they see no networks (because status is still 'frozen') with a confusing "You don't have any active networks" message, even though they just re-added the bot.

**Impact:**  
Admin is confused after re-adding a previously frozen bot; they have no indication they need to re-run the wizard. "My Networks" shows empty.

**Fix:**  
After `upsert_group` in the all-permissions path in `chat_member.py`, check if the group is frozen and, if so, DM the admin with a "Your group was previously frozen complete the setup wizard to reactivate" message, or automatically set status to a `'pending_setup'` value.

---

### BUG-06 `delete_group` in `admin_menu.py` has a race condition with active payments
**File:** `renewise/handlers/admin_menu.py` `msg_delete_confirm_name()` (line ~1446)  
**File:** `renewise/db/queries.py` `delete_group()` (line ~980)  
**Severity:** HIGH

**What the bug is:**  
The delete flow is:
1. `set_group_status(group["id"], "paused")` pauses the group
2. Notifies subscribers via DM
3. `bot.leave_chat(...)` bot leaves
4. `delete_group(...)` deletes all records

Between steps 1 and 4, the watcher can still be processing a payment transaction for a `pending` subscription in this group. `activate_subscription` uses `WHERE status IN ('pending','active')` which would succeed. Then `delete_group` runs and hard-deletes the subscription and vault_registry rows. The watcher's bot listener (`bot_listener.py`) then tries to `approve_chat_join_request` for the now-deleted subscription's user, and may succeed (bot hasn't left yet at that microsecond). Result: user is admitted with no subscription record. Then `leave_chat` fires and the bot leaves.

This is a narrow race but it corrupts data: subscription deleted but user approved.

The broader issue is that `delete_group` uses multiple sequential `db.execute()` calls with **no explicit transaction**. The `_db()` context manager wraps in a single PG transaction automatically, so the deletes are atomic in Postgres. In SQLite, each `db.execute()` auto-commits, so a crash mid-delete leaves partial data.

**Impact:**  
SQLite: partial deletes leave orphaned subscription/vault_registry rows. Postgres: the multi-call sequence is atomic, but the watcher race window remains.

**Fix:**  
Set group status to `'suspended'` (not `'paused'`) immediately on delete confirmation so the watcher's group status check in `handle_join_request` short-circuits. Also set all `pending` subscriptions to `cancelled` before deleting.

---

### BUG-07 `_require_group` multi-group picker callback data parsing is fragile
**File:** `renewise/handlers/admin_menu.py` `_require_group()` (line ~48)  
**Severity:** MEDIUM

**What the bug is:**  
```python
_, gid_str, _ = query.data.split(":", 2)
```
This unpacks exactly 3 parts from the callback data. This works for `grpsel:5:menu:members` because `split(":", 2)` gives `["grpsel", "5", "menu:members"]`. That's correct. However, if `query.data` is exactly `"grpsel:5:"` (malformed), the third element is `""` which causes the picker to skip (no match found), silently falling through to `_show_network_picker` again. This is recoverable but produces a confusing loop.

The real issue is the `_require_group` function **also handles grpsel callbacks** at line ~48, but the actual `grpsel:` callback is separately handled by `cb_network_selected` registered in `register_menu_callbacks`. If both handlers fire (which they can in PTB when a callback matches multiple handlers), `query.answer()` is called twice, producing a `BadRequest: query already answered` error.

**Impact:**  
Potential double-answer error on `grpsel:` callbacks, which PTB's error handler catches and logs but doesn't crash. Medium noise and potential confusing behavior.

---

### BUG-08 `cb_pay_now` in `join_request.py` accesses `pending["payment_url"]` and `pending["vault_addr"]` before they are set
**File:** `renewise/handlers/join_request.py` `cb_pay_now()` (line ~170–180)  
**Severity:** MEDIUM

**What the bug is:**  
`handle_join_request` stores the pending context in `ctx.bot_data["pending_joins"][user.id]` with keys `group_id`, `chat_id`, `group_name`, `interval` but **does NOT include** `payment_url`, `vault_addr`, or `amount_ton`. These are only added later by `_send_payment_details` (called from the `pay_` deep-link handler in `/start`).

If a user somehow triggers `cb_pay_now` (callback data `join:pay_now`) **without** going through the `/start pay_N` deep-link (e.g. via a stale inline button from a prior session), `cb_pay_now` will try to access `pending["payment_url"]` on line ~180 and crash with `KeyError` because the key doesn't exist.

In practice, the new flow sends a URL button (not an inline callback button) for `pay_link`, so `cb_pay_now` should never be triggered in the new flow. However, `cb_pay_now` is still registered in `bot.py` (line ~) for `join:pay_now`, meaning any old button from a prior session would still hit it and crash.

**Impact:**  
`KeyError` crash for users with stale payment buttons from previous sessions. Error handler catches it but user sees the generic error message.

**Fix:**  
Guard the `pending["payment_url"]` access, or remove the `cb_pay_now` handler registration if it's been fully superseded by the deep-link flow.

---

### BUG-09 `msg_support` in `admin_menu.py` calls `_show_network_picker` after sending the confirmation, but `AWAIT_SUPPORT_MSG` ConversationHandler returns `ConversationHandler.END` this is correct, but the picker call can fail
**File:** `renewise/handlers/admin_menu.py` `msg_support()` (line ~955)  
**Severity:** MEDIUM

**What the bug is:**  
After sending the support confirmation, `msg_support` calls `await _show_network_picker(update, ctx)`. The `_show_network_picker` function first calls `queries.get_groups_for_admin(user_id)`. If the admin has no active/paused groups (e.g. they accessed support from the wizard flow via `pw:support`, not from `/menu`), `_show_network_picker` hits the `if not groups:` branch and calls `await update.message.reply_text(...)`. However, `update.callback_query` is `None` at this point (we're in a `MessageHandler` context), so it falls to `update.message.reply_text("⚠️ You don't have any active networks...")`. This works but sends a confusing "no networks" message right after "message sent to support" poor UX.

More importantly: the `cb_support` entry point is **only** registered in the support ConversationHandler with pattern `^menu:support$`. It is not registered for `^pw:support$`. So the `pw:support` button in `create_paywall.py` goes to a separate `cb_support` function in `create_paywall.py` (line ~853), not the one in `admin_menu.py`. The support ConversationHandler in admin_menu.py will never intercept `pw:support`. This is actually correct separation, but the create_paywall `cb_support` handler just shows a "contact @RenewiseSupport" message and returns `STEP_INSTRUCTIONS` it doesn't use the support conversation at all.

**Impact:**  
Confusing UX: admin submitting a support message from `/menu` is shown "no networks" message if they have no active groups. Low severity.

---

### BUG-10 `cb_members_page` passes `0` instead of the actual telegram_chat_id to `_send_members_page`
**File:** `renewise/handlers/admin_menu.py` `cb_members_page()` (line ~1050)  
**Severity:** MEDIUM

**What the bug is:**  
```python
await _send_members_page(update, ctx, gid, 0, int(page_str))
```
The second argument to `_send_members_page` is `chat_id_tg` (the Telegram chat ID), but `0` is passed instead of the actual `telegram_chat_id`. The function signature is:
```python
async def _send_members_page(update, ctx, group_id, chat_id_tg, page)
```
Looking at `_send_members_page`, the `chat_id_tg` parameter is **not actually used** in the function body the member buttons use only `uid` and `group_id`, not `chat_id_tg`. So passing `0` causes no functional harm currently, but it is incorrect and will break if `chat_id_tg` is ever used.

**Impact:**  
Currently harmless; future regression risk if `chat_id_tg` is used in the function.

---

### BUG-11 `get_vaults_to_watch()` returns 3-tuples but `server.py` debug endpoint unpacks as 2-tuples
**File:** `renewise/miniapp/server.py` `api_developer_debug_charges()` (line ~1321)  
**Severity:** MEDIUM

**What the bug is:**  
```python
watched_vaults = await get_vaults_to_watch()
watched_vault_addresses = {addr for addr, _net in watched_vaults}
```
`get_vaults_to_watch()` returns `list[tuple[str, str, float]]` (3-tuples: address, network, last_activity). This set comprehension uses a 2-tuple unpack `(addr, _net)` which will raise `ValueError: too many values to unpack` on every call to this endpoint.

**Impact:**  
The `/api/developer/debug/charges` endpoint always returns HTTP 500. Not user-facing in production but breaks the debug diagnostic tool.

**Fix:**
```python
watched_vault_addresses = {addr for addr, _net, _ts in watched_vaults}
```

---

### BUG-12 `platform.py::update_platform_wallet` allows changing wallet without a passcode even when a passcode IS set, if `current_wallet` is empty but `wallet_passcode_hash` is set
**File:** `renewise/services/platform.py` `update_platform_wallet()` (line ~220)  
**Severity:** MEDIUM

**What the bug is:**  
The function only requires passcode verification when `current_wallet` is truthy:
```python
if current_wallet:
    # verify passcode...
```
But it's possible for a platform to have `wallet_address = NULL` (not yet set) but `wallet_passcode_hash` set (passcode was set without a wallet). In this case, `current_wallet` is falsy so the passcode check is skipped entirely, allowing an attacker who gains access to the developer's Telegram account (but not their passcode) to set an attacker-controlled wallet address as the first wallet.

The intent was "friction only applies to changing an existing wallet." But if someone set a passcode on a platform that didn't yet have a wallet, their intent was clearly to protect wallet setup too.

**Impact:**  
Passcode protection can be bypassed on first wallet set if the passcode was set before the wallet address. Funds could be redirected to an attacker wallet.

**Fix:**  
Change the condition to `if current_wallet or existing_hash:` require passcode if either a current wallet exists OR a passcode hash exists.

---

### BUG-13 The `_schedule_wallet_change` function in `admin_menu.py` calls `get_superseded_pending_wallet_change` *after* `create_pending_wallet_change`, but `create_pending_wallet_change` already cancels all pending rows first
**File:** `renewise/handlers/admin_menu.py` `_schedule_wallet_change()` (line ~306–330)  
**File:** `renewise/db/queries.py` `create_pending_wallet_change()` (line ~945)  
**Severity:** LOW

**What the bug is:**  
The code flow is:
```python
superseded = await queries.get_superseded_pending_wallet_change(group["id"], admin_id)  # line 308

change_id = await queries.create_pending_wallet_change(...)  # line 310
# ^ this function internally cancels all existing pending rows first

superseded_row = await queries.get_superseded_pending_wallet_change(group["id"], admin_id)  # line 312
if superseded_row and superseded_row["id"] != change_id:
    # audit superseded change...
```
The first call to `get_superseded_pending_wallet_change` on line 308 stores a result in `superseded` but it is **never used**. The second call on line 312 correctly fetches the superseded row after creation. The first call is dead code and wastes a DB round trip.

**Impact:**  
Performance waste (extra DB query per wallet change). No correctness issue.

---

## Cross-Cutting Concerns

### CC-01 No transaction wrapping for multi-step wizard activation
**Files:** `create_paywall.py::cb_final_confirm()`, `miniapp/server.py::api_groups_create()`  
**Severity:** MEDIUM  

Both activation paths call:
1. `upsert_group(...)` 
2. `activate_paywall(...)` 
3. `update_group_price_usd_cents(...)`

Each call opens its own `_db()` context (its own PG transaction). If step 2 or 3 fails, step 1 has already committed, leaving a group row with `status='active'` but no price or wallet. A user joining at that moment would get a $0 payment request.

**Fix:** Expose a combined `activate_group_full(...)` query that performs all three writes in a single transaction.

---

### CC-02 `get_groups_for_admin` / frozen group in "My Networks" verified CORRECT
The recent fix to filter `status IN ('active','paused')` correctly excludes `'frozen'` and `'suspended'` groups. Groups frozen by bot removal do not appear in "My Networks". This is working as intended.

---

### CC-03 `cb_grant_confirm` `_db as _conn` with `db.fetchrow("... WHERE id=$1", grant_id)` verified CORRECT
The `_db` connection module wraps asyncpg and aiosqlite with a unified API that uses `$N` placeholders for both. The `fetchrow("SELECT * FROM recent_admin_grants WHERE id=$1", grant_id)` call in `cb_grant_confirm` and `cb_pick_chat` is syntactically correct and functionally valid on both backends.

---

## Findings Summary Table

| ID | Severity | File(s) | Short Description |
|----|----------|---------|-------------------|
| BUG-01 | CRITICAL | `db/schema.py`, `db/queries.py` | `is_partial` column missing from SQLite schema → crash on every payment check |
| BUG-02 | CRITICAL | `watcher/watcher.py` | 2-tuple unpack of 3-tuple from `get_vaults_to_watch` → crashes watcher loop |
| BUG-03 | HIGH | `handlers/create_paywall.py` | `cb_grant_confirm` permission-fail path leaves wizard state empty |
| BUG-04 | HIGH | `handlers/chat_member.py`, `db/queries.py` | `upsert_group` creates `status='active'` before wallet/price are set |
| BUG-05 | HIGH | `handlers/chat_member.py` | Re-adding bot to frozen group leaves it invisible in My Networks with no guidance |
| BUG-06 | HIGH | `handlers/admin_menu.py`, `db/queries.py` | Delete flow race with active watcher payments; SQLite non-atomic multi-delete |
| BUG-07 | MEDIUM | `handlers/admin_menu.py` | `_require_group` callback data parsing can double-answer `grpsel:` queries |
| BUG-08 | MEDIUM | `handlers/join_request.py` | `cb_pay_now` crashes with `KeyError` on stale `join:pay_now` buttons |
| BUG-09 | MEDIUM | `handlers/admin_menu.py` | `msg_support` shows confusing "no networks" after support message sent |
| BUG-10 | MEDIUM | `handlers/admin_menu.py` | `cb_members_page` passes `0` as `chat_id_tg` to `_send_members_page` |
| BUG-11 | MEDIUM | `miniapp/server.py` | Debug charges endpoint 2-tuple unpack of 3-tuple → HTTP 500 always |
| BUG-12 | MEDIUM | `services/platform.py` | Passcode bypass: wallet can be set without passcode if no existing wallet even with a passcode hash present |
| BUG-13 | LOW | `handlers/admin_menu.py`, `db/queries.py` | Dead first call to `get_superseded_pending_wallet_change` |
| CC-01 | MEDIUM | `create_paywall.py`, `miniapp/server.py` | Multi-step group activation not wrapped in a single transaction |

---

## Priority Fix Order

1. **BUG-02** (watcher crash) fix immediately; blocks all payment processing in polling mode
2. **BUG-01** (schema crash) fix in schema.py; blocks payment confirmation in SQLite dev
3. **BUG-04** (zero-price group) data integrity; stop groups going active before wizard completion
4. **BUG-12** (passcode bypass) security; change condition to `if current_wallet or existing_hash:`
5. **CC-01** (non-atomic activation) data integrity; wrap in a single transaction
6. **BUG-06** (delete race) data integrity on delete
7. **BUG-11** (debug endpoint crash) quick 2-char fix
8. **BUG-08** (stale button crash) add KeyError guard
9. Remaining bugs: LOW/MEDIUM with UX impact only

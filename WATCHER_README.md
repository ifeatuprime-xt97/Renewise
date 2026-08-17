# renewise — Phase 3: Chain-Watcher & Subscription Lifecycle

This document covers running the full stack locally: Phase 1 bot + Phase 3 watcher.

---

## Architecture

```
                    TON blockchain
                         │
              ┌──────────▼──────────┐
              │  TonCenter API      │  (webhook push or polling fallback)
              └──────────┬──────────┘
                         │ POST /webhook
              ┌──────────▼──────────┐
              │  Webhook Server     │  renewise/watcher/webhook.py
              │  (aiohttp :8080)    │  + VaultPoller (polling fallback)
              └──────────┬──────────┘
                         │ enqueue
              ┌──────────▼──────────┐
              │  Redis / RQ Queue   │  "renewise" queue
              └──────────┬──────────┘
                         │ consume
              ┌──────────▼──────────┐
              │  RQ Workers (1–N)   │  renewise/watcher/worker.py
              │  process_payment    │  verify → activate → publish bot action
              └──────────┬──────────┘
                         │ Redis pub/sub
              ┌──────────▼──────────┐
              │  Bot Process        │  renewise/bot.py
              │  (bot_listener)     │  approve join request + send DMs
              └─────────────────────┘

              ┌─────────────────────┐
              │  Scheduler          │  renewise/watcher/scheduler.py
              │  (APScheduler)      │  renewal reminders + grace enforcement
              └──────────┬──────────┘
                         │ enqueue
                    RQ Queue (same)
```

---

## Prerequisites

- Python 3.11+
- Redis 6+ running locally (or via Docker)
- Node.js 18+ (for Tact contract compilation — one-time)

---

## Quick Start (local dev)

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Build the smart contract (one-time)

```bash
cd contracts
npm install
npm run build
cd ..
```

### 3. Configure environment

```bash
cp .env.example .env
# Fill in at minimum:
#   BOT_TOKEN
#   PLATFORM_WALLET
#   LOG_ADDRESS
#   REDIS_URL (default: redis://localhost:6379/0)
#   TONCENTER_API_KEY (get free at https://toncenter.com)
#   TONCENTER_TESTNET=true  (for local testing)
```

### 4. Start Redis

```bash
# Docker (easiest):
docker run -d -p 6379:6379 redis:7-alpine

# Or if Redis is installed locally:
redis-server
```

### 5. Start all processes (4 terminals)

**Terminal 1 — Telegram bot:**
```bash
python -m renewise.bot
```

**Terminal 2 — RQ workers (4 concurrent):**
```bash
python -m renewise.watcher.worker --concurrency 4
```

**Terminal 3 — Webhook server + polling loop:**
```bash
python -m renewise.watcher.webhook
```

**Terminal 4 — Scheduler (renewal reminders + grace enforcement):**
```bash
python -m renewise.watcher.scheduler
```

---

## Process Reference

| Process | Command | Purpose |
|---|---|---|
| Bot | `python -m renewise.bot` | Telegram bot, handles join requests |
| Workers | `python -m renewise.watcher.worker --concurrency 4` | Process payment jobs from queue |
| Webhook server | `python -m renewise.watcher.webhook` | Receive TonCenter webhooks + polling |
| Scheduler | `python -m renewise.watcher.scheduler` | Renewal reminders + grace enforcement |

All four must be running for the full pipeline to work.  In production, run each
as a systemd service or Docker container.

---

## TonCenter Webhook Setup

1. Get a free API key at https://toncenter.com
2. Set `TONCENTER_API_KEY` and `WEBHOOK_SECRET` in `.env`
3. Expose your webhook server publicly (ngrok for local dev):
   ```bash
   ngrok http 8080
   ```
4. Register the webhook with TonCenter:
   ```bash
   curl -X POST "https://toncenter.com/api/v2/setWebhook" \
     -H "X-API-Key: YOUR_KEY" \
     -d "url=https://YOUR_NGROK_URL/webhook&secret=YOUR_WEBHOOK_SECRET"
   ```

If webhooks are unavailable, the polling loop in `webhook.py` automatically
falls back to polling every `POLL_INTERVAL_SECONDS` (default: 15s).

---

## Payment Flow (end-to-end)

```
User requests to join group
        │
        ▼
Bot: generate_payment_request()
  → computes vault address (pure math, instant)
  → registers vault in vault_registry DB table
  → registers vault with webhook server polling loop
  → returns ton:// deep-link
        │
        ▼
User taps link in Tonkeeper
  → sends StateInit + Pay body to vault address
  → vault deploys + splits funds atomically
  → emits PaymentLog message to LOG_ADDRESS
        │
        ▼
TonCenter detects tx on vault address
  → POST /webhook  (or polling loop picks it up)
        │
        ▼
Webhook server enqueues process_payment job
        │
        ▼
RQ worker picks up job:
  1. Check processed_tx_hashes (idempotency)
  2. Look up vault_registry → (sub_id, user_id, group_id)
  3. Verify amount >= price + buyer_fee
  4. Atomically claim tx hash (INSERT OR IGNORE)
  5. activate_subscription() in DB
  6. Publish "approve_and_welcome" to Redis pub/sub
        │
        ▼
Bot listener receives pub/sub message:
  → approve_chat_join_request()
  → send welcome DM to user
```

---

## Idempotency

The `processed_tx_hashes` table is the safety net against double-processing.

- The worker does a fast pre-check (`SELECT`) before any writes.
- The actual claim is an `INSERT OR IGNORE` — only one worker wins the race.
- If a worker crashes after the INSERT but before activating the subscription,
  the retry will find the hash already claimed and exit cleanly.
- The subscription activation (`UPDATE ... WHERE status='pending'`) is also
  safe to run twice — it's a no-op if already active.

---

## Burst Handling

The RQ queue absorbs bursts naturally:

- 100 simultaneous webhook calls → 100 jobs enqueued in Redis in ~50ms
- Workers drain the queue at ~10–50 jobs/s depending on DB write speed
- SQLite WAL mode allows concurrent reads + one writer, sufficient for
  the write patterns here (one INSERT + one UPDATE per payment)
- For higher throughput, swap aiosqlite for asyncpg + PostgreSQL

Run the load test to verify your setup handles 120 concurrent events:

```bash
# With workers running:
python tests/load_test.py
```

Expected output:
```
✅ LOAD TEST PASSED
  Subscriptions activated:     120 / 120
  Duplicate tx_hash entries:   0
  Failed jobs:                 0
  Jobs still in queue:         0
```

---

## Manual Recheck

For support cases where the automated pipeline missed a payment:

```python
from renewise.watcher.recheck import recheck_by_tx_hash, recheck_by_subscription

# By tx hash:
result = await recheck_by_tx_hash("abc123...", "EQvault...")
# → {"enqueued": True, "reason": "ok"}

# By subscription:
result = await recheck_by_subscription(user_db_id=42, group_id=7)
# → {"enqueued": 1, "vault_address": "EQ...", "reason": "ok"}
```

Or via the webhook server's HTTP endpoint:

```bash
curl -X POST http://localhost:8080/recheck \
  -H "Content-Type: application/json" \
  -d '{"vault_address": "EQ...", "tx_hash": "abc123..."}'
```

---

## Scheduled Jobs

| Job | Schedule | What it does |
|---|---|---|
| `renewal_reminder_job` | Hourly at :05 | DMs users whose subscription expires within `REMINDER_WINDOW_DAYS` |
| `grace_enforcement_job` | Hourly at :15 | Expires + kicks users past `next_renewal_date + GRACE_PERIOD_DAYS` |

Both jobs run immediately on scheduler startup (no waiting up to an hour).

Skipped for:
- `status = 'comped'` subscriptions
- Users who are the group's registered admin

---

## Structured Logging

Every state transition is logged with structured fields:

```
2024-01-15 10:23:01 [INFO] tasks: process_payment start | vault=EQ... tx=abc123 amount=1020000000
2024-01-15 10:23:01 [INFO] tasks: process_payment: subscription activated | sub_id=42 user_id=7 group_id=3 tx=abc123
2024-01-15 10:23:01 [INFO] tasks: process_payment done | vault=EQ... tx=abc123
2024-01-15 10:23:01 [INFO] bot_listener: Approved join request | user=123456789 chat=-1001234567890
```

To trace a support issue end-to-end, grep for the tx hash or vault address
across all process logs.

---

## Environment Variables (Phase 3)

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `RQ_QUEUE_NAME` | `renewise` | RQ queue name |
| `TONCENTER_API_KEY` | `` | TonCenter API key |
| `TONCENTER_TESTNET` | `false` | Use testnet endpoint |
| `MIN_CONFIRMATIONS` | `1` | Confirmations before tx is final |
| `POLL_INTERVAL_SECONDS` | `15` | Polling fallback interval |
| `WEBHOOK_HOST` | `0.0.0.0` | Webhook server bind host |
| `WEBHOOK_PORT` | `8080` | Webhook server port |
| `WEBHOOK_SECRET` | `` | X-Webhook-Token verification secret |
| `GRACE_PERIOD_DAYS` | `3` | Days after renewal before kick |
| `REMINDER_WINDOW_DAYS` | `3` | Days before renewal to send reminder |
| `JOB_MAX_RETRIES` | `5` | Max RQ job retry attempts |
| `JOB_RETRY_BASE_DELAY` | `10` | Base delay (s) for exponential backoff |
| `BOT_ACTIONS_CHANNEL` | `renewise:bot_actions` | Redis pub/sub channel |

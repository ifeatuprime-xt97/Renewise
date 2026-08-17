# ReneWise

Non-custodial subscription paywall bot for Telegram groups and channels. Members pay in TON, admins receive payouts directly to their wallet — no middleman holds funds at any point.

---

## How It Works

1. Admin adds the bot to their group/channel as admin and runs `/createpaywall`
2. Bot generates a unique invite link for the group
3. Member requests to join → bot DMs them a payment link (QR code + Tonkeeper deep-link)
4. Member pays in TON → a per-subscription smart contract splits the payment atomically:
   - Admin receives their share directly to their payout wallet
   - Platform receives its fee
   - Any overpayment > $1 USD is held in the vault for automatic refund
5. Bot detects the on-chain payment, approves the join request, and sends a welcome DM
6. Before expiry, bot sends a renewal reminder. If not renewed, member is removed automatically.

---

## Quick Start

### Prerequisites
- Python 3.11+
- Node.js 18+ (for contract compilation only)
- Two Telegram bot tokens from [@BotFather](https://t.me/BotFather) — one for the main bot, one for the superadmin bot
- Group Privacy mode **disabled** in BotFather for the main bot

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Build the smart contract
```bash
cd contracts
npm install
npm run build
cd ..
```

### 3. Configure environment
```bash
cp .env.example .env
# Fill in all values — see Environment Variables section below
```

### 4. Run everything
```bash
python run.py
```

This starts the main bot, superadmin bot, and in-process payment watcher in a single process. No Redis or separate worker processes needed for development/testnet.

---

## Environment Variables

### Core
| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Main bot token from BotFather |
| `DATABASE_PATH` | optional | SQLite path (default: `./data/renewise.db`) |
| `SUPERADMIN_BOT_TOKEN` | ✅ | Separate bot token for the superadmin control panel |
| `ALLOWED_SUPERADMIN_IDS` | ✅ | Comma-separated Telegram user IDs allowed to use superadmin bot |

### TON Payments
| Variable | Required | Description |
|---|---|---|
| `PLATFORM_WALLET` | ✅ | TON address that receives platform fees (keep offline/cold) |
| `LOG_ADDRESS` | ✅ | TON address that receives `PaymentLog` messages for off-chain indexing |
| `TRIGGER_WALLET` | ✅ | Dedicated hot wallet address for sending `Refund{}` messages to vaults |
| `TRIGGER_MNEMONIC` | ✅ | 24-word mnemonic for the trigger wallet (NOT the platform wallet) |
| `OVERPAYMENT_REFUND_THRESHOLD_USD` | optional | Minimum overpayment to trigger refund flow (default: `1.00`) |
| `BUYER_FEE_BPS` | optional | Buyer-side fee in basis points (default: `200` = 2.00%) |
| `ADMIN_FEE_BPS` | optional | Admin-side fee in basis points (default: `330` = 3.30%) |

### Chain Watcher (TonCenter)
| Variable | Required | Description |
|---|---|---|
| `TONCENTER_API_KEYS` | recommended | Comma-separated TonCenter API keys (avoids rate limits) |
| `TONCENTER_TESTNET` | optional | Set to `true` to use testnet (default: `false`) |
| `POLL_INTERVAL_SECONDS` | optional | How often to poll vaults for new transactions (default: `15`) |
| `MIN_CONFIRMATIONS` | optional | Confirmations before treating tx as final (default: `1`) |

### Subscription Lifecycle
| Variable | Required | Description |
|---|---|---|
| `GRACE_PERIOD_DAYS` | optional | Days after expiry before member is removed (default: `3`) |
| `REMINDER_WINDOW_DAYS` | optional | Days before expiry to send renewal reminder (default: `3`) |

### Production (Redis + RQ)
| Variable | Production only | Description |
|---|---|---|
| `REDIS_URL` | ✅ | Redis connection URL (default: `redis://localhost:6379/0`) |
| `RQ_QUEUE_NAME` | optional | RQ queue name (default: `renewise_tasks`) |
| `BOT_ACTIONS_CHANNEL` | optional | Redis pub/sub channel for watcher→bot actions |
| `JOB_MAX_RETRIES` | optional | Max payment job retries (default: `3`) |

---

## Bot Setup Checklist

1. Create the main bot via [@BotFather](https://t.me/BotFather), copy token to `.env`
2. In BotFather → Bot Settings → Group Privacy → **Turn off**
3. Create a second bot for superadmin — copy its token as `SUPERADMIN_BOT_TOKEN`
4. Find your Telegram user ID (message [@userinfobot](https://t.me/userinfobot)) and add to `ALLOWED_SUPERADMIN_IDS`
5. Set up your trigger wallet: create a fresh wallet in Tonkeeper, fund with ~2 TON for gas, copy address → `TRIGGER_WALLET` and mnemonic → `TRIGGER_MNEMONIC`
6. Build the contract: `cd contracts && npm install && npm run build`
7. Start: `python run.py`
8. Add the main bot to your group/channel as admin with: **Invite Users**, **Manage Chat**, **Approve New Members**
9. DM the main bot `/createpaywall` to run the setup wizard

---

## Smart Contract — PaymentVault

One vault contract is deployed per subscription (user × group pair), on first payment via TON's deploy-on-first-message pattern.

### Fee Model
```
Buyer sends:     price + buyer_fee + gas_reserve
Admin receives:  price - admin_fee              (direct to payout wallet)
Platform gets:   admin_fee + buyer_fee          (to PLATFORM_WALLET)
Vault holds:     any overpayment above required (for trustless refund)
```

### Overpayment Refund Flow
When a member sends more than the required amount:
1. Vault holds the excess in `overage_held` (on-chain state)
2. Watcher detects it and DMs the member asking for their wallet address
3. Member replies with their TON address (validated by the bot)
4. Trigger wallet sends `Refund{recipient}` to the vault
5. Vault verifies `sender == trigger_wallet` and releases `overage_held` to the member
6. Member receives their TON directly from the vault — no platform private key involved

**Security:** The trigger wallet only holds ~2 TON for gas. If compromised, an attacker can only trigger refunds the contract already validated — they cannot redirect funds or drain the platform wallet.

### Underpayment
If a member sends less than required, the contract rejects the message with exit code 400 and TON's bounce mechanism returns most of the funds automatically. The bot additionally DMs the member explaining the shortfall.

### Contract Messages
| Message | Sender | Description |
|---|---|---|
| `Pay{}` | Payer | Deploys vault (first payment) and triggers split |
| `Refund{recipient}` | Trigger wallet only | Releases held overage to recipient |

---

## Architecture

```
python run.py
├── Main bot (PTB Application)
│   ├── /start, /menu, /createpaywall
│   ├── ChatJoinRequestHandler → payment flow
│   ├── CallbackQueryHandlers  → pay/confirm/cancel/refund buttons
│   └── MessageHandler         → wallet address collection for refunds
├── Superadmin bot (separate PTB Application)
│   └── /start → dashboard: groups, users, fees, kill switch, refund audit
└── In-process payment watcher (asyncio task)
    ├── Polls TonCenter every POLL_INTERVAL_SECONDS
    ├── Detects new vault transactions
    ├── Activates subscriptions in DB
    ├── Detects overpayments → triggers refund flow
    └── Calls bot handlers directly (no Redis needed in dev mode)
```

### Production Scaling
For production with high volume, swap the in-process watcher for the separate stack:
```bash
# Terminal 1: Bot
python -m renewise.bot

# Terminal 2: RQ workers
python -m renewise.watcher.worker --concurrency 4

# Terminal 3: Webhook server (TonCenter push)
python -m renewise.watcher.webhook

# Terminal 4: Scheduler (renewal reminders + grace enforcement)
python -m renewise.watcher.scheduler

# Terminal 5: Superadmin bot
python -m renewise.superadmin.bot
```

---

## Project Structure

```
renewise/
├── bot.py                      # Main bot entry point
├── config.py                   # Environment variable loading
├── run.py                      # Single-process launcher (bot + watcher)
├── db/
│   ├── schema.py               # Table definitions + migrations
│   └── queries.py              # All async DB operations
├── handlers/
│   ├── admin_menu.py           # /menu + admin sub-flows
│   ├── chat_member.py          # Bot added/removed as admin
│   ├── create_paywall.py       # /createpaywall wizard
│   ├── join_request.py         # Member join + payment flow
│   └── refund.py               # Overpayment wallet collection + refund trigger
├── services/
│   ├── payment.py              # Payment request generation
│   └── wallet.py               # TON address validation
├── superadmin/
│   ├── bot.py                  # Superadmin control panel bot
│   └── queries.py              # Superadmin-specific DB queries
├── ton/
│   ├── vault.py                # Off-chain address computation + message builders
│   ├── deploy.py               # Testnet deployment + split verification script
│   └── refund_trigger.py       # Sends Refund{} to vault via trigger wallet
├── utils/
│   ├── coingecko.py            # TON/USD price feed (CoinGecko + CMC fallback)
│   └── keyboards.py            # InlineKeyboardMarkup builders
└── watcher/
    ├── inprocess_watcher.py    # Dev-mode in-process payment polling
    ├── bot_listener.py         # Redis pub/sub → bot actions bridge (production)
    ├── scheduler.py            # Renewal reminders + grace enforcement jobs
    ├── tasks.py                # RQ job definitions (production)
    ├── toncenter.py            # TonCenter API client
    └── webhook.py              # TonCenter webhook receiver (production)

contracts/
├── PaymentVault.tact           # Smart contract source
├── tests/
│   └── PaymentVault.spec.ts    # Contract test suite
└── build/                      # Compiled contract (generated by npm run build)
```

---

## Data Model

```sql
groups                  one row per Telegram group/channel
users                   one row per Telegram user
subscriptions           one row per (user, group) pair — status + vault address
vault_registry          vault_address → (subscription, user, group) mapping
processed_tx_hashes     idempotency table — prevents double-processing
overpayment_refunds     refund lifecycle: pending_wallet → pending_send → sent
admin_audit_log         append-only log of all admin and platform actions
platform_config         global fee defaults + payments kill switch
```

---

## Admin Commands (Main Bot)

| Command | Description |
|---|---|
| `/start` | Dashboard — manage groups and subscriptions |
| `/createpaywall` | Launch the paywall setup wizard (DM only) |
| `/menu` | Open the admin management menu (DM only) |

## Superadmin Commands

| Command | Description |
|---|---|
| `/start` | Platform overview — groups, revenue, users |
| `/pendingrefunds` | View overpayment refund audit log + trigger wallet status |

---

## Running Tests

```bash
# Python unit tests (no Node required)
pytest renewise/ton/tests/test_vault.py -v -m "not tvm"

# Full test suite including TVM execution (requires Node + built contract)
pytest renewise/ton/tests/test_vault.py -v

# Testnet end-to-end deployment + split verification
python -m renewise.ton.deploy
```


---

## Production Deployment — Process Supervision

The project has no external process manager dependency, but you **must** run the two processes under something that restarts them automatically. A crashed bot means no subscription activations, no renewal enforcement, and no refunds until it's manually restarted.

### What runs where

| Process | Command | Restarts needed? |
|---|---|---|
| Bot + watcher | `python run.py` | ✅ Yes — critical |
| Mini App API | `uvicorn renewise.miniapp.server:app --host 0.0.0.0 --port 8000` | ✅ Yes — critical |

### systemd (recommended for Linux VPS)

Two unit files are provided in `deploy/`:

```bash
# Copy unit files
sudo cp deploy/renewise-bot.service /etc/systemd/system/
sudo cp deploy/renewise-api.service /etc/systemd/system/

# Edit paths/user to match your server (default assumes /opt/renewise)
sudo nano /etc/systemd/system/renewise-bot.service
sudo nano /etc/systemd/system/renewise-api.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable renewise-bot renewise-api
sudo systemctl start renewise-bot renewise-api

# Check status
sudo systemctl status renewise-bot renewise-api

# Follow logs
journalctl -u renewise-bot -f
journalctl -u renewise-api -f
```

Both units use `Restart=always` with a 5-second delay and a 5-restart-in-60-seconds burst limit, after which systemd stops trying and pages the admin.

### Health check

The Mini App API exposes an unauthenticated health endpoint:

```bash
curl -sf http://localhost:8000/healthz
# → {"status": "ok"}
```

Returns `200 {"status":"ok"}` when the process is alive and SQLite is reachable.
Returns `503 {"status":"error","detail":"..."}` if the DB connectivity check fails.

Wire this into any uptime monitor (UptimeRobot, Better Uptime, Grafana, etc.) to get alerted if the API goes down.

### SQLite concurrency note

See the note in `config.py` — WAL mode is enabled, but SQLite has a single-writer ceiling. Monitor your logs for `database is locked` errors under load; if they appear, migrating to PostgreSQL is the next step.

### Checking if processes are alive (no uptime monitor)

```bash
# Quick liveness check
systemctl is-active renewise-bot renewise-api

# Check bot process is polling (look for last Telegram API call in logs)
journalctl -u renewise-bot --since "5 minutes ago" | grep -i "polling\|telegram"
```

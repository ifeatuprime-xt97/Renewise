# ReneWise

Non-custodial subscription paywall bot for Telegram groups and channels. Members pay in TON (GRAM), admins receive payouts directly to their wallet  no middleman holds funds at any point.

See [`about.md`](about.md) for a product overview and [`DOCUMENT.md`](DOCUMENT.md) for the Terms of Service and Privacy Policy draft.

---

## How It Works

1. Admin adds the bot to their group/channel as admin and runs `/createpaywall`
2. Bot generates a unique private invite link for the group
3. Member requests to join → bot DMs them a payment link (QR code + `ton://` deep-link)
4. Member pays in TON → a per-subscription smart contract splits the payment atomically:
   - Admin receives their share directly to their payout wallet
   - Platform receives its fee
   - Any overpayment > $1 USD is held in the vault for automatic trustless refund
5. Bot detects the on-chain payment, approves the join request, and sends a welcome DM
6. Before expiry, bot sends a renewal reminder. If not renewed, member is removed automatically.

---

## Quick Start

### Prerequisites
- Python 3.11+
- Node.js 18+ (for contract compilation only)
- Two Telegram bot tokens from [@BotFather](https://t.me/BotFather)  one for the main bot, one for the superadmin bot
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
# Fill in all values  see Environment Variables below
```

### 4. Run everything
```bash
python run.py
```

This starts the main bot, superadmin bot, Mini App API, and in-process payment watcher in a single process. No Redis or separate workers needed for development or testnet.

---

## Bot Setup Checklist

1. Create the main bot via [@BotFather](https://t.me/BotFather), copy token to `BOT_TOKEN`
2. In BotFather → Bot Settings → Group Privacy → **Turn off**
3. Create a second bot for superadmin, copy its token to `SUPERADMIN_BOT_TOKEN`
4. Find your Telegram user ID (message [@userinfobot](https://t.me/userinfobot)) and add to `ALLOWED_SUPERADMIN_IDS`
5. Set up your trigger wallet: create a fresh wallet in Tonkeeper, fund with ~2 TON for gas, copy address → `TRIGGER_WALLET` and mnemonic → `TRIGGER_MNEMONIC`
6. Build the contract: `cd contracts && npm install && npm run build`
7. Start: `python run.py`
8. Add the main bot to your group/channel as admin with: **Invite Users**, **Manage Chat**, **Approve New Members**
9. DM the main bot `/createpaywall` to run the setup wizard

---

## Environment Variables

### Core
| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Main bot token from BotFather |
| `SUPERADMIN_BOT_TOKEN` | ✅ | Separate bot token for the superadmin control panel |
| `ALLOWED_SUPERADMIN_IDS` | ✅ | Comma-separated Telegram user IDs allowed to use superadmin bot |
| `DATABASE_PATH` | optional | SQLite path (default: `./data/renewise.db`) |
| `DATABASE_URL` | optional | PostgreSQL connection string  overrides `DATABASE_PATH` when set |

### TON Payments
| Variable | Required | Description |
|---|---|---|
| `PLATFORM_WALLET` | ✅ | TON address that receives platform fees (keep offline/cold) |
| `LOG_ADDRESS` | ✅ | TON address that receives `PaymentLog` messages for off-chain indexing |
| `TRIGGER_WALLET` | ✅ | Dedicated hot wallet address for sending `Refund{}` messages to vaults |
| `TRIGGER_MNEMONIC` | ✅ | 24-word mnemonic for the trigger wallet (NOT the platform wallet) |
| `BUYER_FEE_BPS` | optional | Buyer-side fee in basis points (default: `200` = 2.00%) |
| `ADMIN_FEE_BPS` | optional | Admin-side fee in basis points (default: `330` = 3.30%) |
| `OVERPAYMENT_REFUND_THRESHOLD_USD` | optional | Minimum overpayment to trigger refund flow (default: `1.00`) |

### Chain Watcher (TonCenter)
| Variable | Required | Description |
|---|---|---|
| `TONCENTER_API_KEYS` | recommended | Comma-separated TonCenter API keys for key rotation (avoids rate limits) |
| `TONCENTER_TESTNET` | optional | Set to `true` to use testnet (default: `false`) |
| `POLL_INTERVAL_SECONDS` | optional | How often to poll vaults for new transactions (default: `15`) |
| `MIN_CONFIRMATIONS` | optional | Confirmations before treating a tx as final (default: `1`) |

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

### Render keep-alive (free tier)
| Variable | Description |
|---|---|
| `KEEP_ALIVE` | Set to `false` to disable self-ping (default: `true` on Render) |
| `KEEP_ALIVE_URL` | Your Render service URL  set automatically via `RENDER_EXTERNAL_URL` |
| `KEEP_ALIVE_URLS` | Additional comma-separated URLs to ping (e.g. the Mini App) |
| `KEEP_ALIVE_INTERVAL` | Ping interval in seconds (default: `600`) |

---

## Smart Contract  PaymentVault

One vault contract is deployed per subscription (user × group pair), on first payment via TON's deploy-on-first-message pattern.

### Fee Model
```
Buyer sends:     price + buyer_fee + gas_reserve  (0.05 TON)
Admin receives:  price - admin_fee                (direct to payout wallet)
Platform gets:   admin_fee + buyer_fee            (to PLATFORM_WALLET)
Vault holds:     any overpayment above required   (for trustless refund)
```

### Overpayment Refund Flow
When a member sends more than the required amount:
1. Vault holds the excess in `overage_held` (on-chain state)
2. Watcher detects it and DMs the member asking for their wallet address
3. Member replies with their TON address (validated by the bot)
4. Trigger wallet sends `Refund{recipient}` to the vault
5. Vault verifies `sender == trigger_wallet` and releases `overage_held` to the member
6. Member receives their TON directly from the vault  no platform private key involved

**Security:** The trigger wallet holds only ~2 TON for gas. If compromised, an attacker can only trigger refunds the contract already validated  they cannot redirect funds or drain the platform wallet.

### Underpayment
If a member sends less than required, the contract rejects the message with exit code 400 and TON's bounce mechanism returns most of the funds automatically. The bot DMs the member explaining the shortfall.

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
│   └── Full platform control panel (see Superadmin Commands below)
├── Mini App API (FastAPI  uvicorn)
│   ├── Admin + subscriber endpoints
│   ├── Developer Platform API  (/api/platform/*, /api/developer/*)
│   └── Hosted checkout page    (/checkout/{charge_id})
└── In-process payment watcher (asyncio task)
    ├── Polls TonCenter every POLL_INTERVAL_SECONDS
    ├── Detects new vault transactions
    ├── Activates subscriptions in DB
    ├── Detects overpayments → triggers refund flow
    └── Calls bot handlers directly (no Redis needed in dev mode)
```

### Production Scaling
For production with high volume, run each component as a separate process:
```bash
# Bot
python -m renewise.bot

# RQ workers
python -m renewise.watcher.worker --concurrency 4

# Webhook server (TonCenter push)
python -m renewise.watcher.webhook

# Scheduler (renewal reminders + grace enforcement)
python -m renewise.watcher.scheduler

# Superadmin bot
python -m renewise.superadmin.bot

# Mini App API
uvicorn renewise.miniapp.server:app --host 0.0.0.0 --port 8000
```

---

## Mini App API

The Telegram Mini App is a FastAPI application. All endpoints require a valid Telegram `initData` Authorization header (`tma <initData>`) unless noted.

### Admin / Creator endpoints
| Endpoint | Description |
|---|---|
| `GET /api/my-groups` | Admin's groups with revenue summary |
| `GET /api/my-subscriptions` | Subscriber's active subscriptions |
| `GET /api/my-payment-history` | Subscriber's full payment history |
| `POST /api/groups/detect` | Detect recently admin-granted chats (wizard step) |
| `POST /api/groups/create` | Activate a new paywall |
| `GET /api/groups/{id}/detail` | Group detail  price, wallet, members, revenue |
| `GET /api/groups/{id}/payment-history` | Paginated payment history |
| `GET /api/groups/{id}/payments/{sub_id}` | Full payment detail with fee breakdown |
| `GET /api/groups/{id}/members` | Paginated member list |
| `PUT /api/groups/{id}/price` | Update subscription price |
| `PUT /api/groups/{id}/wallet` | Schedule a payout wallet change (24h delay) |
| `GET /api/groups/{id}/wallet-change-pending` | Active pending wallet change (if any) |
| `POST /api/groups/{id}/wallet-change/{change_id}/cancel` | Cancel a pending wallet change |
| `GET /api/groups/{id}/wallet-change-history` | Full wallet change history |
| `POST /api/groups/{id}/pause` | Pause the paywall |
| `POST /api/groups/{id}/resume` | Resume the paywall |
| `POST /api/groups/{id}/comp` | Comp a member (grant free access) |
| `DELETE /api/groups/{id}` | Delete a group and all subscriber records |
| `GET /api/search/tx?hash=` | Search subscriptions by TX hash (scoped to this admin) |

### Developer Platform endpoints
| Endpoint | Description |
|---|---|
| `GET /api/developer/platforms` | List the authenticated user's platforms |
| `POST /api/developer/platforms` | Create a new platform (generates test keys) |
| `GET /api/developer/platforms/{id}/charges` | Paginated charge list for a platform |
| `POST /api/developer/platforms/{id}/live-keys` | Generate live (mainnet) keys |
| `POST /api/developer/platforms/{id}/keys/regenerate` | Rotate test or live keys |
| `PUT /api/developer/platforms/{id}/wallet` | Update platform payout wallet |
| `POST /api/developer/platforms/{id}/passcode` | Set or change the wallet passcode |
| `POST /api/developer/platforms/{id}/webhook` | Set or update webhook URL |
| `POST /api/developer/platforms/{id}/webhook/rotate-secret` | Rotate webhook signing secret |
| `DELETE /api/developer/platforms/{id}` | Delete a platform and all its data |
| `GET /api/developer/charges` | All charges across all platforms (filterable by status) |
| `GET /api/developer/search?q=` | Search charges by external_reference or TX hash |

### External Developer API (Bearer token auth)
| Endpoint | Description |
|---|---|
| `POST /api/platform/charges` | Create a charge  returns `payment_url`, `vault_address`, `checkout_url` |
| `GET /api/platform/charges/{id}` | Get charge status and details |

### Public / unauthenticated
| Endpoint | Description |
|---|---|
| `GET /checkout/{charge_id}` | Hosted checkout page |
| `GET /api/public/checkout/{charge_id}` | Charge data for the checkout page |
| `GET /healthz` | Health check  `200 {"status":"ok"}` or `503` on DB failure |
| `GET /api/config` | Bot username for constructing deep-links |

---

## Project Structure

```
renewise/
├── bot.py                      # Main bot entry point
├── config.py                   # Environment variable loading
├── run.py                      # Single-process launcher (bot + watcher + API)
├── api/
│   └── platform.py             # External Developer API (Bearer token auth)
├── db/
│   ├── schema.py               # Table definitions + migrations (SQLite + PostgreSQL)
│   └── queries.py              # All async DB operations
├── handlers/
│   ├── admin_menu.py           # /menu + admin sub-flows (price, wallet, comp, etc.)
│   ├── chat_member.py          # Bot added/removed as admin
│   ├── create_paywall.py       # /createpaywall 9-step wizard
│   ├── join_request.py         # Member join request → payment flow
│   └── refund.py               # Overpayment wallet collection + refund trigger
├── miniapp/
│   ├── server.py               # FastAPI Mini App API (all /api/* endpoints)
│   ├── auth.py                 # Telegram initData HMAC verification
│   └── static/
│       ├── index.html          # Mini App frontend (creator + developer modes)
│       └── checkout.html       # Hosted checkout page (developer charges)
├── services/
│   ├── payment.py              # Payment request generation (subscription + platform)
│   ├── platform.py             # Platform CRUD and key management
│   └── wallet.py               # TON address format validation
├── superadmin/
│   ├── bot.py                  # Superadmin control panel bot
│   └── queries.py              # Superadmin-specific DB queries
├── ton/
│   ├── vault.py                # Off-chain address computation + payment link builder
│   ├── deploy.py               # Testnet deployment + split verification script
│   └── refund_trigger.py       # Sends Refund{} to vault via trigger wallet
├── utils/
│   ├── coingecko.py            # TON/USD price feed (CoinGecko with fallback)
│   └── keyboards.py            # InlineKeyboardMarkup builders
└── watcher/
    ├── inprocess_watcher.py    # Dev-mode in-process payment polling
    ├── bot_listener.py         # Redis pub/sub → bot actions bridge (production)
    ├── scheduler.py            # Renewal reminders + grace enforcement jobs
    ├── tasks.py                # RQ job definitions (production)
    ├── toncenter.py            # TonCenter API client with key rotation
    └── webhook.py              # TonCenter webhook receiver (production)

contracts/
├── PaymentVault.tact           # Smart contract source
├── tests/
│   └── PaymentVault.spec.ts    # Contract test suite
└── build/                      # Compiled contract (generated by npm run build)

deploy/
├── renewise-bot.service        # systemd unit  bot + watcher
└── renewise-api.service        # systemd unit  Mini App API
```

---

## Data Model

```sql
groups                  one row per Telegram group/channel
users                   one row per Telegram user
subscriptions           one row per (user, group) pair  status, vault address, renewal dates
vault_registry          vault_address → (subscription, user, group) mapping
processed_tx_hashes     idempotency table  prevents double-processing of transactions
overpayment_refunds     refund lifecycle: pending_wallet → pending_send → sent
admin_audit_log         append-only log of all admin and platform events
platform_config         global fee defaults + payments kill switch
pending_wallet_changes  delayed wallet updates (24h hold with cancel window)
platforms               developer API platforms (keys, wallet, webhook config)
platform_charges        individual charges created via the Developer API
webhook_endpoints       per-platform webhook configuration
webhook_deliveries      delivery log with retry tracking
platform_audit_log      per-platform audit trail
```

---

## Commands

### Main bot
| Command | Description |
|---|---|
| `/start` | Welcome screen; handles `renew_`, `reminders`, `support` deep-links |
| `/createpaywall` | Launch the paywall setup wizard (DM only) |
| `/menu` | Open the admin management menu (DM only) |

### Superadmin bot
| Command | Description |
|---|---|
| `/start` | Platform dashboard  stats, kill switch, trigger wallet status |
| `/overview` | Monthly GMV, fee revenue, active admins and subscribers |
| `/groups` | Paginated group directory |
| `/lookup <term>` | Search by TX hash or Telegram user ID |
| `/pendingrefunds` | Overpayment refund queue |
| `/txfeed` | Platform-wide transaction feed (filterable) |
| `/revenue` | Revenue breakdown (all-time, MoM, by status) |
| `/announce` | Broadcast to all users / admins / active subscribers |
| `/msgadmin <id> <text>` | DM an admin via the main bot |
| `/auditlog` | Recent platform audit events |
| `/killswitch` | Pause or resume all payment processing |
| `/help` | Command list |

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

## Production Deployment

The project has no external process manager dependency, but you **must** run the two core processes under something that restarts them automatically. A crashed bot means no subscription activations, no renewal enforcement, and no refunds until manually restarted.

### What runs where

| Process | Command | Restarts needed? |
|---|---|---|
| Bot + watcher | `python run.py` | ✅ Yes  critical |
| Mini App API | `uvicorn renewise.miniapp.server:app --host 0.0.0.0 --port 8000` | ✅ Yes  critical |

### systemd (recommended for Linux VPS)

Two unit files are provided in `deploy/`:

```bash
sudo cp deploy/renewise-bot.service /etc/systemd/system/
sudo cp deploy/renewise-api.service /etc/systemd/system/

# Edit paths/user to match your server (default assumes /opt/renewise)
sudo nano /etc/systemd/system/renewise-bot.service
sudo nano /etc/systemd/system/renewise-api.service

sudo systemctl daemon-reload
sudo systemctl enable renewise-bot renewise-api
sudo systemctl start renewise-bot renewise-api

# Check status / follow logs
sudo systemctl status renewise-bot renewise-api
journalctl -u renewise-bot -f
journalctl -u renewise-api -f
```

Both units use `Restart=always` with a 5-second restart delay.

### Health check

```bash
curl -sf http://localhost:8000/healthz
# → {"status": "ok"}
```

Returns `200 {"status":"ok"}` when the process is alive and the DB is reachable. Returns `503` if the DB check fails. Wire this into any uptime monitor.

### Render (free tier)

Render spins down free web services after 15 minutes with no inbound HTTP. Telegram polling is outbound and does not count, so `python run.py` self-pings `RENDER_EXTERNAL_URL/health` every 10 minutes to stay awake. `PORT` and `RENDER_EXTERNAL_URL` are set by Render automatically.

```bash
curl -sf https://YOUR-SERVICE.onrender.com/health
# → {"status":"ok","service":"renewise-bot"}
```

To also keep the Mini App API awake, set `KEEP_ALIVE_URLS=https://your-api.onrender.com/healthz`.

### SQLite concurrency

WAL mode is enabled by default. SQLite has a single-writer ceiling  monitor logs for `database is locked` errors under load. If they appear consistently, migrating to PostgreSQL (set `DATABASE_URL`) is the next step.

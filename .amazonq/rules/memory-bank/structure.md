# cosmogate - Project Structure

## Directory Organization

```
cosmogate/
├── .amazonq/rules/memory-bank/    # AI assistant documentation
├── contracts/                      # TON smart contracts (Tact language)
│   ├── lib/                        # Contract dependencies
│   ├── scripts/                    # Deployment/test scripts
│   ├── tests/                      # Contract tests
│   ├── PaymentVault.tact           # Main payment vault contract
│   ├── sandbox_runner.js           # Sandbox testing environment
│   └── tact.config.json            # Tact compiler configuration
├── cosmogate/                      # Main bot application
│   ├── db/                         # Database layer
│   ├── handlers/                   # Telegram event handlers
│   ├── services/                   # Business logic services
│   ├── ton/                        # TON blockchain integration
│   ├── utils/                      # Shared utilities
│   └── watcher/                    # Blockchain monitoring service
├── scripts/                        # Utility scripts
├── superadmin/                     # Admin dashboard (separate bot)
└── tests/                          # Integration/load tests
```

## Core Components

### cosmogate/ - Main Bot Application

**bot.py** - Entry point that wires all handlers and starts the bot

**config.py** - Environment variable loading and validation

**db/** - Database Layer
- `schema.py`: SQLite table definitions and initialization
- `queries.py`: All async database operations (users, groups, subscriptions, audit logs)

**handlers/** - Telegram Event Handlers
- `admin_menu.py`: `/menu` command + Stats/Price/Wallet/Pause/Comp/Members management
- `chat_member.py`: Bot added/removed detection in groups
- `create_paywall.py`: `/createpaywall` 5-step wizard implementation
- `join_request.py`: Member join request processing + payment verification

**services/** - Business Logic Layer
- `payment.py`: Payment request generation and status checking (STUBBED for Phase 2)
- `wallet.py`: TON address format validation with CRC16 checksum

**utils/** - Shared Utilities
- `keyboards.py`: InlineKeyboardMarkup builders for all bot interactions

**ton/** - TON Blockchain Integration
- `vault.py`: PaymentVault contract wrapper and deployment utilities
- `deploy.py`: Contract deployment scripts for testnet/mainnet
- `tests/`: Unit tests for vault operations

**watcher/** - Blockchain Monitoring Service
- `config.py`: Watcher-specific configuration
- `db.py`: Separate database connection for watcher
- `tasks.py`: Async tasks for payment monitoring
- `scheduler.py`: APScheduler job management
- `toncenter.py`: TonCenter API client for blockchain queries
- `webhook.py`: Webhook endpoint for real-time transaction notifications
- `worker.py`: Redis Queue (RQ) worker process
- `bot_listener.py`: Bot event listener integration
- `recheck.py`: Periodic subscription verification logic

### contracts/ - Smart Contracts

**PaymentVault.tact** - Tact smart contract for payment vault
- Manages payment channels between users and group admins
- Stores subscription states on-chain
- Handles payment verification and release

**Sandbox Testing**
- `sandbox_runner.js`: Node.js script for running contract tests in TON sandbox
- `tact.config.json`: Compiler settings for Tact contracts

### superadmin/ - Admin Dashboard (Separate Bot)

**Purpose**: Platform administration bot for managing multiple cosmogate instances

- `handlers/`: Admin/transaction management
- `config.py`: Superadmin configuration
- `middleware.py`: Authentication middleware

### scripts/ - Utility Scripts

**generate_mnemonic.py** - Generate 24-word TON mnemonic for testnet deployment

## Architectural Patterns

### Layered Architecture
```
Presentation (Handlers) → Business Logic (Services) → Data (DB) → External (TON Chain)
```

### Dependency Injection
- Services are imported by handlers, never vice versa
- Database layer is isolated, accessed only via queries.py
- External APIs (TonCenter) wrapped in separate clients

### Stub Pattern
Phase 1 uses stub implementations in `services/payment.py`:
- Clean interfaces defined upfront
- Mock implementations for testing bot UX
- Easy swap-in for Phase 2 real implementations

### Event-Driven Design
- Telegram handlers react to user events
- Watcher service reacts to blockchain events
- Webhooks trigger async payment processing

## Data Flow

### Subscription Creation Flow
```
Admin → /createpaywall → create_paywall.py → db/queries.py → SQLite
                                ↓
                        services/wallet.py (validate address)
```

### Member Join Flow
```
User joins → join_request.py → services/payment.py (stub) → User pays (Phase 2)
                   ↓                                    ↓
              db/queries.py ← ← ← ← ← ← ← ← ← ← on-chain tx confirmed
                   ↓
            Approve join request
```

### Payment Verification Flow (Phase 2)
```
watcher/tasks.py → toncenter.py → TON Blockchain
         ↓
    db/queries.py (update subscription)
         ↓
    bot_listener.py (approve join request)
```

## Database Schema

**groups**: Telegram groups with paywalls configured
- Stores: group_id, admin_user_id, wallet_address, price_grams, duration_days, is_active

**users**: All users who interact with the bot
- Stores: user_id, username, created_at

**subscriptions**: User-group subscription pairs
- Stores: status (pending/active/expired), payment_tx_hash, expires_at, frozen_at

**admin_audit_log**: Immutable audit trail
- Stores: action_type, actor_user_id, details_json, created_at

## File Relationships

- All handlers import from `utils/keyboards.py` for UI consistency
- All handlers import from `db/queries.py` for data access
- Services are stateless, receive data from handlers
- Config is centralized in `config.py`, imported everywhere
- Watcher operates independently, shares only `db/queries.py` schema

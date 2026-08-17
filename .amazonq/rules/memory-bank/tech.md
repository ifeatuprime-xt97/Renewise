# cosmogate - Technology Stack

## Core Technologies

### Runtime & Languages
- **Python 3.11+**: Main application runtime
- **JavaScript/Node.js**: Smart contract testing (TON sandbox)
- **Tact**: TON blockchain smart contract language (TypeScript-like syntax)

### Bot Framework
- **python-telegram-bot v21.3**: Async Telegram Bot API wrapper
- Supports both polling and webhook modes
- FSM (Finite State Machine) for conversation flows

### Database
- **SQLite**: Lightweight embedded database
- **aiosqlite v0.20.0**: Async SQLite driver
- Single-file database (`cosmogate.db`)
- No separate database server required

### Blockchain Integration
- **TON Blockchain**: The Open Network
- **pytoniq v0.1.39**: TON Python SDK for bot integration
- **pytoniq-core v0.1.36**: Core TON utilities (mnemonic, crypto)
- **TonCenter API**: HTTP API for TON blockchain queries

### Job Queue & Scheduling
- **Redis v5.0.7**: In-memory data store for job queues
- **RQ (Redis Queue) v1.16.2**: Python job queue for watcher workers
- **APScheduler v3.10.4**: Background task scheduler

### HTTP Client
- **aiohttp v3.9.5**: Async HTTP client for webhook endpoints and API calls

## Development Dependencies

### Environment Management
- **python-dotenv v1.0.1**: Load environment variables from `.env` files

### Smart Contract Development
- **TON Sandbox**: Local blockchain simulation for testing
- **Tact Compiler**: Smart contract compilation to TON bytecode

## Project Configuration

### Environment Variables

**Bot Service**
- `BOT_TOKEN` (required): Telegram bot token from BotFather
- `DATABASE_PATH` (optional): SQLite file path (default: `./data/cosmogate.db`)

**Watcher Service**
- `WATCHER_REDIS_URL`: Redis connection URL
- `WATCHER_TONCENTER_API_KEY`: TonCenter API authentication
- `WATCHER_WEBHOOK_SECRET`: Webhook endpoint authentication

**TON Deployment**
- `DEPLOYER_MNEMONIC`: 24-word mnemonic for contract deployment

## Build & Run Commands

### Installation
```bash
pip install -r requirements.txt
```

### Run Bot
```bash
python -m cosmogate.bot
```

### Run Watcher
```bash
python -m cosmogate.watcher.worker
```

### Generate TON Wallet
```bash
python scripts/generate_mnemonic.py
```

### Deploy Smart Contract (Testnet)
```bash
python -m cosmogate.ton.deploy --network testnet
```

### Test Smart Contracts
```bash
cd contracts
npm install
node sandbox_runner.js
```

## Dependency Management

### Python Dependencies (requirements.txt)
- Minimal dependencies by design
- Explicit version pinning for reproducibility
- Separated by deployment phase (bot, watcher, contracts)

### Node.js Dependencies (contracts/package.json)
- Required only for contract testing
- TON sandbox and testing utilities
- Not needed for bot runtime

## Architecture Decisions

### Why SQLite?
- **Simplicity**: Single-file database, no server to manage
- **Adequate**: Expected load is low (Telegram groups, not high-frequency trading)
- **Async**: aiosqlite provides non-blocking I/O for bot performance
- **Portable**: Easy backup, migration, and development setup

### Why python-telegram-bot?
- **Mature**: Most maintained Python Telegram library
- **Async**: Native async/await support for handling concurrent requests
- **FSM**: Built-in state machine for wizard flows (`/createpaywall`)
- **Well-documented**: Extensive examples and community support

### Why Separate Watcher Service?
- **Scalability**: Can run on separate machine from bot
- **Reliability**: Bot restart doesn't interrupt payment monitoring
- **Isolation**: Blockchain polling doesn't block bot message handling
- **Flexibility**: Multiple watchers for different networks (testnet/mainnet)

### Why Tact for Smart Contracts?
- **Developer-friendly**: TypeScript-like syntax, easier than FunC
- **Safety**: Built-in protections against common TON pitfalls
- **Modern**: Designed for TON's unique actor model
- **Tooling**: Good IDE support and debugging tools

## Version Requirements

- **Minimum Python**: 3.11 (for async improvements and type hints)
- **Minimum Node.js**: 16.x (for TON sandbox)
- **Telegram Bot API**: 7.0+ (for join request support)

## Testing Infrastructure

### Python Tests
- Location: `tests/` directory
- Framework: unittest (standard library)
- Load testing: `tests/load_test.py`

### Smart Contract Tests
- Location: `contracts/tests/`
- Framework: TON Sandbox
- Runner: `contracts/sandbox_runner.js`

## Deployment Topology

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Telegram Bot   │────▶│   Watcher        │────▶│  TON Blockchain │
│  (Python)       │     │   (Python + RQ)  │     │                 │
└────────┬────────┘     └────────┬─────────┘     └─────────────────┘
         │                       │
         ▼                       ▼
    ┌─────────┐            ┌──────────┐
    │ SQLite  │            │  Redis   │
    └─────────┘            └──────────┘
```

## Security Considerations

- **Bot tokens**: Stored in `.env`, never committed to git
- **Mnemonics**: 24-word seed phrases for TON wallets (offline generation)
- **Checksum validation**: All TON addresses validated before use
- **Audit logging**: All admin actions logged immutably
- **Non-custodial**: Bot never holds user funds, only facilitates direct payments

# cosmogate - Development Guidelines

## Code Quality Standards

### Python Style Conventions
- **Imports**: Group imports in order: standard library, third-party, local modules (separated by blank lines)
- **Type hints**: Use Python 3.11+ type hints with `from __future__ import annotations` at module top
- **String quotes**: Prefer double quotes for strings; use single quotes inside f-strings when needed
- **Line length**: Follow PEP 8 with max 100-120 characters per line
- **Variable naming**: snake_case for variables/functions, PascalCase for classes, UPPER_CASE for constants
- **Private functions**: Prefix with underscore (`_helper_function`) to indicate internal use

### Module Structure Pattern
```python
"""Module docstring — purpose and key design points."""
from __future__ import annotations

# Standard library imports
import asyncio
import logging
from pathlib import Path

# Third-party imports
from pytoniq_core import Address, Cell, begin_cell

# Local imports
from cosmogate.config import BOT_TOKEN
from cosmogate.db import queries

# Constants (UPPER_CASE)
MIN_GAS_RESERVE_NANO: int = 50_000_000
PAY_OPCODE: int = 0x9A15B153

# Classes/functions
```

### Documentation Standards
- **Module docstrings**: Triple-quote at top with purpose, key points, and usage examples
- **Function docstrings**: Brief description + Args/Returns for complex functions; skip for trivial ones
- **Inline comments**: Use sparingly; prefer self-documenting code with clear variable names
- **Section headers**: Use Unicode box-drawing characters (─ ═ ═) to separate logical blocks

Example:
```python
# ── Cell builders ─────────────────────────────────────────────────────────────

def _build_data_cell(p: VaultParams) -> Cell:
    """Build the contract's init data cell."""
    return (
        begin_cell()
        .store_address(p.admin_wallet)
        .store_uint(p.buyer_fee_bps, 16)
        .end_cell()
    )
```

## Architectural Patterns

### Layered Architecture
Strict separation of concerns across layers:
```
Handlers (presentation) → Services (business logic) → DB Queries (data) → External APIs
```

Files in each layer never import from layers above:
- `handlers/` → `services/`, `db/`, `utils/`
- `services/` → `db/`, external clients
- `db/` → only aiosqlite and standard library
- `utils/` → no project dependencies (pure utilities)

### Dataclass Pattern for Configuration
Use frozen dataclasses for immutable parameter bundles:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class VaultParams:
    admin_wallet: Address
    platform_wallet: Address
    price: int  # nanoTON
    buyer_fee_bps: int  # basis points
    subscription_id: int
```

### Conversation Handler Pattern
Multi-step wizard flows use python-telegram-bot's ConversationHandler:

```python
from telegram.ext import ConversationHandler, MessageHandler, filters

# Conversation states
AWAIT_INPUT, AWAIT_CONFIRM = range(2)

def build_wizard_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", start_wizard)],
        states={
            AWAIT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
            AWAIT_CONFIRM: [CallbackQueryHandler(handle_confirm, pattern="^confirm:")],
        },
        fallbacks=[CommandHandler("cancel", cancel_wizard)],
        per_chat=True,
        per_user=True,
    )
```

### Async-First Design
All I/O operations are async:
- Database: `aiosqlite` with `async with` context managers
- HTTP: `aiohttp` for async web requests
- Telegram API: All bot handlers are `async def`

```python
async def get_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE id=?", (user_id,))
        return await cur.fetchone()
```

### Error Handling Pattern
- **Fail fast**: Check preconditions early with clear error messages
- **Graceful degradation**: Catch exceptions at handler boundaries, log, and show user-friendly errors
- **Audit logging**: Record all admin actions in immutable audit log before performing them

```python
async def _require_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Fetch admin's group; send error and return None if not found."""
    group = await _get_admin_group(update.effective_user.id)
    if not group:
        await update.message.reply_text(
            "⚠️ No active groups. Use /createpaywall first."
        )
    return group
```

## Common Implementation Patterns

### Telegram Bot Handler Pattern
```python
async def cmd_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /command or callback."""
    # 1. Validate (check DM, permissions, data)
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please use this in a DM.")
        return
    
    # 2. Fetch data
    group = await _require_group(update, ctx)
    if not group:
        return
    
    # 3. Perform action
    await queries.update_group(group["id"], ...)
    await queries.audit(group["id"], "action_name", user_id, {...})
    
    # 4. Respond
    await update.message.reply_text("✅ Done.", reply_markup=keyboard())
```

### Database Query Pattern
- Use `aiosqlite.Row` factory for dict-like access
- Return typed dicts or None (not raw tuples)
- Keep SQL queries in `db/queries.py`, not scattered in handlers

```python
async def get_group_by_id(group_id: int) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM groups WHERE id=?", (group_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None
```

### Test Organization Pattern
Tests split into pure Python (fast) and integration (requires external dependencies):

```python
import pytest

# Skip marker for slow/integration tests
tvm = pytest.mark.skipif(
    not (_node_available() and _contract_built()),
    reason="Requires Node.js and compiled contract",
)

class TestPurePython:
    """Fast tests with no external dependencies."""
    def test_fee_arithmetic(self):
        assert expected_admin_amount(1_000_000_000, 330) == 967_000_000

@tvm
class TestTvmExecution:
    """Tests requiring TON Virtual Machine."""
    def test_payment_flow(self):
        result = _run_sandbox({...})
        assert result["success"]
```

### Idempotency Pattern for Background Tasks
All background tasks must be idempotent with atomic check-and-set:

```python
def process_payment(vault_address: str, tx_hash: str, amount: int) -> None:
    """Idempotent payment processing task."""
    async def _run() -> None:
        # Check-then-act with database constraint
        if await is_tx_processed(tx_hash):
            return  # Already done
        
        # Atomic claim (INSERT OR IGNORE)
        claimed = await mark_tx_processed(tx_hash, subscription_id)
        if not claimed:
            return  # Lost race to another worker
        
        # Safe to proceed with side effects
        await activate_subscription(...)
    
    asyncio.run(_run())
```

### Retry with Exponential Backoff
```python
from rq import Retry

def _retry() -> Retry:
    delays = [BASE_DELAY * (2 ** i) for i in range(MAX_RETRIES)]
    return Retry(max=MAX_RETRIES, interval=delays)
```

## Code Organization Rules

### File Naming
- Modules: snake_case (`admin_menu.py`, `join_request.py`)
- Tests: `test_<module>.py` pattern
- Scripts: descriptive snake_case (`generate_mnemonic.py`)

### Function Organization Within Files
1. Constants and configuration
2. Helper functions (private, prefixed with `_`)
3. Public functions (alphabetical or by call order)
4. Handler builders (for ConversationHandlers)

### Import Order
Standard order enforced across codebase:
```python
# 1. Future annotations
from __future__ import annotations

# 2. Standard library
import asyncio
import logging
from pathlib import Path

# 3. Third-party packages
from pytoniq_core import Address, Cell
from telegram import Update

# 4. Local modules
from cosmogate.config import BOT_TOKEN
from cosmogate.db import queries
```

## Testing Guidelines

### Test Naming Convention
- Test classes: `Test<Feature>` (e.g., `TestFeeArithmetic`, `TestCellLayout`)
- Test methods: `test_<scenario>` (e.g., `test_standard_split_1_ton`)
- Use `@pytest.mark.parametrize` for multiple test cases

### Test Organization
```python
# ═════════════════════════════════════════════════════════════════════════
# Group A — Pure Python tests
# ═════════════════════════════════════════════════════════════════════════

class TestFeeArithmetic:
    """Verify fee split math matches the contract's integer arithmetic."""
    
    def test_standard_split(self):
        ...
```

### Test Helpers
Extract common test utilities:
```python
def _params(price: int = ONE_TON, buyer_fee_bps: int = 200) -> VaultParams:
    """Factory for test VaultParams with sensible defaults."""
    return VaultParams(
        admin_wallet=_ADMIN,
        platform_wallet=_PLATFORM,
        price=price,
        buyer_fee_bps=buyer_fee_bps,
        ...
    )
```

## Security Best Practices

### Input Validation
Always validate user input before processing:
```python
async def msg_new_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        price = float(text)
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Send a positive number.")
        return AWAIT_NEW_PRICE
```

### Secret Management
- Never commit secrets to git (`.env` is in `.gitignore`)
- Use `python-dotenv` to load environment variables
- Access via `os.environ["KEY"]` for required, `os.getenv("KEY")` for optional

### Audit Logging
Log all admin actions immutably:
```python
await queries.audit(
    group_id=group["id"],
    action="price_updated",
    actor_id=update.effective_user.id,
    details={"new_price": price},
)
```

## Common Code Idioms

### Keyboard Builder Pattern
```python
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="menu:stats")],
        [InlineKeyboardButton("💰 Update Price", callback_data="menu:update_price")],
        [InlineKeyboardButton("◀️ Back", callback_data="menu:back")],
    ])
```

### Context Data Storage
Use `ctx.user_data` for conversation state:
```python
ctx.user_data["price_group"] = group  # Store
group = ctx.user_data.get("price_group")  # Retrieve
ctx.user_data.pop("price_group", None)  # Cleanup
```

### Callback Data Format
Structured callback data with colon separator:
```python
# Pattern: "category:action:arg1:arg2"
callback_data=f"member:view:{user_id}:{group_id}"

# Parsing
_, action, uid_str, gid_str = query.data.split(":")
```

### Async Context Manager for DB
```python
async with aiosqlite.connect(DATABASE_PATH) as db:
    db.row_factory = aiosqlite.Row
    cur = await db.execute(query, params)
    result = await cur.fetchone()
```

## Performance Considerations

### Database Connection Management
- Create fresh connections per operation (SQLite handles lightweight connections well)
- Use `db.row_factory = aiosqlite.Row` for dict-like access
- Commit transactions explicitly: `await db.commit()`

### Redis Pub/Sub for Bot Communication
Background workers publish actions; bot process subscribes:
```python
# Publisher (worker)
r.publish(BOT_ACTIONS_CHANNEL, json.dumps({"action": "approve", ...}))

# Subscriber (bot)
pubsub = r.pubsub()
pubsub.subscribe(BOT_ACTIONS_CHANNEL)
for message in pubsub.listen():
    if message["type"] == "message":
        handle_bot_action(json.loads(message["data"]))
```

### Pagination Pattern
```python
MEMBERS_PAGE_SIZE = 5

async def get_members_page(group_id: int, offset: int, limit: int) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM subscriptions WHERE group_id=? LIMIT ? OFFSET ?",
            (group_id, limit, offset),
        )
        return [dict(row) for row in await cur.fetchall()]
```

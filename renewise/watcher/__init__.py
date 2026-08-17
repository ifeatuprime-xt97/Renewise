"""
renewise.watcher — Phase 3 chain-watcher and subscription lifecycle manager.

Components
──────────
webhook.py      — aiohttp server: receives TonCenter webhooks + polling fallback
worker.py       — RQ worker process(es): consume the job queue
scheduler.py    — APScheduler: renewal reminders + grace enforcement cron jobs
bot_listener.py — Redis pub/sub listener: executes Telegram API calls
recheck.py      — Manual recheck for support cases
tasks.py        — RQ task definitions (the actual business logic)
db.py           — Additional DB tables (vault_registry, processed_tx_hashes, etc.)
toncenter.py    — TonCenter HTTP client
config.py       — All watcher config from environment variables
"""

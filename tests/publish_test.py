"""
test_rate_stale_alert.py

Publishes a synthetic rate_stale_alert to the BOT_ACTIONS_CHANNEL Redis
channel to verify the full pipeline:

  coingecko.py --> Redis pub --> bot_listener.py --> Telegram DM

Run with the bot process already running:
    python test_rate_stale_alert.py

You should receive a Telegram DM from the superadmin bot within a few seconds.
"""
import json
import os
import sys
from dotenv import load_dotenv

load_dotenv()

try:
    import redis
except ImportError:
    print("ERROR: redis package not installed. Run: pip install redis")
    sys.exit(1)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CHANNEL   = os.getenv("BOT_ACTIONS_CHANNEL", "renewise:bot_actions")

payload = {
    "action":        "rate_stale_alert",
    "hours_old":     3.7,
    "current_price": 5.0,
}

r = redis.from_url(REDIS_URL)
recipients = r.publish(CHANNEL, json.dumps(payload))

print(f"Published rate_stale_alert to channel '{CHANNEL}'")
print(f"  payload: {json.dumps(payload, indent=2)}")
print(f"  Redis subscriber count: {recipients}")

if recipients == 0:
    print("\n⚠️  No subscribers received the message.")
    print("   Is 'python -m renewise.bot' running? bot_listener starts there.")
else:
    print(f"\n✅ Message delivered to {recipients} subscriber(s).")
    print("   Check your Telegram — you should have a DM from the superadmin bot.")

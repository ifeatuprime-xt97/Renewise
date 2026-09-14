"""Webhook receiver integration check for the testapps.
Starts the Python app (full mode, receiver on :4555) in the background,
then sends a correctly-signed and a tampered event, and verifies the
receiver accepts/rejects respectively.
Run:  python tests/_test_webhook_receivers.py
"""
import hashlib
import hmac
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "testapps/python")

def post(port, payload: bytes, sig: str) -> int:
    req = urllib.request.Request(
        f"http://localhost:{port}/webhook", data=payload, method="POST",
        headers={"Content-Type": "application/json", "Renewise-Signature": sig},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code

def main():
    app = subprocess.Popen(
        [sys.executable, "testapps/python/app.py"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        time.sleep(2.0)  # wait for receiver to bind

        payload = json.dumps({
            "id": 42, "external_reference": "order_1234", "status": "completed",
            "amount_usd_cents": 999, "tx_hash": "abc123", "mode": "live",
        }).encode("utf-8")
        secret = "whsec_mocksecret"  # matches testapps/.env written during smoke test
        good = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        ok = True
        code = post(4555, payload, good)
        print(f"signed event   -> HTTP {code} (expect 200)")
        ok &= code == 200

        code = post(4555, payload, "0" * 64)
        print(f"tampered event -> HTTP {code} (expect 401)")
        ok &= code == 401

        print("WEBHOOK_RECEIVER_TEST:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        app.terminate()

if __name__ == "__main__":
    sys.exit(main())

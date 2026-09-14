"""Throwaway mock of POST/GET /api/platform/charges for smoke-testing testapps/.
Run:  python tests/_mock_platform_api.py   (listens on :8000)
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHARGES = {}

class H(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/api/platform/charges":
            return self._send(404, {"detail": "Not Found"})
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer sk_live_good"):
            return self._send(401, {"detail": "Invalid API key"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        ref = body.get("external_reference")
        if any(c["external_reference"] == ref for c in CHARGES.values()):
            return self._send(409, {"detail": "Charge with this external_reference already exists"})
        cid = max(CHARGES, default=0) + 1
        CHARGES[cid] = {
            "id": cid, "external_reference": ref, "amount_usd_cents": body["amount_usd_cents"],
            "required_nano": 24680000000, "vault_address": "EQDmockvault123",
            "payment_url": "ton://transfer/EQDmockvault123?amount=24.68",
            "status": "pending", "checkout_url": f"http://localhost:8000/checkout/{cid}",
            "tx_hash": None, "created_at": "2026-09-14T18:00:00Z", "completed_at": None,
        }
        self._send(200, CHARGES[cid])

    def do_GET(self):
        if not self.path.startswith("/api/platform/charges/"):
            return self._send(404, {"detail": "Not Found"})
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return self._send(401, {"detail": "Missing or invalid Authorization header"})
        if not auth.startswith("Bearer sk_live_good"):
            return self._send(401, {"detail": "Invalid API key"})
        try:
            cid = int(self.path.rsplit("/", 1)[1])
        except ValueError:
            return self._send(404, {"detail": "Charge not found"})
        if cid not in CHARGES:
            return self._send(404, {"detail": "Charge not found"})
        self._send(200, CHARGES[cid])

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8000), H).serve_forever()

# Renewise Payments API — Developer Integration Guide

## Overview

Renewise lets you accept **TON cryptocurrency payments** from your own app or service by issuing a charge request against the Renewise vault infrastructure. Your users pay in TON; you receive value in USD-equivalent terms tracked per charge. This guide covers everything you need to go live.

---

## 1. Getting Your API Keys

Open the Renewise Telegram Mini App, navigate to **Developer →** select your platform **→ Dashboard**.

You have two independent key pairs:

| Key | Prefix | Environment |
|-----|--------|-------------|
| Publishable key (live) | `pk_live_…` | Mainnet (real TON) |
| Secret key (live) | `sk_live_…` | Mainnet |
| Publishable key (test) | `pk_test_…` | Testnet (free test TON) |
| Secret key (test) | `sk_test_…` | Testnet |

> **Keep secret keys private.** Never expose them in client-side code, mobile apps, or public repositories. Rotate immediately if compromised via the Dashboard → **Regenerate**.

---

## 2. Authentication

All API calls must include your **secret key** in the `Authorization` header:

```http
Authorization: Bearer sk_live_<your_secret_key>
```

The environment (live vs. test) is inferred automatically from the key prefix. A `sk_live_` key always routes to mainnet; a `sk_test_` key always routes to testnet. Mixing them returns `403 Forbidden`.

---

## 3. Creating a Charge

**`POST /api/platform/charges`**

Creates a new payment request and returns a TON address and amount your user must pay.

### Request

```http
POST /api/platform/charges
Authorization: Bearer sk_live_…
Content-Type: application/json

{
  "amount_usd": 9.99,
  "external_reference": "order_1234",
  "expires_in": 900
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `amount_usd` | float | ✅ | Charge amount in USD (min `0.10`) |
| `external_reference` | string | ✅ | Your internal order/user ID. Max 255 chars. Must be unique per platform. |
| `expires_in` | integer | ❌ | Seconds until the charge expires. Default: `900` (15 min). Range: 60–3600. |

### Response `200 OK`

```json
{
  "charge_id": "ch_abc123",
  "vault_address": "EQD…",
  "required_nano_amount": 24680000000,
  "amount_usd": 9.99,
  "mode": "live",
  "expires_at": "2025-09-01T12:15:00Z"
}
```

| Field | Description |
|-------|-------------|
| `vault_address` | TON address your user must send TON to |
| `required_nano_amount` | Exact amount in nanoTON (1 TON = 1,000,000,000 nanoTON) |
| `expires_at` | ISO 8601 UTC timestamp after which the charge expires |

### Directing Your User to Pay

You must use the returned `payment_url` (a `ton://` deep-link) to process payments.

1. **Telegram Mini Apps (Mobile)**: Embed the `payment_url` behind a "Pay with TON" button.
2. **Desktop / Web**: Render the `payment_url` as a QR code for the user to scan with Tonkeeper or TonHub.

> [!WARNING]
> **DO NOT display the raw `vault_address` for users to manually copy/paste.**
> Renewise uses a "Deploy-on-first-message" architecture to atomically split funds on-chain. The deep-link contains a `StateInit` payload required to deploy the smart contract. If a user manually copies the raw address and sends TON from an exchange (like Binance or OKX), the contract will not deploy and the payment will get "stuck". While Renewise has a background trigger-wallet to retroactively deploy and recover these funds, it delays the payment confirmation.

> **Test mode**: Use the [Testnet Wallet](https://wallet.ton.org/?testnet=true) to send free test TON. Transactions appear on [testnet.tonscan.org](https://testnet.tonscan.org).

---

## 4. Checking Charge Status

**`GET /api/platform/charges/{charge_id}`**

Poll this endpoint or rely on webhooks (recommended).

### Response

```json
{
  "charge_id": "ch_abc123",
  "external_reference": "order_1234",
  "status": "completed",
  "amount_usd": 9.99,
  "mode": "live",
  "tx_hash": "abc…def",
  "created_at": "2025-09-01T12:00:00Z",
  "completed_at": "2025-09-01T12:07:34Z"
}
```

### Charge Statuses

| Status | Meaning |
|--------|---------|
| `pending` | Awaiting on-chain payment |
| `completed` | Payment confirmed on-chain ✅ |
| `expired` | Payment window elapsed without payment |
| `failed` | Payment received but invalid (wrong amount, wrong chain) |

---

## 5. Webhooks (Recommended)

Instead of polling, configure a webhook to receive real-time charge events.

### Setting Up

In the Dashboard → **Webhook** → **Edit**, enter your HTTPS endpoint URL. You will receive a `whsec_…` signature secret — **save it immediately**, it is only shown once.

### Payload

Renewise sends an HTTP `POST` to your endpoint with:

```http
POST /your-webhook-handler
Content-Type: application/json
Renewise-Signature: <hex_signature>

{
  "id": 42,
  "external_reference": "order_1234",
  "status": "completed",
  "amount_usd_cents": 999,
  "tx_hash": "abc…def"
}
```

### Verifying the Signature

Always verify the `Renewise-Signature` header before processing the event:

```python
import hmac, hashlib

def verify_renewise_signature(payload: bytes, header: str, secret: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

```javascript
const crypto = require("crypto");

function verifySignature(rawBody, header, secret) {
  const expected = crypto
    .createHmac("sha256", secret)
    .update(rawBody)
    .digest("hex");
  return crypto.timingSafeEqual(
    Buffer.from(expected),
    Buffer.from(header)
  );
}
```

> **Always use a constant-time comparison** (`hmac.compare_digest` / `timingSafeEqual`) to prevent timing attacks.

### Responding to Webhooks

Return `HTTP 200` within 5 seconds to acknowledge receipt. Any non-2xx response or timeout is recorded as a failed delivery and visible in your Dashboard → **Webhook → Recent Deliveries**.

---

## 6. Rotating Keys

If a secret key is compromised or you want to rotate:

1. Dashboard → **Dashboard** → **API Keys** → click **Regenerate** for the relevant key.
2. The old key is **immediately invalidated**.
3. Copy the new secret key from the one-time alert — it will not be shown again.
4. Update your server's environment variable.

---

## 7. Wallet & Passkey Setup

Before you can receive payouts, set a **payout wallet** in the Dashboard:

- **First time**: simply enter your TON wallet address — no passkey required.
- **Changing an existing wallet**: requires your **4-digit passkey**. Set it once via Dashboard → **Security → Set Passkey**.

> Your passkey is bcrypt-hashed and never stored in plaintext. If you lose it, contact support — there is no self-service reset.

---

## 8. Environment Isolation

| | Test | Live |
|-|------|------|
| Keys | `pk_test_` / `sk_test_` | `pk_live_` / `sk_live_` |
| TON Network | TON Testnet | TON Mainnet |
| Vault contract | Testnet deployment | Mainnet deployment |
| TonScan explorer | testnet.tonscan.org | tonscan.org |

A test-mode charge **never touches mainnet**. A live-mode charge **never touches testnet**. This is enforced at both the API authentication layer (key prefix check) and the database level (`CHECK(mode IN ('test','live'))`).

---

## 9. Error Reference

| HTTP Status | Error | Cause |
|-------------|-------|-------|
| `400` | `Bad Request` | Missing/invalid field in request body |
| `401` | `Unauthorized` | Missing or malformed `Authorization` header |
| `403` | `Forbidden` | Invalid secret key, wrong environment, or revoked platform |
| `404` | `Not Found` | Charge ID does not exist or belongs to another platform |
| `422` | `Unprocessable Entity` | Invalid TON address or passcode format |
| `429` | `Too Many Requests` | Rate limit exceeded (5–60 req/min depending on endpoint) |
| `500` | `Internal Server Error` | Server-side error — retry with exponential backoff |

---

## 10. Rate Limits

| Endpoint | Limit |
|----------|-------|
| `POST /charges` | 60 / minute |
| `GET /charges/{id}` | 60 / minute |
| `POST /keys/regenerate` | 5 / minute |
| `POST /passcode` | 5 / minute |
| `PUT /wallet` | 5 / minute |
| `POST /webhook` | 10 / minute |

Limits are applied **per platform** (keyed on IP). Exceeding them returns `429 Too Many Requests`.

---

## 11. Quick-Start Example (Python)

```python
import requests

BASE = "https://your-renewise-host.example.com"
SECRET = "sk_live_…"  # from env var, never hardcoded

def create_charge(amount_usd: float, order_id: str) -> dict:
    resp = requests.post(
        f"{BASE}/api/platform/charges",
        headers={"Authorization": f"Bearer {SECRET}"},
        json={
            "amount_usd": amount_usd,
            "external_reference": order_id,
            "expires_in": 900,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()

def get_charge(charge_id: str) -> dict:
    resp = requests.get(
        f"{BASE}/api/platform/charges/{charge_id}",
        headers={"Authorization": f"Bearer {SECRET}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()

# Usage
charge = create_charge(9.99, "order_1234")
print("Payment created!")
print(f"Direct link: {charge['payment_url']}")
print("(Render this URL as a QR code or embed it in a 'Pay' button)")
```

---

## 12. Quick-Start Example (Node.js)

```javascript
const fetch = require("node-fetch");

const BASE = "https://your-renewise-host.example.com";
const SECRET = process.env.RENEWISE_SECRET_KEY; // never hardcode

async function createCharge(amountUsd, orderId) {
  const res = await fetch(`${BASE}/api/platform/charges`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${SECRET}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      amount_usd: amountUsd,
      external_reference: orderId,
      expires_in: 900,
    }),
  });
  if (!res.ok) throw new Error(`Renewise API error: ${res.status}`);
  return res.json();
}

// Usage
const charge = await createCharge(9.99, "order_1234");
console.log("Payment created!");
console.log(`Direct link: ${charge.payment_url}`);
console.log("(Render this URL as a QR code or embed it in a 'Pay' button)");
```

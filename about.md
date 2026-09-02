# About ReneWise

ReneWise is a non-custodial subscription paywall platform for Telegram. It lets group and channel admins charge for membership in TON (GRAM), with payments settled directly on-chain  no middleman holds funds at any point.

---

## What it does

An admin runs `/createpaywall`, sets a USD price and billing interval, and links their payout wallet. ReneWise generates a private invite link for the group. When someone requests to join, the bot DMs them a QR code and a `ton://` deep-link. They pay from any TON wallet. The smart contract splits the payment atomically  admin's share goes straight to their wallet, the platform fee goes to the ReneWise platform wallet  and the bot approves the join request.

After that, ReneWise handles everything automatically: renewal reminders 3 days before expiry, a 3-day grace period, and automatic removal if payment isn't received. Overpayments above $1.00 USD are detected and refunded trustlessly to the subscriber's wallet.

---

## How payments work

Each subscription gets its own PaymentVault smart contract deployed on the TON blockchain. The contract address is computed deterministically from the subscription parameters  no network call needed. On the subscriber's first payment, the contract deploys and splits funds in the same transaction. On renewals, the same contract address is reused.

The split is encoded in the contract at deploy time and cannot be changed retroactively. ReneWise never has access to admin or subscriber funds. A completed on-chain split is final.

**Fees (defaults):**
- Buyer fee: 2.00% added on top of the subscription price
- Admin fee: 3.30% deducted from the admin's payout
- Combined platform take: ~5.30%

Per-group fee overrides are available via the Superadmin panel.

---

## Who it's for

**Group and channel admins** who want to monetise their Telegram community without relying on a payment processor or custodian. Works for any private group or channel  knowledge communities, investment groups, content channels, professional networks.

**Developers** who want to accept TON payments in their own Telegram bot or Mini App. The ReneWise Payments API (`sk_test_` / `sk_live_`) lets you create charges, get a hosted checkout page, and receive webhook events  no smart-contract knowledge required.

---

## Key features

- **Non-custodial**  funds go directly on-chain. ReneWise cannot freeze, reverse, or redirect a completed payment.
- **USD pricing, TON settlement**  admins set prices in USD. The TON equivalent is computed at payment time using a live CoinGecko rate and locked in on-chain.
- **Automatic lifecycle**  renewal reminders, grace periods, and subscriber removal all run without admin intervention.
- **Trustless overpayment refunds**  excess TON is held in the vault and released directly to the subscriber's wallet via a dedicated trigger wallet. The trigger wallet cannot redirect funds or drain any other balance.
- **Mini App dashboard**  admins manage groups, view payment history, update prices and wallets, and comp members through a Telegram Mini App. Developers manage API apps, keys, webhooks, and charges from the same interface.
- **Developer API**  create charges, get `payment_url` (full `ton://` deep-link with `StateInit`), poll status, and receive signed webhook events.
- **Superadmin controls**  platform-wide kill switch, per-group fee overrides, admin ban/unban, refund queue, TX lookup, and broadcast announcements.

---

## Tech stack

- **Bot:** Python, python-telegram-bot
- **API / Mini App backend:** FastAPI + asyncpg (PostgreSQL) or aiosqlite (SQLite for development)
- **Smart contract:** Tact (compiles to TON FunC), tested with Blueprint
- **Chain interaction:** pytoniq-core for off-chain address computation, TonCenter API for transaction polling
- **Frontend:** Vanilla JS Telegram Mini App (no framework)

---

## Security model

- All admin actions are logged to an append-only audit log.
- Wallet changes are delayed by 24 hours by default and can be cancelled within that window.
- The trigger wallet holds only ~2 TON for gas. If compromised, an attacker can only trigger refunds the contract already validated  they cannot redirect funds or steal from anyone.
- API secret keys are SHA-256 hashed before storage. Test secret keys are stored in plain text for development convenience and can be regenerated or the entire platform can be deleted from the Mini App.
- Mini App endpoints are authenticated via Telegram's `initData` HMAC mechanism and scoped strictly to the authenticated user's own data.

---

## Project status

ReneWise is a working, production-ready system. Testnet operation is fully supported; mainnet operation requires filling the legal placeholders in `DOCUMENT.md` before going live with real users. See the pre-launch checklist at the top of that file.

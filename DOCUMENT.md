# Renewise — Terms of Service & Privacy Policy (DRAFT)

> **This is a draft, not a finished legal document.** It reflects what Renewise actually does technically as of the current build. Have a qualified attorney review and adapt it — particularly the liability, dispute, and data-retention sections — before it governs real users and real money, and before relying on it in any jurisdiction with specific consumer-protection or data-protection requirements (e.g., GDPR in the EU/UK, or local payment-services regulation).

---

## ⚠️ PRE-LAUNCH LEGAL CHECKLIST — ALL PLACEHOLDERS MUST BE FILLED BEFORE GO-LIVE

The following items are the only things preventing this document from being legally complete. None of them can be filled by the engineering team — they require real business information and legal review. Complete every item before showing this document to real users or processing real money.

| # | Location | Placeholder | What you need to provide |
|---|---|---|---|
| 1 | ToS — header | `[DATE]` (line 9) | The actual effective date of the Terms, e.g. `August 12, 2026`. Set this when the document is finalized, not when the code ships. |
| 2 | Privacy Policy — header | `[DATE]` (line 116) | Same date as the ToS, or the date this Privacy Policy version takes effect. |
| 3 | ToS — Section 16 "Governing Law" | `[TO BE COMPLETED WITH LEGAL COUNSEL]` (line 106) | The jurisdiction whose law governs disputes (e.g. "the laws of England and Wales" or "the State of Delaware, USA"). Depends on where the operating entity is incorporated. Requires legal advice. |
| 4 | ToS — Section 17 "Contact" | `[CONTACT METHOD]` (line 110) | A real contact address for legal/ToS questions — typically a support email or Telegram handle (e.g. `support@renewise.app` or `@RenewiseSupport`). |
| 5 | Privacy Policy — Section 4 "Data Retention" | `[SPECIFIC RETENTION PERIODS TO BE DEFINED WITH LEGAL COUNSEL]` (line 161) | Concrete retention windows for each data category (e.g. "subscription records retained for 7 years", "audit logs for 2 years"). GDPR requires this to be specific. Requires legal advice. |
| 6 | Privacy Policy — Section 7 "Your Rights" | `[CONTACT METHOD]` (line 179) | Same as item 4 — a real contact point for data access/deletion requests. |
| 7 | Privacy Policy — Section 10 "Contact" | `[CONTACT METHOD]` (line 191) | Same as item 4. |

> **Items 3 and 5 are legally substantive** — they can't just be filled in; they require decisions about the operating entity and advice from a qualified lawyer familiar with your jurisdiction. Items 1, 2, 4, 6, 7 are factual fills that only need the correct date and a working contact address.

---


## TERMS OF SERVICE

**Last updated: August 13, 2026**

### 1. What Renewise Is

Renewise ("we," "us," "the Service") is a Telegram bot, companion Mini App, and TON blockchain smart-contract platform that allows administrators of private Telegram groups and channels ("Admins") to charge for membership access. Members ("Subscribers") pay in GRAM (the native currency of The Open Network / TON blockchain). Once a payment is confirmed on-chain, Renewise automatically approves the Subscriber's join request and manages their subscription lifecycle — including renewal reminders, re-billing, and removal on non-payment — without any manual action from the Admin.

### 2. Non-Custodial Architecture — Please Read Carefully

Renewise settles payments through a per-subscription smart contract ("PaymentVault") deployed on the TON blockchain. When a Subscriber pays:

- Their payment goes directly to the vault contract, which in the same on-chain transaction splits the funds between the Admin's designated wallet and Renewise's platform fee wallet.
- **Renewise never holds, controls, or has discretionary access to user or Admin funds.** The split is encoded in the contract at deployment time and cannot be changed retroactively.
- **We cannot reverse, refund, or redirect a completed on-chain split.** Once TON has been distributed from the vault to the Admin's wallet and Renewise's platform wallet, those transactions are final and immutable.
- **Overpayments are an exception.** If a Subscriber sends more than the required amount, the vault contract holds the excess in a ring-fenced `overage_held` balance. Renewise can trigger a `Refund{recipient}` message from a dedicated trigger wallet to release that overage to the Subscriber. See Section 5.

### 3. Fees

Renewise charges two fees per transaction, both disclosed to Admins during onboarding and to Subscribers before payment:

- **Buyer fee** — added on top of the subscription price; paid by the Subscriber. Default: 2.00% (200 basis points).
- **Admin fee** — deducted from the Admin's payout. Default: 3.30% (330 basis points).
- **Combined platform take:** approximately 5.30% on a standard transaction.

These defaults apply globally unless Renewise has negotiated a specific per-group override with an Admin. The applicable fees for any given group are always displayed before a Subscriber completes payment, and before an Admin activates a paywall.

Renewise reserves the right to change the global default fee rates, with the then-current rates always disclosed prior to any payment being requested. Per-group overrides are not affected by changes to the global default.

### 4. Pricing and Currency

Admins set prices in USD. The GRAM equivalent is calculated automatically at the time a payment link is generated, using a live exchange rate sourced from CoinGecko. The exact GRAM amount required is locked in at link-generation time and stored on-chain in the vault's initialisation data, so the Subscriber's required payment does not change after the link is issued.

Billing intervals are either **weekly** (7 days) or **monthly** (30 days), chosen by the Admin at paywall creation.

### 5. Overpayment and Refunds

If a Subscriber sends more than the required payment amount, the vault contract retains the overage in a dedicated `overage_held` balance — it is not distributed to the Admin or Renewise. Renewise will:

1. Detect the overpayment automatically (threshold: USD 1.00 by default; configurable).
2. DM the Subscriber asking for their TON wallet address.
3. Use a dedicated trigger wallet to send a `Refund{recipient}` instruction to the vault contract, which releases the held overage directly to the Subscriber's wallet.

This refund is trustless — the trigger wallet cannot redirect funds to itself or drain any other balance; it can only release what the vault has already computed and ring-fenced as overage. If the trigger wallet (`TRIGGER_MNEMONIC`) is not configured, the refund is queued for manual processing by Renewise's team within 24 hours.

Refunds are processed in TON and will be reduced by the on-chain gas cost of the `Refund{}` transaction (approximately 0.005 TON).

### 6. Subscription Lifecycle

- **Renewal reminders** are sent to Subscribers via bot DM **3 days before** their subscription expires (configurable via `REMINDER_WINDOW_DAYS`).
- After expiry, a **grace period** of **3 days** applies before Renewise removes the Subscriber from the group or channel (configurable via `GRACE_PERIOD_DAYS`). Comped subscribers are excluded from grace-period enforcement.
- If a Subscriber does not renew within the grace period, they are automatically removed (kicked and/or their join request is declined) and their subscription status is set to `expired`.
- Subscribers can renew at any time via the `/start renew_{group_id}` deep-link sent in the reminder.

### 7. Comped Access

Admins may grant free ("comped") access to specific Subscribers via the Admin Dashboard (`/menu → Comp a Member`). Comped subscriptions have no expiry, are excluded from renewal reminders and grace-period kicks, and are logged in the audit trail.

### 8. Admin Responsibilities

Admins are solely responsible for:
- The legality of the content, products, or services offered through their paywalled group or channel.
- Complying with Telegram's Terms of Service and applicable law in their jurisdiction.
- Providing accurate pricing and billing interval information.
- Providing a valid TON wallet address they control and that is capable of receiving GRAM.
- Ensuring their group or channel is set to **Private** and has **Join Approval** enabled before sharing the Renewise invite link; failure to do so will allow users to join without paying.

### 9. Required Bot Permissions

For groups, Renewise requires **Invite Users / Process Join Requests** and **Ban Users** (Manage Chat) admin permissions. For channels, **Manage Messages (Post Messages)** is additionally required. If these permissions are removed, the affected group is automatically frozen and the Admin is notified via DM.

### 10. Prohibited Use

Renewise may not be used to charge for access to content or services that are illegal, that facilitate fraud, that infringe intellectual property rights, or that violate Telegram's Terms of Service. Renewise reserves the right to suspend or ban any Admin's use of the Service, and to suspend any specific group or channel's paywall, at its discretion, including without prior notice in cases of suspected fraud, abuse, or illegal activity.

Superadmins of the Renewise platform may suspend groups, ban Admins, and trigger a platform-wide payment kill switch at any time for operational or compliance reasons.

### 11. Account Suspension and Termination

Renewise may suspend or terminate an Admin's or Subscriber's access for violation of these Terms, suspected fraudulent activity, or at our reasonable discretion. A banned Admin's groups are suspended automatically. Suspension of a paywall does not entitle any party to a refund of funds already settled on-chain, given the non-custodial, irreversible nature of the on-chain split.

### 12. Insufficient Payments

If a Subscriber sends less than the required amount to the vault, the payment is not processed and their subscription is not activated. The Subscriber is notified via DM with the shortfall amount and instructed to send a fresh payment for the full required amount. The underpayment transaction cannot be "topped up" — a new full payment is required.

### 13. No Warranty

The Service is provided "as is" without warranties of any kind, express or implied. Renewise does not warrant that the Service will be uninterrupted, error-free, or secure, and is not responsible for outages or issues arising from the TON blockchain network, TonCenter or other RPC/indexer providers, third-party wallet applications, Telegram's platform, or CoinGecko or other exchange-rate data providers.

### 14. Limitation of Liability

To the maximum extent permitted by law, Renewise's liability for any claim arising from use of the Service is limited to the fees actually collected by Renewise in connection with the specific transaction giving rise to the claim. Renewise is not liable for indirect, incidental, or consequential damages, including loss of funds due to user error, blockchain network issues, smart-contract bugs, or third-party wallet failures.

### 15. Changes to These Terms

We may update these Terms from time to time. Continued use of the Service after changes take effect constitutes acceptance of the updated Terms.

### 16. Governing Law

[TO BE COMPLETED WITH LEGAL COUNSEL — depends on where you incorporate/operate]

### 17. Contact

Questions about these Terms can be directed to [CONTACT METHOD].

---

## PRIVACY POLICY

**Last updated: August 13, 2026**

### 1. What We Collect

When you use Renewise as an Admin or Subscriber, we collect and store:

**All users:**
- Telegram user ID, first name, and username, as provided by Telegram at the time of first interaction.
- Subscription status, start date, next renewal date, and the transaction hash of each on-chain payment for each group you subscribe to.
- Timestamps of renewal reminders sent to you.

**Admins additionally:**
- The Telegram chat ID, title, and type (group or channel) of each group or channel you administer.
- Your configured subscription price (in USD cents and GRAM), billing interval, and payout wallet address.
- The invite link generated for your group.
- Your chosen buyer and admin fee configuration (global defaults unless overridden).
- A record of every administrative action you take through the bot or Mini App (e.g., price changes, wallet updates, comps, support messages), stored in an audit log.

**Derived / computed data:**
- The deterministic vault contract address for each (Subscriber, Group) pair, computed from on-chain parameters.
- The exact payment amount (in nanoTON) required at the time a payment link is generated.

We do **not** collect or have access to:
- Private keys or wallet seed phrases. Renewise never asks for or has technical access to these.
- The content of your Telegram messages outside of direct interactions with the Renewise bot.
- Any payment information beyond what is independently and publicly visible on the TON blockchain.

### 2. How We Use This Data

- **To operate the Service:** approving or declining group access, tracking subscription status, computing fee splits, sending renewal reminders, enforcing grace-period expirations, and processing overpayment refunds.
- **To maintain security:** detecting and preventing fraud or abuse, maintaining an internal audit log of all administrative and payment events, and enforcing Admin bans and platform kill-switch states.
- **To communicate with you:** sending bot DMs related to your subscriptions, your administered groups, payment confirmations, underpayment or overpayment notices, renewal reminders, and support requests you initiate.
- **To support Admins contacting us:** messages sent via the `/menu → Contact Support` flow are relayed to Renewise's platform operators via a separate Superadmin bot.

### 3. What We Share

We do not sell user data. We do not share user data with third parties except:

- **TonCenter** (or an equivalent TON RPC/indexer provider): to read publicly-available on-chain transaction data for payment verification. This is inherent to how the Service works.
- **CoinGecko** (or an equivalent price-feed provider): to determine the current GRAM/USD exchange rate for payment-link generation. This request contains no personal data.
- **The TON blockchain itself:** the vault contract address, payment amounts, and the involved wallet addresses are written permanently and publicly to the TON blockchain as part of every payment transaction. This is a property of the underlying blockchain, not a choice Renewise makes about data sharing.
- **Where required by law.**

### 4. Data Retention

We retain account and transaction records for as long as necessary to operate the Service and to maintain accurate financial and audit records, and as required by applicable law. Audit log entries are retained indefinitely by default. [SPECIFIC RETENTION PERIODS TO BE DEFINED WITH LEGAL COUNSEL]

Records that are tied to a public blockchain transaction (e.g., tx hashes, vault addresses, wallet addresses) cannot be deleted from the blockchain itself, regardless of any request to Renewise.

### 5. On-Chain Data Is Public

Because Renewise settles payments via the TON public blockchain, every payment transaction — including the exact amount and all wallet addresses involved — is permanently and publicly visible on-chain. TonScan and other TON blockchain explorers allow anyone to inspect these transactions. This is a fundamental property of the TON network, not a choice Renewise makes.

### 6. Mini App

Renewise includes a Telegram Mini App that provides an alternative interface for Admins to set up paywalls and view their subscription data. The Mini App authenticates users via Telegram's standard `initData` HMAC mechanism; no separate login is required. All Mini App API endpoints are scoped to the authenticated user's own data only — no cross-admin data is accessible.

### 7. Your Rights

Depending on your jurisdiction, you may have rights to access, correct, or request deletion of your personal data held by Renewise's databases. Note that:
- On-chain transaction data (vault addresses, payment amounts, wallet addresses) is immutable and public by nature of the blockchain and cannot be deleted by Renewise.
- Telegram user IDs and usernames are provided by Telegram's platform; correction requests should be directed to Telegram.

To make a data request, contact [CONTACT METHOD].

### 8. Children

The Service is not directed at, and should not be used by, individuals under the age of 18 (or the age of majority in their jurisdiction).

### 9. Changes to This Policy

We may update this Privacy Policy from time to time. Material changes will be communicated through the bot or Mini App.

### 10. Contact

Questions about this Privacy Policy can be directed to [CONTACT METHOD].

---

## APPENDIX — Platform Technical Summary

This appendix describes how Renewise works technically. It is provided for transparency and to support the legal sections above. It is not itself a legally binding document.

### A. Bot Commands and Flows

**Main bot (`BOT_TOKEN`):**

| Trigger | Who | What it does |
|---|---|---|
| `/start` | Anyone | Welcome screen; deep-links `renew_{group_id}`, `reminders`, `support` are handled here |
| `/createpaywall` (or `start:create_paywall` button) | Admins | 9-step guided wizard: instructions → bot admin grant detection → chat confirmation → fee transparency → billing interval → USD price → wallet address → final confirm → activation |
| `/menu` | Admins | Admin Dashboard showing all managed groups with live stats and action buttons |
| `join_request` event | Subscribers | Triggers the payment flow: welcome DM → QR code + `ton://` payment link → "I've Paid" on-chain check → auto-approve or hold |
| `my_chat_member` event | Bot lifecycle | Handles bot being added/removed as admin; records permission grants for wizard detection |
| `refund:wallet_prompt` callback | Subscribers | Prompts Subscriber to reply with their TON wallet address for overpayment refund |
| Plain text in private chat | Subscribers | Captured only when a pending refund is waiting for a wallet address |

**Admin Dashboard (`/menu`) actions:**
- View group stats (active members, price, next 3 renewals)
- Update subscription price (takes effect for next billing cycle; existing subscribers keep locked-in price)
- Update payout wallet address
- Pause / Resume the paywall (paused groups do not approve new join requests)
- Browse members (paginated, 5 per page) with per-member kick and comp actions
- View paginated payment history
- Comp a member (grant free permanent access by @username or forwarded message)
- Contact Support (routes message to Renewise Superadmin bot)

**Superadmin bot (`SUPERADMIN_BOT_TOKEN`):**

Access is restricted to user IDs listed in `ALLOWED_SUPERADMIN_IDS`.

| Action | What it does |
|---|---|
| Platform Overview | Monthly GMV, fee revenue, active admins/subs |
| Groups Directory | Paginated list of all groups; drill into any group |
| Group details | Suspend/unsuspend, override per-group fees, update price, view paginated payment history, message the Admin |
| Users Directory | Paginated list of all registered users; look up by Telegram ID |
| TX / User Lookup | Look up any transaction hash or user ID; trigger manual payment recheck |
| Kill Switch | Pause or resume all payment processing platform-wide |
| Settings → Global Fees | Update or reset the platform-wide default buyer and admin fee basis points |
| Settings → Ban/Unban Admin | Ban an Admin (suspends their groups); unban an Admin |
| Settings → Rate Feed | View CoinGecko rate feed status |

### B. Payment Flow (step by step)

1. Subscriber requests to join a paywalled group/channel.
2. Renewise DMes the Subscriber with the price and a "Pay Now" button.
3. Tapping "Pay Now" sends a QR code and a `ton://` deep-link encoding the exact payment amount and the vault contract's address (computed deterministically from the group and subscription parameters). The QR code is fetched from api.qrserver.com.
4. The Subscriber sends TON from their wallet (e.g., Tonkeeper). The first payment also deploys the vault contract atomically in the same transaction (TON deploy-on-first-message pattern).
5. The vault contract executes atomically:
   - Splits the payment between the Admin's wallet (price minus admin fee) and Renewise's platform wallet (buyer fee + admin fee).
   - Any overpayment above the required amount is held in `overage_held` and not distributed.
6. The in-process watcher (`poll_vaults_inprocess`) polls TonCenter for new transactions on every registered vault address. When a transaction is found:
   - Idempotency is checked against `processed_tx_hashes`.
   - The payment amount is validated against `required_nano_amount` stored at link-generation time.
   - Underpayments: Subscriber is DMed with the shortfall; the transaction is not marked processed.
   - Overpayments ≥ $1.00 USD: a refund record is created and the overpayment refund flow is triggered.
   - Exact or sufficient payments: subscription is activated in the DB, Subscriber is DMed with confirmation and renewal date, and their join request is approved.
7. Tapping "I've Paid" triggers an immediate on-chain recheck rather than waiting for the next poll cycle.

### C. Smart Contract

The PaymentVault is a Tact-compiled TON smart contract. Each subscription gets its own vault instance, deployed on first payment. Key properties:

- **Deterministic address:** computed from `(admin_wallet, platform_wallet, log_address, trigger_wallet, price, buyer_fee_bps, admin_fee_bps, subscription_id)` — no network call needed.
- **Atomic split on `Pay{}`:** admin's share and platform fee are sent in the same transaction; neither party can selectively block the other's payout.
- **`Refund{recipient}` opcode:** can only be sent by the designated `trigger_wallet`. The refund amount is exactly `overage_held`. The trigger wallet cannot steal from the split or direct funds elsewhere.
- **Gas reserve:** `MIN_GAS_RESERVE = 0.05 TON` is included in the required payment to cover contract compute costs.

### D. Overpayment Refund Flow

1. Watcher detects `amount_nano > required_nano`.
2. A record is created in `overpayment_refunds` (status: `pending_wallet`).
3. Subscriber is DMed asking for their TON wallet address.
4. Subscriber replies with their address (or taps the "Send My Wallet Address" button).
5. Address is validated (format + checksum; testnet/mainnet mismatch caught).
6. If `TRIGGER_MNEMONIC` is set: trigger wallet sends `Refund{recipient}` to the vault via `pytoniq` + `LiteBalancer`. The tx is confirmed by polling seqno (90-second timeout). Refund record is marked `sent` automatically.
7. If `TRIGGER_MNEMONIC` is not set: wallet is saved to DB (status: `pending_send`), Superadmins are alerted for manual processing, Subscriber is told refund will arrive within 24 hours.

### E. Subscription Lifecycle & Enforcement

- **Renewal reminders** run hourly (at :05 past the hour). Subscribers with `next_renewal_date` within the next 3 days (and no reminder sent this cycle) receive a DM with a renewal deep-link.
- **Grace enforcement** runs hourly (at :15 past the hour). Subscribers more than 3 days past their `next_renewal_date` with status `active` are expired and kicked from the group.
- Both jobs can use the Redis/RQ stack (production) or run in-process with the main bot (single-server / development mode via `run.py`).

### F. Mini App API

The Telegram Mini App is a FastAPI application that mirrors the bot's functionality in a web interface. All endpoints require a valid Telegram `initData` Authorization header (HMAC-SHA256). Rate limits: 60 req/min for reads, 10 req/min for writes, per IP.

Key endpoints:
- `GET /api/my-subscriptions` — Subscriber's active subscriptions
- `GET /api/my-groups` — Admin's groups with revenue summary
- `POST /api/groups/detect` — Mirrors wizard STEP_DETECT; returns recently admin-granted chats
- `POST /api/groups/create` — Mirrors wizard final confirm; activates a paywall
- `GET /api/groups/{id}/payment-history` — Paginated payment history for a group
- `GET /api/config` — Returns bot username for constructing renewal deep-links

### G. Database Tables

| Table | Purpose |
|---|---|
| `groups` | One row per paywalled group/channel. Stores price, fees, wallet, status, invite link. |
| `users` | One row per Telegram user who has interacted with the bot. |
| `subscriptions` | One row per (user, group) pair. Tracks status, price locked in, vault address, renewal dates, tx hash. |
| `vault_registry` | Watcher table: maps vault address → (subscription_id, user_id, group_id) for payment resolution. |
| `processed_tx_hashes` | Idempotency table: every processed transaction hash, with optional subscription link. |
| `recent_admin_grants` | Short-lived records of bot admin-grant events (used by wizard STEP_DETECT, 15-minute window). |
| `overpayment_refunds` | Tracks each overpayment refund from detection through to `sent`. |
| `admin_audit_log` | Immutable log of all admin and platform events (price changes, comps, suspensions, etc.). |
| `admin_suspensions` | Admins suspended by Superadmins. |
| `banned_admins` | Admins permanently banned, with reason. |
| `platform_config` | Singleton row: global fee defaults, payments kill-switch state. |

### H. Key Configuration Variables

| Variable | Default | Purpose |
|---|---|---|
| `BUYER_FEE_BPS` | `200` (2.00%) | Buyer-side fee, added on top of subscription price |
| `ADMIN_FEE_BPS` | `330` (3.30%) | Admin-side fee, deducted from payout |
| `GRACE_PERIOD_DAYS` | `3` | Days after expiry before kick |
| `REMINDER_WINDOW_DAYS` | `3` | Days before expiry to send renewal reminder |
| `OVERPAYMENT_REFUND_THRESHOLD_USD` | `1.00` | Minimum overpayment (USD) that triggers a refund |
| `TONCENTER_TESTNET` | `false` | Switches all TON interactions to testnet |
| `TRIGGER_MNEMONIC` | _(unset)_ | 24-word mnemonic for the refund trigger wallet; if unset, refunds are manual |
| `MIN_CONFIRMATIONS` | `1` | On-chain confirmations required before activating a subscription |

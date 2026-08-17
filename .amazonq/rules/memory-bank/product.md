# cosmogate - Product Overview

## Purpose
cosmogate is a non-custodial cryptocurrency paywall bot for Telegram groups and channels. It enables group administrators to gate access behind GRAM (TON) blockchain subscriptions, creating a monetization layer for private Telegram communities.

## Value Proposition
- **Non-custodial**: No third-party holds user funds - payments go directly to admin wallets via smart contracts
- **Automated access control**: Bot manages join requests, payment verification, and subscription lifecycle
- **Transparent pricing**: Admins set subscription prices in GRAM with configurable renewal periods
- **Audit trail**: Complete logging of admin actions and subscription events

## Key Features

### Phase 1 (Current)
- **Paywall setup wizard**: 5-step `/createpaywall` flow for admins to configure group access
- **Join request handling**: Automatic processing of member join requests with payment prompts
- **Admin management menu**: `/menu` command for stats, pricing updates, wallet management, pause/resume
- **TON address validation**: Format and CRC16 checksum verification for wallet addresses
- **Stubbed payment layer**: Clean interfaces ready for Phase 2 blockchain integration

### Phase 2 (Planned)
- **On-chain invoicing**: Generate payment requests via TON smart contracts
- **Chain watcher**: Background service monitoring blockchain for payment confirmations
- **Automatic subscription activation**: Real-time payment verification and access granting

### Phase 3 (Planned)
- **Webhook infrastructure**: TonCenter API integration for real-time transaction monitoring
- **Scheduler service**: Automated subscription renewal checks and expiration handling
- **Load balancing**: Redis-backed job queue for scalable payment processing

## Target Users
1. **Group Administrators**: Content creators, community managers, service providers monetizing private Telegram groups
2. **Group Members**: Users seeking access to exclusive communities willing to pay GRAM subscriptions

## Use Cases
- **Premium content communities**: Paid access to exclusive discussion groups
- **Service access gates**: Subscription-based access to professional services or support
- **Membership organizations**: Automated dues collection for clubs or organizations
- **Course/program access**: Time-limited access to educational communities

## Core Workflows

### Administrator Flow
1. Create Telegram bot via BotFather
2. Add bot to group as admin with proper permissions
3. Run `/createpaywall` wizard (5 steps: group selection, wallet, price, duration, comp users)
4. Manage via `/menu` (stats, adjust price, pause, comp members)

### Member Flow
1. Request to join paywalled group
2. Bot sends DM with payment request
3. Member pays via TON wallet (Phase 2: on-chain, Phase 1: stubbed)
4. Bot verifies payment and approves join request
5. Subscription tracked for renewal

## Monetization Model
- Administrators set their own subscription prices in GRAM
- No platform fees in Phase 1
- Non-custodial: payments flow directly to admin-provided wallet addresses
- Flexible duration: daily, weekly, monthly, or custom subscription periods



// Sent by the payer's wallet app to trigger the split.
message Pay {}

// Emitted as an outgoing internal message to log_address for off-chain indexing.
message PaymentLog {
    subscription_id: Int as uint64;
    amount_paid:     Int as coins;
    admin_amount:    Int as coins;
    platform_amount: Int as coins;
    overage:         Int as coins;
    timestamp:       Int as uint32;
}

// Sent by trigger_wallet to forward held overage to the buyer.
// Gated: only trigger_wallet may send this — all other senders are rejected.
message Refund {
    recipient: Address;
}

// ── Contract ──────────────────────────────────────────────────────────────────

contract PaymentVault {

    admin_wallet:    Address;
    platform_wallet: Address;
    log_address:     Address;
    trigger_wallet:  Address;   // dedicated hot wallet that sends Refund{} messages
    price:           Int as coins;
    buyer_fee_bps:   Int as uint16;
    admin_fee_bps:   Int as uint16;
    subscription_id: Int as uint64;
    overage_held:    Int as coins;  // nanoTON held for refund (0 if no overpayment)
    accumulated_paid: Int as coins; // nanoTON received so far towards the required amount

    // 0.05 TON gas reserve — must match MIN_GAS_RESERVE_NANO in vault.py
    // Higher than the old 0.02 to cover the potential Refund{} send path.
    const MIN_GAS_RESERVE: Int = ton("0.05");

    init(
        admin_wallet:    Address,
        platform_wallet: Address,
        log_address:     Address,
        trigger_wallet:  Address,
        price:           Int,
        buyer_fee_bps:   Int,
        admin_fee_bps:   Int,
        subscription_id: Int
    ) {
        self.admin_wallet    = admin_wallet;
        self.platform_wallet = platform_wallet;
        self.log_address     = log_address;
        self.trigger_wallet  = trigger_wallet;
        self.price           = price;
        self.buyer_fee_bps   = buyer_fee_bps;
        self.admin_fee_bps   = admin_fee_bps;
        self.subscription_id = subscription_id;
        self.overage_held    = 0;
        self.accumulated_paid = 0;
    }

    // ── Payment handler ───────────────────────────────────────────────────────

    receive(msg: Pay) {
        let buyer_fee: Int = self.price * self.buyer_fee_bps / 10000;
        let required:  Int = self.price + buyer_fee + self.MIN_GAS_RESERVE;

        // Add the incoming payment to the accumulated total
        self.accumulated_paid = self.accumulated_paid + context().value;

        // Check if the total accumulated meets the requirement
        if (self.accumulated_paid < required) {
            // Not enough yet. Emit a log so the off-chain watcher can notify the user.
            send(SendParameters{
                to:    self.log_address,
                value: ton("0.001"),
                mode:  SendIgnoreErrors,
                body:  PaymentLog{
                    subscription_id: self.subscription_id,
                    amount_paid:     self.accumulated_paid,
                    admin_amount:    0,
                    platform_amount: 0,
                    overage:         0,
                    timestamp:       now()
                }.toCell()
            });
            // Exit early; funds remain in the vault
            return;
        }

        let admin_fee:      Int = self.price * self.admin_fee_bps / 10000;
        let admin_amount:   Int = self.price - admin_fee;
        let platform_amount: Int = admin_fee + buyer_fee;

        // Overage is based on total accumulated minus required.
        let overage: Int = self.accumulated_paid - required;
        self.overage_held = self.overage_held + overage;
        
        // Reset accumulated_paid so future payments start fresh
        let total_paid_for_log: Int = self.accumulated_paid;
        self.accumulated_paid = 0;

        // 1. Admin share.
        // Mode 0 (regular): forward fees are deducted from the message value itself.
        // This is the correct pattern when splitting a fixed incoming amount — the
        // contract does NOT need extra balance beyond what arrived. SendPayGasSeparately
        // (flag +1) is wrong here because it requires the contract to have additional
        // pre-existing balance to cover fwd_fees, which doesn't exist on first deploy,
        // causing exit code 37 (not enough value) and the send being silently skipped.
        send(SendParameters{
            to:     self.admin_wallet,
            value:  admin_amount,
            bounce: false,
            mode:   SendIgnoreErrors,
            body:   emptyCell()
        });

        // 2. Platform share — same pattern.
        send(SendParameters{
            to:     self.platform_wallet,
            value:  platform_amount,
            bounce: false,
            mode:   SendIgnoreErrors,
            body:   emptyCell()
        });

        // 3. Log message to off-chain watcher (includes overage so watcher can
        //    detect and record the refund amount without reading chain state).
        //    A small non-zero value (0.001 TON) is attached so the message can
        //    cover its own forward fees — sending value=0 causes an action_fine
        //    when skipped, which cascades to fail subsequent sends silently.
        send(SendParameters{
            to:    self.log_address,
            value: ton("0.001"),
            mode:  SendIgnoreErrors,
            body:  PaymentLog{
                subscription_id: self.subscription_id,
                amount_paid:     total_paid_for_log,
                admin_amount:    admin_amount,
                platform_amount: platform_amount,
                overage:         overage,
                timestamp:       now()
            }.toCell()
        });

        // NOTE: No SendRemainingBalance sweep. The gas dust stays in the vault
        // to fund the future Refund{} message call if overage > 0.
        // If overage == 0 the dust is negligible (< 0.012 TON) and remains
        // locked in the vault permanently — an acceptable trade-off.
    }

    // ── Refund handler ────────────────────────────────────────────────────────

    receive(msg: Refund) {
        // Only the trusted trigger_wallet may initiate a refund.
        require(sender() == self.trigger_wallet, "Unauthorized: only trigger_wallet");

        let amount: Int = self.overage_held;
        require(amount > 0, "No overage to refund");

        // Deduct a small gas fee for this message execution before sending.
        // 0.005 TON covers compute + outgoing message fees conservatively.
        let gas_fee:     Int = ton("0.005");
        let refund_amount: Int = amount - gas_fee;
        require(refund_amount > 0, "Overage too small to cover refund gas");

        // Clear the held overage before the send (checks-effects-interactions).
        self.overage_held = 0;

        send(SendParameters{
            to:     msg.recipient,
            value:  refund_amount,
            bounce: false,
            mode:   SendIgnoreErrors,
            body:   emptyCell()
        });
    }

    // ── Getters ───────────────────────────────────────────────────────────────

    get fun config(): VaultConfig {
        return VaultConfig{
            admin_wallet:    self.admin_wallet,
            platform_wallet: self.platform_wallet,
            log_address:     self.log_address,
            trigger_wallet:  self.trigger_wallet,
            price:           self.price,
            buyer_fee_bps:   self.buyer_fee_bps,
            admin_fee_bps:   self.admin_fee_bps,
            subscription_id: self.subscription_id
        };
    }

    get fun overage(): Int {
        return self.overage_held;
    }

    get fun required_payment(): Int {
        // IMPORTANT: includes MIN_GAS_RESERVE so this getter matches exactly
        // what the receive(Pay) handler enforces and what vault.py sends via
        // required_payment_nano(). Any frontend or integration that calls this
        // getter to determine how much to send will produce the correct amount.
        // Previously this omitted MIN_GAS_RESERVE, causing callers who relied
        // on the getter to underpay by 0.05 GRAM and hit the partial-payment
        // accumulation path instead of triggering the split.
        return self.price + (self.price * self.buyer_fee_bps / 10000) + self.MIN_GAS_RESERVE;
    }
}

struct VaultConfig {
    admin_wallet:    Address;
    platform_wallet: Address;
    log_address:     Address;
    trigger_wallet:  Address;
    price:           Int as coins;
    buyer_fee_bps:   Int as uint16;
    admin_fee_bps:   Int as uint16;
    subscription_id: Int as uint64;
}

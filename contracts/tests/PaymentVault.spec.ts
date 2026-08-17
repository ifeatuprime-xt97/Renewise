import { Blockchain, SandboxContract, TreasuryContract } from '@ton/sandbox';
import { toNano, fromNano, beginCell } from '@ton/core';
import { PaymentVault } from '../build/PaymentVault_PaymentVault';
import '@ton/test-utils';

describe('PaymentVault', () => {
    let blockchain: Blockchain;
    let deployer: SandboxContract<TreasuryContract>;
    let admin: SandboxContract<TreasuryContract>;
    let platform: SandboxContract<TreasuryContract>;
    let logAddress: SandboxContract<TreasuryContract>;
    let triggerWallet: SandboxContract<TreasuryContract>;
    let payer: SandboxContract<TreasuryContract>;
    let vault: SandboxContract<PaymentVault>;

    const price = toNano('10'); // 10 TON
    const buyerFeeBps = 200n;  // 2.00%
    const adminFeeBps = 330n;  // 3.30%
    const subscriptionId = 12345n;
    const minGasReserve = toNano('0.05');

    beforeEach(async () => {
        blockchain = await Blockchain.create();

        deployer      = await blockchain.treasury('deployer');
        admin         = await blockchain.treasury('admin');
        platform      = await blockchain.treasury('platform');
        logAddress    = await blockchain.treasury('logAddress');
        triggerWallet = await blockchain.treasury('triggerWallet');
        payer         = await blockchain.treasury('payer');

        vault = blockchain.openContract(await PaymentVault.fromInit(
            admin.address,
            platform.address,
            logAddress.address,
            triggerWallet.address,
            price,
            buyerFeeBps,
            adminFeeBps,
            subscriptionId
        ));
    });

    it('should deploy on first payment and split funds correctly', async () => {
        const buyerFee = (price * buyerFeeBps) / 10000n;
        const requiredAmount = price + buyerFee + minGasReserve;

        const adminFee = (price * adminFeeBps) / 10000n;
        const expectedAdminAmount = price - adminFee;
        const expectedPlatformAmount = adminFee + buyerFee;

        const adminBalanceBefore = await admin.getBalance();
        const platformBalanceBefore = await platform.getBalance();

        const payResult = await vault.send(
            payer.getSender(),
            { value: requiredAmount },
            { $$type: 'Pay' }
        );

        expect(payResult.transactions).toHaveTransaction({
            from: payer.address,
            to: vault.address,
            success: true,
        });

        const adminBalanceAfter = await admin.getBalance();
        const adminReceived = adminBalanceAfter - adminBalanceBefore;
        expect(adminReceived).toBeGreaterThan(expectedAdminAmount - toNano('0.01'));
        expect(adminReceived).toBeLessThanOrEqual(expectedAdminAmount);

        // Platform receives its share + any gas dust that wasn't consumed
        const platformBalanceAfter = await platform.getBalance();
        const platformReceived = platformBalanceAfter - platformBalanceBefore;
        expect(platformReceived).toBeGreaterThan(expectedPlatformAmount);

        // Gas reserve stays in vault to fund a future Refund{} message.
        // Vault balance is NOT zero — the 0.05 TON reserve remains.
        const vaultBalance = (await blockchain.getContract(vault.address)).balance;
        expect(vaultBalance).toBeGreaterThan(0n);
        expect(vaultBalance).toBeLessThanOrEqual(minGasReserve);

        const gasUsed = requiredAmount - expectedAdminAmount - platformReceived;
        console.log(`Actual gas cost for payment split: ${fromNano(gasUsed)} TON`);
    });

    // ── Partial payment (underpayment accumulation) ───────────────────────────
    // The contract accumulates multiple payments in accumulated_paid. It does NOT
    // revert on underpayment — it returns early (exit 0) so funds stay in the vault.
    // The split fires only once accumulated_paid >= required.
    it('should accumulate a partial payment without splitting, then split on completion', async () => {
        const buyerFee = (price * buyerFeeBps) / 10000n;
        const requiredAmount = price + buyerFee + minGasReserve;
        const underpayment = requiredAmount - toNano('0.01'); // 0.01 TON short

        const adminBalanceBefore = await admin.getBalance();
        const platformBalanceBefore = await platform.getBalance();

        // First tx: partial — exits early, no split, transaction still succeeds
        const part1Result = await vault.send(
            payer.getSender(),
            { value: underpayment },
            { $$type: 'Pay' }
        );
        expect(part1Result.transactions).toHaveTransaction({
            from: payer.address,
            to: vault.address,
            success: true, // early return is exit 0, not a revert
        });

        // No funds should have reached admin or platform yet
        expect(await admin.getBalance()).toEqual(adminBalanceBefore);
        expect(await platform.getBalance()).toEqual(platformBalanceBefore);

        // Top-up: send the shortfall — now total >= required, split fires
        const part2Result = await vault.send(
            payer.getSender(),
            { value: toNano('0.01') },
            { $$type: 'Pay' }
        );
        expect(part2Result.transactions).toHaveTransaction({
            from: payer.address,
            to: vault.address,
            success: true,
        });

        const adminFee = (price * adminFeeBps) / 10000n;
        const expectedAdminAmount = price - adminFee;
        const adminReceived = (await admin.getBalance()) - adminBalanceBefore;
        expect(adminReceived).toBeGreaterThan(expectedAdminAmount - toNano('0.01'));
        expect(adminReceived).toBeLessThanOrEqual(expectedAdminAmount);
    });

    // ── Overpayment: overage goes to overage_held, NOT to admin ──────────────
    // This is the key security/honesty property: the contract holds excess funds
    // in overage_held so the payer can reclaim them via Refund{}, not the admin.
    it('should hold overpayment in overage_held for trustless refund (not forward to admin)', async () => {
        const buyerFee = (price * buyerFeeBps) / 10000n;
        const requiredAmount = price + buyerFee + minGasReserve;
        const tipAmount = toNano('2'); // 2 TON overpayment
        const totalPayment = requiredAmount + tipAmount;

        const adminFee = (price * adminFeeBps) / 10000n;
        // Admin receives exactly price - adminFee. Overage goes to overage_held.
        const expectedAdminAmount = price - adminFee;

        const adminBalanceBefore = await admin.getBalance();

        const payResult = await vault.send(
            payer.getSender(),
            { value: totalPayment },
            { $$type: 'Pay' }
        );
        expect(payResult.transactions).toHaveTransaction({
            from: payer.address,
            to: vault.address,
            success: true,
        });

        const adminReceived = (await admin.getBalance()) - adminBalanceBefore;
        // Admin does NOT receive the overage
        expect(adminReceived).toBeGreaterThan(expectedAdminAmount - toNano('0.01'));
        expect(adminReceived).toBeLessThanOrEqual(expectedAdminAmount);

        // overage_held should contain exactly the tip amount
        const overage = await vault.getOverage();
        expect(overage).toEqual(tipAmount);

        // Vault balance > 0: holds overage_held + gas reserve
        const vaultBalance = (await blockchain.getContract(vault.address)).balance;
        expect(vaultBalance).toBeGreaterThan(0n);
    });

    it('should split funds correctly for a low-value subscription (0.2 TON price)', async () => {
        const customPrice = toNano('0.2');
        const customVault = blockchain.openContract(await PaymentVault.fromInit(
            admin.address, platform.address, logAddress.address, triggerWallet.address,
            customPrice, buyerFeeBps, adminFeeBps, subscriptionId
        ));
        const buyerFee = (customPrice * buyerFeeBps) / 10000n;
        const requiredAmount = customPrice + buyerFee + minGasReserve;
        const adminFee = (customPrice * adminFeeBps) / 10000n;
        const expectedAdminAmount = customPrice - adminFee;
        const expectedPlatformAmount = adminFee + buyerFee;

        const adminBalanceBefore = await admin.getBalance();
        const platformBalanceBefore = await platform.getBalance();

        const payResult = await customVault.send(payer.getSender(), { value: requiredAmount }, { $$type: 'Pay' });
        expect(payResult.transactions).toHaveTransaction({ from: payer.address, to: customVault.address, success: true });

        const adminReceived = (await admin.getBalance()) - adminBalanceBefore;
        expect(adminReceived).toBeGreaterThan(expectedAdminAmount - toNano('0.01'));
        expect(adminReceived).toBeLessThanOrEqual(expectedAdminAmount);

        const platformReceived = (await platform.getBalance()) - platformBalanceBefore;
        expect(platformReceived).toBeGreaterThan(expectedPlatformAmount);
    });

    it('should split funds correctly for a high-value subscription (100 TON price)', async () => {
        const customPrice = toNano('100');
        const customVault = blockchain.openContract(await PaymentVault.fromInit(
            admin.address, platform.address, logAddress.address, triggerWallet.address,
            customPrice, buyerFeeBps, adminFeeBps, subscriptionId
        ));
        const buyerFee = (customPrice * buyerFeeBps) / 10000n;
        const requiredAmount = customPrice + buyerFee + minGasReserve;
        const adminFee = (customPrice * adminFeeBps) / 10000n;
        const expectedAdminAmount = customPrice - adminFee;
        const expectedPlatformAmount = adminFee + buyerFee;

        const adminBalanceBefore = await admin.getBalance();
        const platformBalanceBefore = await platform.getBalance();

        const payResult = await customVault.send(payer.getSender(), { value: requiredAmount }, { $$type: 'Pay' });
        expect(payResult.transactions).toHaveTransaction({ from: payer.address, to: customVault.address, success: true });

        const adminReceived = (await admin.getBalance()) - adminBalanceBefore;
        expect(adminReceived).toBeGreaterThan(expectedAdminAmount - toNano('0.01'));
        expect(adminReceived).toBeLessThanOrEqual(expectedAdminAmount);

        const platformReceived = (await platform.getBalance()) - platformBalanceBefore;
        expect(platformReceived).toBeGreaterThan(expectedPlatformAmount);
    });

    it('should split funds correctly with a different fee combination (400/250 bps)', async () => {
        const customBuyerFeeBps = 400n;
        const customAdminFeeBps = 250n;
        const customVault = blockchain.openContract(await PaymentVault.fromInit(
            admin.address, platform.address, logAddress.address, triggerWallet.address,
            price, customBuyerFeeBps, customAdminFeeBps, subscriptionId
        ));
        const buyerFee = (price * customBuyerFeeBps) / 10000n;
        const requiredAmount = price + buyerFee + minGasReserve;
        const adminFee = (price * customAdminFeeBps) / 10000n;
        const expectedAdminAmount = price - adminFee;
        const expectedPlatformAmount = adminFee + buyerFee;

        const adminBalanceBefore = await admin.getBalance();
        const platformBalanceBefore = await platform.getBalance();

        const payResult = await customVault.send(payer.getSender(), { value: requiredAmount }, { $$type: 'Pay' });
        expect(payResult.transactions).toHaveTransaction({ from: payer.address, to: customVault.address, success: true });

        const adminReceived = (await admin.getBalance()) - adminBalanceBefore;
        expect(adminReceived).toBeGreaterThan(expectedAdminAmount - toNano('0.01'));
        expect(adminReceived).toBeLessThanOrEqual(expectedAdminAmount);

        const platformReceived = (await platform.getBalance()) - platformBalanceBefore;
        expect(platformReceived).toBeGreaterThan(expectedPlatformAmount);
    });
});

/**
 * sandbox_runner.js
 *
 * Thin Node.js script that runs a single PaymentVault scenario in @ton/sandbox
 * and prints a JSON result to stdout. Called by pytest as a subprocess.
 *
 * Usage:
 *   node sandbox_runner.js <scenario_json>
 *
 * scenario_json fields:
 *   price          (string, nanoTON)
 *   buyer_fee_bps  (number)
 *   admin_fee_bps  (number)
 *   send_amount    (string, nanoTON — what the payer actually sends)
 *   subscription_id (number)
 *
 * Output JSON fields:
 *   success          (bool)
 *   exit_code        (number | null)
 *   aborted          (bool)
 *   admin_delta      (string, nanoTON received by admin — no tip, overage held in vault)
 *   platform_delta   (string, nanoTON received by platform treasury)
 *   vault_balance    (string, nanoTON left in vault — includes overage if any)
 *   total_fees       (string, nanoTON total gas consumed)
 *   error            (string | null)
 */

const { Blockchain } = require("@ton/sandbox");
const { toNano, fromNano, beginCell } = require("@ton/ton");
const path = require("path");
const fs = require("fs");

const PAY_OPCODE = 0x9a15b153;

async function run(scenario) {
    const price        = BigInt(scenario.price);
    const buyerFeeBps  = scenario.buyer_fee_bps;
    const adminFeeBps  = scenario.admin_fee_bps;
    const sendAmount   = BigInt(scenario.send_amount);
    const subId        = BigInt(scenario.subscription_id ?? 1);

    // Load compiled contract
    const wrapperPath = path.resolve(__dirname, "build/PaymentVault_PaymentVault.js");
    if (!fs.existsSync(wrapperPath)) {
        return { success: false, error: "Compiled wrapper not found. Run: npm run build" };
    }
    const { PaymentVault } = require(wrapperPath);

    const blockchain      = await Blockchain.create();
    const payer           = await blockchain.treasury("payer");
    const adminWallet     = await blockchain.treasury("admin");
    const platformWallet  = await blockchain.treasury("platform");
    const logAddress      = await blockchain.treasury("log");
    const triggerWallet   = await blockchain.treasury("trigger");

    const BASE = toNano("1000000");

    const vault = blockchain.openContract(
        await PaymentVault.fromInit(
            adminWallet.address,
            platformWallet.address,
            logAddress.address,
            triggerWallet.address,
            price,
            buyerFeeBps,
            adminFeeBps,
            subId
        )
    );

    const result = await vault.send(
        payer.getSender(),
        { value: sendAmount },
        { $$type: "Pay" }
    );

    // Find the inbound tx to the vault (first generic tx after treasury send)
    const vaultTx = result.transactions.find(
        tx => tx.description.type === "generic"
    );

    const aborted = vaultTx?.description?.computePhase?.type === "vm" &&
                    vaultTx.description.computePhase.exitCode !== 0;
    const exitCode = vaultTx?.description?.computePhase?.type === "vm"
        ? vaultTx.description.computePhase.exitCode
        : null;

    const adminBal    = await adminWallet.getBalance();
    const platformBal = await platformWallet.getBalance();
    let vaultBal = (await blockchain.getContract(vault.address)).balance;

    let totalFees = 0n;
    for (const tx of result.transactions) {
        if (tx.description.type === "generic") {
            totalFees += tx.totalFees.coins;
        }
    }

    let refundAborted = false;
    let refundExitCode = null;
    let payerBal = await payer.getBalance();

    if (scenario.trigger_refund) {
        const senderWallet = scenario.refund_sender === "trigger" ? triggerWallet : adminWallet;
        const refundResult = await vault.send(
            senderWallet.getSender(),
            { value: toNano("0.05") },
            { $$type: "Refund", recipient: payer.address }
        );

        const refundTx = refundResult.transactions.find(
            tx => tx.description.type === "generic" && tx.inMessage?.body?.asSlice()?.loadUint(32) === 2132218047
        );
        
        refundAborted = refundTx?.description?.computePhase?.type === "vm" &&
                        refundTx.description.computePhase.exitCode !== 0;
        refundExitCode = refundTx?.description?.computePhase?.type === "vm"
            ? refundTx.description.computePhase.exitCode
            : null;

        for (const tx of refundResult.transactions) {
            if (tx.description.type === "generic") {
                totalFees += tx.totalFees.coins;
            }
        }
        vaultBal = (await blockchain.getContract(vault.address)).balance;
        payerBal = await payer.getBalance();
    }

    return {
        success:        !aborted,
        exit_code:      exitCode,
        aborted:        aborted,
        admin_delta:    (adminBal - BASE).toString(),
        platform_delta: (platformBal - BASE).toString(),
        vault_balance:  vaultBal.toString(),
        payer_delta:    (payerBal - BASE).toString(),
        total_fees:     totalFees.toString(),
        refund_aborted: refundAborted,
        refund_exit_code: refundExitCode,
        error:          null,
    };
}

const scenario = JSON.parse(process.argv[2]);
run(scenario)
    .then(r => { process.stdout.write(JSON.stringify(r) + "\n"); })
    .catch(e => { process.stdout.write(JSON.stringify({ success: false, error: String(e) }) + "\n"); });

/**
 * verify_vault_address.js
 * 
 * Uses the EXACT same generated TypeScript PaymentVault class (via the compiled JS)
 * to compute the vault address for the same parameters the deploy script uses.
 * This is the ground truth — if Python doesn't match this, Python is wrong.
 */
const { Address, beginCell, toNano, Cell } = require('@ton/core');

// We need to use the generated TS wrapper, but since it's TypeScript we'll
// replicate the exact init logic from PaymentVault_PaymentVault.ts lines 1044-1067

function initPaymentVault_init_args(src) {
    return (builder) => {
        const b_0 = builder;
        b_0.storeAddress(src.admin_wallet);
        b_0.storeAddress(src.platform_wallet);
        b_0.storeAddress(src.log_address);
        const b_1 = beginCell();
        b_1.storeInt(src.price, 257);
        b_1.storeInt(src.buyer_fee_bps, 257);
        b_1.storeInt(src.admin_fee_bps, 257);
        const b_2 = beginCell();
        b_2.storeInt(src.subscription_id, 257);
        b_1.storeRef(b_2.endCell());
        b_0.storeRef(b_1.endCell());
    };
}

// Exact code cell from PaymentVault_PaymentVault.ts line 1062
const __code = Cell.fromHex('b5ee9c7241020c01000305000228ff008e88f4a413f4bcf2c80bed5320e303ed43d901060202710204019dbf1f2f6a268690000c7097d207d207d207d006987e987e99faab0360bc715fd207d207d206a00e8408080eb80408080eb80408080eb806a1868408080eb801808238823082283e8aa82f16d9e3638c0300145da8812710a9045240a0019dbc936f6a268690000c7097d207d207d207d006987e987e99faab0360bc715fd207d207d206a00e8408080eb80408080eb80408080eb806a1868408080eb801808238823082283e8aa82f16d9e363bc05000e5476545476542602ee3001d072d721d200d200fa4021103450666f04f86102f862ed44d0d200018e12fa40fa40fa40fa00d30fd30fd33f55606c178e2bfa40fa40fa40d401d0810101d700810101d700810101d700d430d0810101d7003010471046104507d15505e208925f08e006d70d1ff2e082218210b94c4535bae30201070b02fa5b5ca8812710a9045320a0820afaf080a08200b637f8416f24135f0358bef2f45326a8812710a9045330a15112a0f8416f24135f0325a15003a1820afaf080a15ca071882a55205a6d6d40037fc8cf8580ca00cf8440ce01fa028069cf40025c6e016eb0935bcf819d58cf8680cf8480f400f400cf81e2f400c901fb00090802fe81008288285445305a6d6d40037fc8cf8580ca00cf8440ce01fa028069cf40025c6e016eb0935bcf819d58cf8680cf8480f400f400cf81e2f400c901fb007072f8416f24135f035043a0f8232c04035066c8554082109cb1e1435006cb1f14cb3f58fa0201fa0201fa02cb1fc92555205a6d6d40037fc8cf8580ca00cf8440090a0000008ace01fa028069cf40025c6e016eb0935bcf819d58cf8680cf8480f400f400cf81e2f400c901fb0010465513c87f01ca0055605067ce14ce12ce01fa02cb0fcb0fcb3fc9ed5400ca8210946a98b6ba8e56d33f30c8018210aff90f5758cb1fcb3fc91057104610354430f84270705003804201503304c8cf8580ca00cf8440ce01fa02806acf40f400c901fb00c87f01ca0055605067ce14ce12ce01fa02cb0fcb0fcb3fc9ed54e05f08f2c082bb80e248');

function PaymentVault_init(admin_wallet, platform_wallet, log_address, price, buyer_fee_bps, admin_fee_bps, subscription_id) {
    const builder = beginCell();
    builder.storeUint(0, 1);
    initPaymentVault_init_args({
        admin_wallet, platform_wallet, log_address,
        price, buyer_fee_bps, admin_fee_bps, subscription_id
    })(builder);
    const __data = builder.endCell();
    return { code: __code, data: __data };
}

// Use the @ton/core contractAddress function — this is the ground truth
const { contractAddress } = require('@ton/core');

// Same params as deploy_testnet.py
const admin_wallet = Address.parse('0QBESfJphztczwgd-wDoU3oGuCvDYNZjT-Z7DeLEvFUX_vov');
const platform_wallet = Address.parse('0QB9wJ_mJUYbLAdHezkpOgBJRSg9M1Ym-IzC5dxkn9Vq0Z7Y');
const log_address = Address.parse('0QB9wJ_mJUYbLAdHezkpOgBJRSg9M1Ym-IzC5dxkn9Vq0Z7Y');
const price = BigInt(Math.floor(0.1 * 1e9));
const buyer_fee_bps = 200n;
const admin_fee_bps = 330n;
const subscription_id = 9999n;

const init = PaymentVault_init(admin_wallet, platform_wallet, log_address, price, buyer_fee_bps, admin_fee_bps, subscription_id);
const addr = contractAddress(0, init);

console.log('=== Ground Truth from TypeScript ===');
console.log('Vault Address (bounceable):', addr.toString({ bounceable: true, urlSafe: true }));
console.log('Vault Address (raw):', addr.toRawString());
console.log('');
console.log('Code Cell Hash:', init.code.hash().toString('hex'));
console.log('Data Cell Hash:', init.data.hash().toString('hex'));
console.log('');
console.log('Data Cell BOC (base64):', init.data.toBoc().toString('base64'));
console.log('Code Cell BOC (base64):', init.code.toBoc().toString('base64'));

// Also build the StateInit cell exactly as contractAddress does it
const stateInitCell = beginCell()
    .storeBit(0)  // split_depth
    .storeBit(0)  // special
    .storeBit(1)  // code present
    .storeRef(init.code)
    .storeBit(1)  // data present
    .storeRef(init.data)
    .storeBit(0)  // library
    .endCell();
console.log('');
console.log('StateInit Cell Hash:', stateInitCell.hash().toString('hex'));

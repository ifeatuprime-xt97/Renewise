import { beginCell, Address } from '@ton/core';

const ADMIN    = Address.parse("0QBESfJphztczwgd-wDoU3oGuCvDYNZjT-Z7DeLEvFUX_vov");
const PLATFORM = Address.parse("0QB9wJ_mJUYbLAdHezkpOgBJRSg9M1Ym-IzC5dxkn9Vq0Z7Y");
const LOG      = Address.parse("0QB9wJ_mJUYbLAdHezkpOgBJRSg9M1Ym-IzC5dxkn9Vq0Z7Y");
const TRIGGER  = Address.parse("0QBESfJphztczwgd-wDoU3oGuCvDYNZjT-Z7DeLEvFUX_vov");

// Build init data exactly as initPaymentVault_init_args does
const b_0 = beginCell();
b_0.storeUint(0, 1);  // Tact init flag
b_0.storeAddress(ADMIN);
b_0.storeAddress(PLATFORM);
b_0.storeAddress(LOG);

const b_1 = beginCell();
b_1.storeAddress(TRIGGER);
b_1.storeInt(100_000_000n, 257);
b_1.storeInt(200n, 257);
const b_2 = beginCell();
b_2.storeInt(330n, 257);
b_2.storeInt(1n, 257);
b_1.storeRef(b_2.endCell());
b_0.storeRef(b_1.endCell());

const dataCell = b_0.endCell();
console.log("Data cell bits:", dataCell.bits.length);
console.log("Data cell refs:", dataCell.refs.length);
console.log("Data cell hash:", dataCell.hash().toString('hex'));
console.log("Data cell BOC (hex, first 80):", dataCell.toBoc().toString('hex').slice(0, 80));

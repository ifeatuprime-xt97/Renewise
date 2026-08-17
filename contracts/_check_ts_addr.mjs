import { PaymentVault } from './build/PaymentVault_PaymentVault.js';
import { Address } from '@ton/core';

const ADMIN    = Address.parse("0QBESfJphztczwgd-wDoU3oGuCvDYNZjT-Z7DeLEvFUX_vov");
const PLATFORM = Address.parse("0QB9wJ_mJUYbLAdHezkpOgBJRSg9M1Ym-IzC5dxkn9Vq0Z7Y");
const LOG      = Address.parse("0QB9wJ_mJUYbLAdHezkpOgBJRSg9M1Ym-IzC5dxkn9Vq0Z7Y");
const TRIGGER  = Address.parse("0QBESfJphztczwgd-wDoU3oGuCvDYNZjT-Z7DeLEvFUX_vov");

const contract = await PaymentVault.fromInit(
    ADMIN, PLATFORM, LOG, TRIGGER,
    100_000_000n,  // price
    200n,          // buyer_fee_bps
    330n,          // admin_fee_bps
    1n             // subscription_id
);

console.log("TypeScript vault address:", contract.address.toString({ bounceable: true, urlSafe: true }));

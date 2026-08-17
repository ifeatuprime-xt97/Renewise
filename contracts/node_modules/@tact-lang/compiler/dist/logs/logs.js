"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || function (mod) {
    if (mod && mod.__esModule) return mod;
    var result = {};
    if (mod != null) for (var k in mod) if (k !== "default" && Object.prototype.hasOwnProperty.call(mod, k)) __createBinding(result, mod, k);
    __setModuleDefault(result, mod);
    return result;
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.$number = exports.Stack = exports.vmLine = exports.VmUnknown = exports.VmException = exports.VmGasRemaining = exports.VmLimitChanged = exports.VmExecute = exports.VmLoc = exports.VmEntry = exports.VmMessage = exports.BcUnknown = exports.bcLine = exports.BcVmLog = exports.BcSteps = exports.BcLimits = exports.BcEntry = exports.BlockchainMessage = void 0;
/* Generated. Do not edit. */
/* eslint-disable @typescript-eslint/no-namespace */
/* eslint-disable @typescript-eslint/no-redundant-type-constituents */
/* eslint-disable @typescript-eslint/no-duplicate-type-constituents */
/* eslint-disable @typescript-eslint/no-unused-vars */
const $ = __importStar(require("@tonstudio/parser-runtime"));
exports.BlockchainMessage = $.loc($.field($.pure("BlockchainMessage"), "$", $.field($.plus($.alt($.lazy(() => exports.BcEntry), $.lazy(() => exports.BcUnknown))), "entries", $.eps)));
exports.BcEntry = $.loc($.field($.pure("BcEntry"), "$", $.right($.str("[ "), $.right($.regex("1234", [
    $.ExpString("1"),
    $.ExpString("2"),
    $.ExpString("3"),
    $.ExpString("4"),
]), $.right($.str("]"), $.right($.str("[t 0]"), $.right($.str("["), $.field($.stry($.plus($.regex("^\\]", $.negateExps([
    $.ExpString('"\\]"'),
])))), "date", $.right($.str("]"), $.right($.str("["), $.field($.stry($.plus($.regex("^:", $.negateExps([
    $.ExpString(":"),
])))), "source", $.right($.str(":"), $.field($.stry($.plus($.regex("0-9", [
    $.ExpRange("0", "9"),
]))), "line", $.right($.str("]"), $.right($.str("\t"), $.field($.lazy(() => exports.bcLine), "info", $.right($.str("\n"), $.eps)))))))))))))))));
exports.BcLimits = $.loc($.field($.pure("BcLimits"), "$", $.right($.str("gas limits: max="), $.field($.lazy(() => exports.$number), "max", $.right($.str(", limit="), $.field($.lazy(() => exports.$number), "limit", $.right($.str(", credit="), $.field($.lazy(() => exports.$number), "credit", $.eps))))))));
exports.BcSteps = $.loc($.field($.pure("BcSteps"), "$", $.right($.str("steps: "), $.field($.lazy(() => exports.$number), "steps", $.right($.str(" gas: used="), $.field($.lazy(() => exports.$number), "used", $.right($.str(", max="), $.field($.lazy(() => exports.$number), "max", $.right($.str(", limit="), $.field($.lazy(() => exports.$number), "limit", $.right($.str(", credit="), $.field($.lazy(() => exports.$number), "credit", $.eps))))))))))));
exports.BcVmLog = $.loc($.field($.pure("BcVmLog"), "$", $.right($.str("VM log\n"), $.field($.plus($.lazy(() => exports.VmEntry)), "entries", $.eps))));
exports.bcLine = $.alt(exports.BcLimits, $.alt(exports.BcSteps, exports.BcVmLog));
exports.BcUnknown = $.loc($.field($.pure("BcUnknown"), "$", $.field($.stry($.plus($.regex("^\\n", $.negateExps([$.ExpString("\n")])))), "text", $.right($.str("\n"), $.eps))));
exports.VmMessage = $.loc($.field($.pure("VmMessage"), "$", $.field($.star($.lazy(() => exports.VmEntry)), "entries", $.eps)));
exports.VmEntry = $.loc($.field($.pure("VmEntry"), "$", $.field($.lazy(() => exports.Stack), "stack", $.field($.plus($.lazy(() => exports.vmLine)), "other", $.eps))));
exports.VmLoc = $.loc($.field($.pure("VmLoc"), "$", $.right($.str("code cell hash: "), $.field($.stry($.plus($.regex("0-9A-F", [
    $.ExpRange("0", "9"),
    $.ExpRange("A", "F"),
]))), "hash", $.right($.str(" offset: "), $.field($.stry($.plus($.regex("0-9", [$.ExpRange("0", "9")]))), "offset", $.right($.str("\n"), $.eps)))))));
exports.VmExecute = $.loc($.field($.pure("VmExecute"), "$", $.right($.str("execute "), $.field($.stry($.plus($.regex("^\\n", $.negateExps([$.ExpString("\n")])))), "instr", $.right($.str("\n"), $.eps)))));
exports.VmLimitChanged = $.loc($.field($.pure("VmLimitChanged"), "$", $.right($.str("changing gas limit to "), $.field($.stry($.plus($.regex("0-9", [$.ExpRange("0", "9")]))), "limit", $.right($.str("\n"), $.eps)))));
exports.VmGasRemaining = $.loc($.field($.pure("VmGasRemaining"), "$", $.right($.str("gas remaining: "), $.field($.stry($.plus($.regex("0-9", [$.ExpRange("0", "9")]))), "gas", $.right($.str("\n"), $.eps)))));
exports.VmException = $.loc($.field($.pure("VmException"), "$", $.right($.str("handling exception code "), $.field($.lazy(() => exports.$number), "errno", $.right($.str(": "), $.field($.stry($.star($.regex("^\\n", $.negateExps([$.ExpString("\n")])))), "message", $.right($.str("\n"), $.eps)))))));
exports.VmUnknown = $.loc($.field($.pure("VmUnknown"), "$", $.right($.lookNeg($.str("stack")), $.field($.stry($.plus($.regex("^\\n", $.negateExps([$.ExpString("\n")])))), "text", $.right($.str("\n"), $.eps)))));
exports.vmLine = $.alt(exports.VmLoc, $.alt(exports.VmExecute, $.alt(exports.VmLimitChanged, $.alt(exports.VmGasRemaining, $.alt(exports.VmException, exports.VmUnknown)))));
exports.Stack = $.loc($.field($.pure("Stack"), "$", $.right($.str("stack"), $.field($.stry($.plus($.regex("^\\n", $.negateExps([$.ExpString("\n")])))), "stack", $.right($.str("\n"), $.eps)))));
exports.$number = $.stry($.plus($.regex("0-9", [$.ExpRange("0", "9")])));

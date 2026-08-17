import * as $ from "@tonstudio/parser-runtime";
export declare namespace $ast {
    type BlockchainMessage = $.Located<{
        readonly $: "BlockchainMessage";
        readonly entries: readonly (BcEntry | BcUnknown)[];
    }>;
    type BcEntry = $.Located<{
        readonly $: "BcEntry";
        readonly date: string;
        readonly source: string;
        readonly line: string;
        readonly info: bcLine;
    }>;
    type BcLimits = $.Located<{
        readonly $: "BcLimits";
        readonly max: $number;
        readonly limit: $number;
        readonly credit: $number;
    }>;
    type BcSteps = $.Located<{
        readonly $: "BcSteps";
        readonly steps: $number;
        readonly used: $number;
        readonly max: $number;
        readonly limit: $number;
        readonly credit: $number;
    }>;
    type BcVmLog = $.Located<{
        readonly $: "BcVmLog";
        readonly entries: readonly VmEntry[];
    }>;
    type bcLine = BcLimits | BcSteps | BcVmLog;
    type BcUnknown = $.Located<{
        readonly $: "BcUnknown";
        readonly text: string;
    }>;
    type VmMessage = $.Located<{
        readonly $: "VmMessage";
        readonly entries: readonly VmEntry[];
    }>;
    type VmEntry = $.Located<{
        readonly $: "VmEntry";
        readonly stack: Stack;
        readonly other: readonly vmLine[];
    }>;
    type VmLoc = $.Located<{
        readonly $: "VmLoc";
        readonly hash: string;
        readonly offset: string;
    }>;
    type VmExecute = $.Located<{
        readonly $: "VmExecute";
        readonly instr: string;
    }>;
    type VmLimitChanged = $.Located<{
        readonly $: "VmLimitChanged";
        readonly limit: string;
    }>;
    type VmGasRemaining = $.Located<{
        readonly $: "VmGasRemaining";
        readonly gas: string;
    }>;
    type VmException = $.Located<{
        readonly $: "VmException";
        readonly errno: $number;
        readonly message: string;
    }>;
    type VmUnknown = $.Located<{
        readonly $: "VmUnknown";
        readonly text: string;
    }>;
    type vmLine = VmLoc | VmExecute | VmLimitChanged | VmGasRemaining | VmException | VmUnknown;
    type Stack = $.Located<{
        readonly $: "Stack";
        readonly stack: string;
    }>;
    type $number = string;
}
export declare const BlockchainMessage: $.Parser<$ast.BlockchainMessage>;
export declare const BcEntry: $.Parser<$ast.BcEntry>;
export declare const BcLimits: $.Parser<$ast.BcLimits>;
export declare const BcSteps: $.Parser<$ast.BcSteps>;
export declare const BcVmLog: $.Parser<$ast.BcVmLog>;
export declare const bcLine: $.Parser<$ast.bcLine>;
export declare const BcUnknown: $.Parser<$ast.BcUnknown>;
export declare const VmMessage: $.Parser<$ast.VmMessage>;
export declare const VmEntry: $.Parser<$ast.VmEntry>;
export declare const VmLoc: $.Parser<$ast.VmLoc>;
export declare const VmExecute: $.Parser<$ast.VmExecute>;
export declare const VmLimitChanged: $.Parser<$ast.VmLimitChanged>;
export declare const VmGasRemaining: $.Parser<$ast.VmGasRemaining>;
export declare const VmException: $.Parser<$ast.VmException>;
export declare const VmUnknown: $.Parser<$ast.VmUnknown>;
export declare const vmLine: $.Parser<$ast.vmLine>;
export declare const Stack: $.Parser<$ast.Stack>;
export declare const $number: $.Parser<$ast.$number>;

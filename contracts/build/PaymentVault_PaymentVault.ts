import {
    Cell,
    Slice,
    Address,
    Builder,
    beginCell,
    ComputeError,
    TupleItem,
    TupleReader,
    Dictionary,
    contractAddress,
    address,
    ContractProvider,
    Sender,
    Contract,
    ContractABI,
    ABIType,
    ABIGetter,
    ABIReceiver,
    TupleBuilder,
    DictionaryValue
} from '@ton/core';

export type DataSize = {
    $$type: 'DataSize';
    cells: bigint;
    bits: bigint;
    refs: bigint;
}

export function storeDataSize(src: DataSize) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeInt(src.cells, 257);
        b_0.storeInt(src.bits, 257);
        b_0.storeInt(src.refs, 257);
    };
}

export function loadDataSize(slice: Slice) {
    const sc_0 = slice;
    const _cells = sc_0.loadIntBig(257);
    const _bits = sc_0.loadIntBig(257);
    const _refs = sc_0.loadIntBig(257);
    return { $$type: 'DataSize' as const, cells: _cells, bits: _bits, refs: _refs };
}

export function loadTupleDataSize(source: TupleReader) {
    const _cells = source.readBigNumber();
    const _bits = source.readBigNumber();
    const _refs = source.readBigNumber();
    return { $$type: 'DataSize' as const, cells: _cells, bits: _bits, refs: _refs };
}

export function loadGetterTupleDataSize(source: TupleReader) {
    const _cells = source.readBigNumber();
    const _bits = source.readBigNumber();
    const _refs = source.readBigNumber();
    return { $$type: 'DataSize' as const, cells: _cells, bits: _bits, refs: _refs };
}

export function storeTupleDataSize(source: DataSize) {
    const builder = new TupleBuilder();
    builder.writeNumber(source.cells);
    builder.writeNumber(source.bits);
    builder.writeNumber(source.refs);
    return builder.build();
}

export function dictValueParserDataSize(): DictionaryValue<DataSize> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storeDataSize(src)).endCell());
        },
        parse: (src) => {
            return loadDataSize(src.loadRef().beginParse());
        }
    }
}

export type SignedBundle = {
    $$type: 'SignedBundle';
    signature: Buffer;
    signedData: Slice;
}

export function storeSignedBundle(src: SignedBundle) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeBuffer(src.signature);
        b_0.storeBuilder(src.signedData.asBuilder());
    };
}

export function loadSignedBundle(slice: Slice) {
    const sc_0 = slice;
    const _signature = sc_0.loadBuffer(64);
    const _signedData = sc_0;
    return { $$type: 'SignedBundle' as const, signature: _signature, signedData: _signedData };
}

export function loadTupleSignedBundle(source: TupleReader) {
    const _signature = source.readBuffer();
    const _signedData = source.readCell().asSlice();
    return { $$type: 'SignedBundle' as const, signature: _signature, signedData: _signedData };
}

export function loadGetterTupleSignedBundle(source: TupleReader) {
    const _signature = source.readBuffer();
    const _signedData = source.readCell().asSlice();
    return { $$type: 'SignedBundle' as const, signature: _signature, signedData: _signedData };
}

export function storeTupleSignedBundle(source: SignedBundle) {
    const builder = new TupleBuilder();
    builder.writeBuffer(source.signature);
    builder.writeSlice(source.signedData.asCell());
    return builder.build();
}

export function dictValueParserSignedBundle(): DictionaryValue<SignedBundle> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storeSignedBundle(src)).endCell());
        },
        parse: (src) => {
            return loadSignedBundle(src.loadRef().beginParse());
        }
    }
}

export type StateInit = {
    $$type: 'StateInit';
    code: Cell;
    data: Cell;
}

export function storeStateInit(src: StateInit) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeRef(src.code);
        b_0.storeRef(src.data);
    };
}

export function loadStateInit(slice: Slice) {
    const sc_0 = slice;
    const _code = sc_0.loadRef();
    const _data = sc_0.loadRef();
    return { $$type: 'StateInit' as const, code: _code, data: _data };
}

export function loadTupleStateInit(source: TupleReader) {
    const _code = source.readCell();
    const _data = source.readCell();
    return { $$type: 'StateInit' as const, code: _code, data: _data };
}

export function loadGetterTupleStateInit(source: TupleReader) {
    const _code = source.readCell();
    const _data = source.readCell();
    return { $$type: 'StateInit' as const, code: _code, data: _data };
}

export function storeTupleStateInit(source: StateInit) {
    const builder = new TupleBuilder();
    builder.writeCell(source.code);
    builder.writeCell(source.data);
    return builder.build();
}

export function dictValueParserStateInit(): DictionaryValue<StateInit> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storeStateInit(src)).endCell());
        },
        parse: (src) => {
            return loadStateInit(src.loadRef().beginParse());
        }
    }
}

export type Context = {
    $$type: 'Context';
    bounceable: boolean;
    sender: Address;
    value: bigint;
    raw: Slice;
}

export function storeContext(src: Context) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeBit(src.bounceable);
        b_0.storeAddress(src.sender);
        b_0.storeInt(src.value, 257);
        b_0.storeRef(src.raw.asCell());
    };
}

export function loadContext(slice: Slice) {
    const sc_0 = slice;
    const _bounceable = sc_0.loadBit();
    const _sender = sc_0.loadAddress();
    const _value = sc_0.loadIntBig(257);
    const _raw = sc_0.loadRef().asSlice();
    return { $$type: 'Context' as const, bounceable: _bounceable, sender: _sender, value: _value, raw: _raw };
}

export function loadTupleContext(source: TupleReader) {
    const _bounceable = source.readBoolean();
    const _sender = source.readAddress();
    const _value = source.readBigNumber();
    const _raw = source.readCell().asSlice();
    return { $$type: 'Context' as const, bounceable: _bounceable, sender: _sender, value: _value, raw: _raw };
}

export function loadGetterTupleContext(source: TupleReader) {
    const _bounceable = source.readBoolean();
    const _sender = source.readAddress();
    const _value = source.readBigNumber();
    const _raw = source.readCell().asSlice();
    return { $$type: 'Context' as const, bounceable: _bounceable, sender: _sender, value: _value, raw: _raw };
}

export function storeTupleContext(source: Context) {
    const builder = new TupleBuilder();
    builder.writeBoolean(source.bounceable);
    builder.writeAddress(source.sender);
    builder.writeNumber(source.value);
    builder.writeSlice(source.raw.asCell());
    return builder.build();
}

export function dictValueParserContext(): DictionaryValue<Context> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storeContext(src)).endCell());
        },
        parse: (src) => {
            return loadContext(src.loadRef().beginParse());
        }
    }
}

export type SendParameters = {
    $$type: 'SendParameters';
    mode: bigint;
    body: Cell | null;
    code: Cell | null;
    data: Cell | null;
    value: bigint;
    to: Address;
    bounce: boolean;
}

export function storeSendParameters(src: SendParameters) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeInt(src.mode, 257);
        if (src.body !== null && src.body !== undefined) { b_0.storeBit(true).storeRef(src.body); } else { b_0.storeBit(false); }
        if (src.code !== null && src.code !== undefined) { b_0.storeBit(true).storeRef(src.code); } else { b_0.storeBit(false); }
        if (src.data !== null && src.data !== undefined) { b_0.storeBit(true).storeRef(src.data); } else { b_0.storeBit(false); }
        b_0.storeInt(src.value, 257);
        b_0.storeAddress(src.to);
        b_0.storeBit(src.bounce);
    };
}

export function loadSendParameters(slice: Slice) {
    const sc_0 = slice;
    const _mode = sc_0.loadIntBig(257);
    const _body = sc_0.loadBit() ? sc_0.loadRef() : null;
    const _code = sc_0.loadBit() ? sc_0.loadRef() : null;
    const _data = sc_0.loadBit() ? sc_0.loadRef() : null;
    const _value = sc_0.loadIntBig(257);
    const _to = sc_0.loadAddress();
    const _bounce = sc_0.loadBit();
    return { $$type: 'SendParameters' as const, mode: _mode, body: _body, code: _code, data: _data, value: _value, to: _to, bounce: _bounce };
}

export function loadTupleSendParameters(source: TupleReader) {
    const _mode = source.readBigNumber();
    const _body = source.readCellOpt();
    const _code = source.readCellOpt();
    const _data = source.readCellOpt();
    const _value = source.readBigNumber();
    const _to = source.readAddress();
    const _bounce = source.readBoolean();
    return { $$type: 'SendParameters' as const, mode: _mode, body: _body, code: _code, data: _data, value: _value, to: _to, bounce: _bounce };
}

export function loadGetterTupleSendParameters(source: TupleReader) {
    const _mode = source.readBigNumber();
    const _body = source.readCellOpt();
    const _code = source.readCellOpt();
    const _data = source.readCellOpt();
    const _value = source.readBigNumber();
    const _to = source.readAddress();
    const _bounce = source.readBoolean();
    return { $$type: 'SendParameters' as const, mode: _mode, body: _body, code: _code, data: _data, value: _value, to: _to, bounce: _bounce };
}

export function storeTupleSendParameters(source: SendParameters) {
    const builder = new TupleBuilder();
    builder.writeNumber(source.mode);
    builder.writeCell(source.body);
    builder.writeCell(source.code);
    builder.writeCell(source.data);
    builder.writeNumber(source.value);
    builder.writeAddress(source.to);
    builder.writeBoolean(source.bounce);
    return builder.build();
}

export function dictValueParserSendParameters(): DictionaryValue<SendParameters> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storeSendParameters(src)).endCell());
        },
        parse: (src) => {
            return loadSendParameters(src.loadRef().beginParse());
        }
    }
}

export type MessageParameters = {
    $$type: 'MessageParameters';
    mode: bigint;
    body: Cell | null;
    value: bigint;
    to: Address;
    bounce: boolean;
}

export function storeMessageParameters(src: MessageParameters) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeInt(src.mode, 257);
        if (src.body !== null && src.body !== undefined) { b_0.storeBit(true).storeRef(src.body); } else { b_0.storeBit(false); }
        b_0.storeInt(src.value, 257);
        b_0.storeAddress(src.to);
        b_0.storeBit(src.bounce);
    };
}

export function loadMessageParameters(slice: Slice) {
    const sc_0 = slice;
    const _mode = sc_0.loadIntBig(257);
    const _body = sc_0.loadBit() ? sc_0.loadRef() : null;
    const _value = sc_0.loadIntBig(257);
    const _to = sc_0.loadAddress();
    const _bounce = sc_0.loadBit();
    return { $$type: 'MessageParameters' as const, mode: _mode, body: _body, value: _value, to: _to, bounce: _bounce };
}

export function loadTupleMessageParameters(source: TupleReader) {
    const _mode = source.readBigNumber();
    const _body = source.readCellOpt();
    const _value = source.readBigNumber();
    const _to = source.readAddress();
    const _bounce = source.readBoolean();
    return { $$type: 'MessageParameters' as const, mode: _mode, body: _body, value: _value, to: _to, bounce: _bounce };
}

export function loadGetterTupleMessageParameters(source: TupleReader) {
    const _mode = source.readBigNumber();
    const _body = source.readCellOpt();
    const _value = source.readBigNumber();
    const _to = source.readAddress();
    const _bounce = source.readBoolean();
    return { $$type: 'MessageParameters' as const, mode: _mode, body: _body, value: _value, to: _to, bounce: _bounce };
}

export function storeTupleMessageParameters(source: MessageParameters) {
    const builder = new TupleBuilder();
    builder.writeNumber(source.mode);
    builder.writeCell(source.body);
    builder.writeNumber(source.value);
    builder.writeAddress(source.to);
    builder.writeBoolean(source.bounce);
    return builder.build();
}

export function dictValueParserMessageParameters(): DictionaryValue<MessageParameters> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storeMessageParameters(src)).endCell());
        },
        parse: (src) => {
            return loadMessageParameters(src.loadRef().beginParse());
        }
    }
}

export type DeployParameters = {
    $$type: 'DeployParameters';
    mode: bigint;
    body: Cell | null;
    value: bigint;
    bounce: boolean;
    init: StateInit;
}

export function storeDeployParameters(src: DeployParameters) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeInt(src.mode, 257);
        if (src.body !== null && src.body !== undefined) { b_0.storeBit(true).storeRef(src.body); } else { b_0.storeBit(false); }
        b_0.storeInt(src.value, 257);
        b_0.storeBit(src.bounce);
        b_0.store(storeStateInit(src.init));
    };
}

export function loadDeployParameters(slice: Slice) {
    const sc_0 = slice;
    const _mode = sc_0.loadIntBig(257);
    const _body = sc_0.loadBit() ? sc_0.loadRef() : null;
    const _value = sc_0.loadIntBig(257);
    const _bounce = sc_0.loadBit();
    const _init = loadStateInit(sc_0);
    return { $$type: 'DeployParameters' as const, mode: _mode, body: _body, value: _value, bounce: _bounce, init: _init };
}

export function loadTupleDeployParameters(source: TupleReader) {
    const _mode = source.readBigNumber();
    const _body = source.readCellOpt();
    const _value = source.readBigNumber();
    const _bounce = source.readBoolean();
    const _init = loadTupleStateInit(source);
    return { $$type: 'DeployParameters' as const, mode: _mode, body: _body, value: _value, bounce: _bounce, init: _init };
}

export function loadGetterTupleDeployParameters(source: TupleReader) {
    const _mode = source.readBigNumber();
    const _body = source.readCellOpt();
    const _value = source.readBigNumber();
    const _bounce = source.readBoolean();
    const _init = loadGetterTupleStateInit(source);
    return { $$type: 'DeployParameters' as const, mode: _mode, body: _body, value: _value, bounce: _bounce, init: _init };
}

export function storeTupleDeployParameters(source: DeployParameters) {
    const builder = new TupleBuilder();
    builder.writeNumber(source.mode);
    builder.writeCell(source.body);
    builder.writeNumber(source.value);
    builder.writeBoolean(source.bounce);
    builder.writeTuple(storeTupleStateInit(source.init));
    return builder.build();
}

export function dictValueParserDeployParameters(): DictionaryValue<DeployParameters> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storeDeployParameters(src)).endCell());
        },
        parse: (src) => {
            return loadDeployParameters(src.loadRef().beginParse());
        }
    }
}

export type StdAddress = {
    $$type: 'StdAddress';
    workchain: bigint;
    address: bigint;
}

export function storeStdAddress(src: StdAddress) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeInt(src.workchain, 8);
        b_0.storeUint(src.address, 256);
    };
}

export function loadStdAddress(slice: Slice) {
    const sc_0 = slice;
    const _workchain = sc_0.loadIntBig(8);
    const _address = sc_0.loadUintBig(256);
    return { $$type: 'StdAddress' as const, workchain: _workchain, address: _address };
}

export function loadTupleStdAddress(source: TupleReader) {
    const _workchain = source.readBigNumber();
    const _address = source.readBigNumber();
    return { $$type: 'StdAddress' as const, workchain: _workchain, address: _address };
}

export function loadGetterTupleStdAddress(source: TupleReader) {
    const _workchain = source.readBigNumber();
    const _address = source.readBigNumber();
    return { $$type: 'StdAddress' as const, workchain: _workchain, address: _address };
}

export function storeTupleStdAddress(source: StdAddress) {
    const builder = new TupleBuilder();
    builder.writeNumber(source.workchain);
    builder.writeNumber(source.address);
    return builder.build();
}

export function dictValueParserStdAddress(): DictionaryValue<StdAddress> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storeStdAddress(src)).endCell());
        },
        parse: (src) => {
            return loadStdAddress(src.loadRef().beginParse());
        }
    }
}

export type VarAddress = {
    $$type: 'VarAddress';
    workchain: bigint;
    address: Slice;
}

export function storeVarAddress(src: VarAddress) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeInt(src.workchain, 32);
        b_0.storeRef(src.address.asCell());
    };
}

export function loadVarAddress(slice: Slice) {
    const sc_0 = slice;
    const _workchain = sc_0.loadIntBig(32);
    const _address = sc_0.loadRef().asSlice();
    return { $$type: 'VarAddress' as const, workchain: _workchain, address: _address };
}

export function loadTupleVarAddress(source: TupleReader) {
    const _workchain = source.readBigNumber();
    const _address = source.readCell().asSlice();
    return { $$type: 'VarAddress' as const, workchain: _workchain, address: _address };
}

export function loadGetterTupleVarAddress(source: TupleReader) {
    const _workchain = source.readBigNumber();
    const _address = source.readCell().asSlice();
    return { $$type: 'VarAddress' as const, workchain: _workchain, address: _address };
}

export function storeTupleVarAddress(source: VarAddress) {
    const builder = new TupleBuilder();
    builder.writeNumber(source.workchain);
    builder.writeSlice(source.address.asCell());
    return builder.build();
}

export function dictValueParserVarAddress(): DictionaryValue<VarAddress> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storeVarAddress(src)).endCell());
        },
        parse: (src) => {
            return loadVarAddress(src.loadRef().beginParse());
        }
    }
}

export type BasechainAddress = {
    $$type: 'BasechainAddress';
    hash: bigint | null;
}

export function storeBasechainAddress(src: BasechainAddress) {
    return (builder: Builder) => {
        const b_0 = builder;
        if (src.hash !== null && src.hash !== undefined) { b_0.storeBit(true).storeInt(src.hash, 257); } else { b_0.storeBit(false); }
    };
}

export function loadBasechainAddress(slice: Slice) {
    const sc_0 = slice;
    const _hash = sc_0.loadBit() ? sc_0.loadIntBig(257) : null;
    return { $$type: 'BasechainAddress' as const, hash: _hash };
}

export function loadTupleBasechainAddress(source: TupleReader) {
    const _hash = source.readBigNumberOpt();
    return { $$type: 'BasechainAddress' as const, hash: _hash };
}

export function loadGetterTupleBasechainAddress(source: TupleReader) {
    const _hash = source.readBigNumberOpt();
    return { $$type: 'BasechainAddress' as const, hash: _hash };
}

export function storeTupleBasechainAddress(source: BasechainAddress) {
    const builder = new TupleBuilder();
    builder.writeNumber(source.hash);
    return builder.build();
}

export function dictValueParserBasechainAddress(): DictionaryValue<BasechainAddress> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storeBasechainAddress(src)).endCell());
        },
        parse: (src) => {
            return loadBasechainAddress(src.loadRef().beginParse());
        }
    }
}

export type Pay = {
    $$type: 'Pay';
}

export function storePay(src: Pay) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeUint(3108783413, 32);
    };
}

export function loadPay(slice: Slice) {
    const sc_0 = slice;
    if (sc_0.loadUint(32) !== 3108783413) { throw Error('Invalid prefix'); }
    return { $$type: 'Pay' as const };
}

export function loadTuplePay(source: TupleReader) {
    return { $$type: 'Pay' as const };
}

export function loadGetterTuplePay(source: TupleReader) {
    return { $$type: 'Pay' as const };
}

export function storeTuplePay(source: Pay) {
    const builder = new TupleBuilder();
    return builder.build();
}

export function dictValueParserPay(): DictionaryValue<Pay> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storePay(src)).endCell());
        },
        parse: (src) => {
            return loadPay(src.loadRef().beginParse());
        }
    }
}

export type PaymentLog = {
    $$type: 'PaymentLog';
    subscription_id: bigint;
    amount_paid: bigint;
    admin_amount: bigint;
    platform_amount: bigint;
    overage: bigint;
    timestamp: bigint;
}

export function storePaymentLog(src: PaymentLog) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeUint(1022495610, 32);
        b_0.storeUint(src.subscription_id, 64);
        b_0.storeCoins(src.amount_paid);
        b_0.storeCoins(src.admin_amount);
        b_0.storeCoins(src.platform_amount);
        b_0.storeCoins(src.overage);
        b_0.storeUint(src.timestamp, 32);
    };
}

export function loadPaymentLog(slice: Slice) {
    const sc_0 = slice;
    if (sc_0.loadUint(32) !== 1022495610) { throw Error('Invalid prefix'); }
    const _subscription_id = sc_0.loadUintBig(64);
    const _amount_paid = sc_0.loadCoins();
    const _admin_amount = sc_0.loadCoins();
    const _platform_amount = sc_0.loadCoins();
    const _overage = sc_0.loadCoins();
    const _timestamp = sc_0.loadUintBig(32);
    return { $$type: 'PaymentLog' as const, subscription_id: _subscription_id, amount_paid: _amount_paid, admin_amount: _admin_amount, platform_amount: _platform_amount, overage: _overage, timestamp: _timestamp };
}

export function loadTuplePaymentLog(source: TupleReader) {
    const _subscription_id = source.readBigNumber();
    const _amount_paid = source.readBigNumber();
    const _admin_amount = source.readBigNumber();
    const _platform_amount = source.readBigNumber();
    const _overage = source.readBigNumber();
    const _timestamp = source.readBigNumber();
    return { $$type: 'PaymentLog' as const, subscription_id: _subscription_id, amount_paid: _amount_paid, admin_amount: _admin_amount, platform_amount: _platform_amount, overage: _overage, timestamp: _timestamp };
}

export function loadGetterTuplePaymentLog(source: TupleReader) {
    const _subscription_id = source.readBigNumber();
    const _amount_paid = source.readBigNumber();
    const _admin_amount = source.readBigNumber();
    const _platform_amount = source.readBigNumber();
    const _overage = source.readBigNumber();
    const _timestamp = source.readBigNumber();
    return { $$type: 'PaymentLog' as const, subscription_id: _subscription_id, amount_paid: _amount_paid, admin_amount: _admin_amount, platform_amount: _platform_amount, overage: _overage, timestamp: _timestamp };
}

export function storeTuplePaymentLog(source: PaymentLog) {
    const builder = new TupleBuilder();
    builder.writeNumber(source.subscription_id);
    builder.writeNumber(source.amount_paid);
    builder.writeNumber(source.admin_amount);
    builder.writeNumber(source.platform_amount);
    builder.writeNumber(source.overage);
    builder.writeNumber(source.timestamp);
    return builder.build();
}

export function dictValueParserPaymentLog(): DictionaryValue<PaymentLog> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storePaymentLog(src)).endCell());
        },
        parse: (src) => {
            return loadPaymentLog(src.loadRef().beginParse());
        }
    }
}

export type Refund = {
    $$type: 'Refund';
    recipient: Address;
}

export function storeRefund(src: Refund) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeUint(2132218047, 32);
        b_0.storeAddress(src.recipient);
    };
}

export function loadRefund(slice: Slice) {
    const sc_0 = slice;
    if (sc_0.loadUint(32) !== 2132218047) { throw Error('Invalid prefix'); }
    const _recipient = sc_0.loadAddress();
    return { $$type: 'Refund' as const, recipient: _recipient };
}

export function loadTupleRefund(source: TupleReader) {
    const _recipient = source.readAddress();
    return { $$type: 'Refund' as const, recipient: _recipient };
}

export function loadGetterTupleRefund(source: TupleReader) {
    const _recipient = source.readAddress();
    return { $$type: 'Refund' as const, recipient: _recipient };
}

export function storeTupleRefund(source: Refund) {
    const builder = new TupleBuilder();
    builder.writeAddress(source.recipient);
    return builder.build();
}

export function dictValueParserRefund(): DictionaryValue<Refund> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storeRefund(src)).endCell());
        },
        parse: (src) => {
            return loadRefund(src.loadRef().beginParse());
        }
    }
}

export type PaymentVault$Data = {
    $$type: 'PaymentVault$Data';
    admin_wallet: Address;
    platform_wallet: Address;
    log_address: Address;
    trigger_wallet: Address;
    price: bigint;
    buyer_fee_bps: bigint;
    admin_fee_bps: bigint;
    subscription_id: bigint;
    overage_held: bigint;
    accumulated_paid: bigint;
}

export function storePaymentVault$Data(src: PaymentVault$Data) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeAddress(src.admin_wallet);
        b_0.storeAddress(src.platform_wallet);
        b_0.storeAddress(src.log_address);
        const b_1 = new Builder();
        b_1.storeAddress(src.trigger_wallet);
        b_1.storeCoins(src.price);
        b_1.storeUint(src.buyer_fee_bps, 16);
        b_1.storeUint(src.admin_fee_bps, 16);
        b_1.storeUint(src.subscription_id, 64);
        b_1.storeCoins(src.overage_held);
        b_1.storeCoins(src.accumulated_paid);
        b_0.storeRef(b_1.endCell());
    };
}

export function loadPaymentVault$Data(slice: Slice) {
    const sc_0 = slice;
    const _admin_wallet = sc_0.loadAddress();
    const _platform_wallet = sc_0.loadAddress();
    const _log_address = sc_0.loadAddress();
    const sc_1 = sc_0.loadRef().beginParse();
    const _trigger_wallet = sc_1.loadAddress();
    const _price = sc_1.loadCoins();
    const _buyer_fee_bps = sc_1.loadUintBig(16);
    const _admin_fee_bps = sc_1.loadUintBig(16);
    const _subscription_id = sc_1.loadUintBig(64);
    const _overage_held = sc_1.loadCoins();
    const _accumulated_paid = sc_1.loadCoins();
    return { $$type: 'PaymentVault$Data' as const, admin_wallet: _admin_wallet, platform_wallet: _platform_wallet, log_address: _log_address, trigger_wallet: _trigger_wallet, price: _price, buyer_fee_bps: _buyer_fee_bps, admin_fee_bps: _admin_fee_bps, subscription_id: _subscription_id, overage_held: _overage_held, accumulated_paid: _accumulated_paid };
}

export function loadTuplePaymentVault$Data(source: TupleReader) {
    const _admin_wallet = source.readAddress();
    const _platform_wallet = source.readAddress();
    const _log_address = source.readAddress();
    const _trigger_wallet = source.readAddress();
    const _price = source.readBigNumber();
    const _buyer_fee_bps = source.readBigNumber();
    const _admin_fee_bps = source.readBigNumber();
    const _subscription_id = source.readBigNumber();
    const _overage_held = source.readBigNumber();
    const _accumulated_paid = source.readBigNumber();
    return { $$type: 'PaymentVault$Data' as const, admin_wallet: _admin_wallet, platform_wallet: _platform_wallet, log_address: _log_address, trigger_wallet: _trigger_wallet, price: _price, buyer_fee_bps: _buyer_fee_bps, admin_fee_bps: _admin_fee_bps, subscription_id: _subscription_id, overage_held: _overage_held, accumulated_paid: _accumulated_paid };
}

export function loadGetterTuplePaymentVault$Data(source: TupleReader) {
    const _admin_wallet = source.readAddress();
    const _platform_wallet = source.readAddress();
    const _log_address = source.readAddress();
    const _trigger_wallet = source.readAddress();
    const _price = source.readBigNumber();
    const _buyer_fee_bps = source.readBigNumber();
    const _admin_fee_bps = source.readBigNumber();
    const _subscription_id = source.readBigNumber();
    const _overage_held = source.readBigNumber();
    const _accumulated_paid = source.readBigNumber();
    return { $$type: 'PaymentVault$Data' as const, admin_wallet: _admin_wallet, platform_wallet: _platform_wallet, log_address: _log_address, trigger_wallet: _trigger_wallet, price: _price, buyer_fee_bps: _buyer_fee_bps, admin_fee_bps: _admin_fee_bps, subscription_id: _subscription_id, overage_held: _overage_held, accumulated_paid: _accumulated_paid };
}

export function storeTuplePaymentVault$Data(source: PaymentVault$Data) {
    const builder = new TupleBuilder();
    builder.writeAddress(source.admin_wallet);
    builder.writeAddress(source.platform_wallet);
    builder.writeAddress(source.log_address);
    builder.writeAddress(source.trigger_wallet);
    builder.writeNumber(source.price);
    builder.writeNumber(source.buyer_fee_bps);
    builder.writeNumber(source.admin_fee_bps);
    builder.writeNumber(source.subscription_id);
    builder.writeNumber(source.overage_held);
    builder.writeNumber(source.accumulated_paid);
    return builder.build();
}

export function dictValueParserPaymentVault$Data(): DictionaryValue<PaymentVault$Data> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storePaymentVault$Data(src)).endCell());
        },
        parse: (src) => {
            return loadPaymentVault$Data(src.loadRef().beginParse());
        }
    }
}

export type VaultConfig = {
    $$type: 'VaultConfig';
    admin_wallet: Address;
    platform_wallet: Address;
    log_address: Address;
    trigger_wallet: Address;
    price: bigint;
    buyer_fee_bps: bigint;
    admin_fee_bps: bigint;
    subscription_id: bigint;
}

export function storeVaultConfig(src: VaultConfig) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeAddress(src.admin_wallet);
        b_0.storeAddress(src.platform_wallet);
        b_0.storeAddress(src.log_address);
        const b_1 = new Builder();
        b_1.storeAddress(src.trigger_wallet);
        b_1.storeCoins(src.price);
        b_1.storeUint(src.buyer_fee_bps, 16);
        b_1.storeUint(src.admin_fee_bps, 16);
        b_1.storeUint(src.subscription_id, 64);
        b_0.storeRef(b_1.endCell());
    };
}

export function loadVaultConfig(slice: Slice) {
    const sc_0 = slice;
    const _admin_wallet = sc_0.loadAddress();
    const _platform_wallet = sc_0.loadAddress();
    const _log_address = sc_0.loadAddress();
    const sc_1 = sc_0.loadRef().beginParse();
    const _trigger_wallet = sc_1.loadAddress();
    const _price = sc_1.loadCoins();
    const _buyer_fee_bps = sc_1.loadUintBig(16);
    const _admin_fee_bps = sc_1.loadUintBig(16);
    const _subscription_id = sc_1.loadUintBig(64);
    return { $$type: 'VaultConfig' as const, admin_wallet: _admin_wallet, platform_wallet: _platform_wallet, log_address: _log_address, trigger_wallet: _trigger_wallet, price: _price, buyer_fee_bps: _buyer_fee_bps, admin_fee_bps: _admin_fee_bps, subscription_id: _subscription_id };
}

export function loadTupleVaultConfig(source: TupleReader) {
    const _admin_wallet = source.readAddress();
    const _platform_wallet = source.readAddress();
    const _log_address = source.readAddress();
    const _trigger_wallet = source.readAddress();
    const _price = source.readBigNumber();
    const _buyer_fee_bps = source.readBigNumber();
    const _admin_fee_bps = source.readBigNumber();
    const _subscription_id = source.readBigNumber();
    return { $$type: 'VaultConfig' as const, admin_wallet: _admin_wallet, platform_wallet: _platform_wallet, log_address: _log_address, trigger_wallet: _trigger_wallet, price: _price, buyer_fee_bps: _buyer_fee_bps, admin_fee_bps: _admin_fee_bps, subscription_id: _subscription_id };
}

export function loadGetterTupleVaultConfig(source: TupleReader) {
    const _admin_wallet = source.readAddress();
    const _platform_wallet = source.readAddress();
    const _log_address = source.readAddress();
    const _trigger_wallet = source.readAddress();
    const _price = source.readBigNumber();
    const _buyer_fee_bps = source.readBigNumber();
    const _admin_fee_bps = source.readBigNumber();
    const _subscription_id = source.readBigNumber();
    return { $$type: 'VaultConfig' as const, admin_wallet: _admin_wallet, platform_wallet: _platform_wallet, log_address: _log_address, trigger_wallet: _trigger_wallet, price: _price, buyer_fee_bps: _buyer_fee_bps, admin_fee_bps: _admin_fee_bps, subscription_id: _subscription_id };
}

export function storeTupleVaultConfig(source: VaultConfig) {
    const builder = new TupleBuilder();
    builder.writeAddress(source.admin_wallet);
    builder.writeAddress(source.platform_wallet);
    builder.writeAddress(source.log_address);
    builder.writeAddress(source.trigger_wallet);
    builder.writeNumber(source.price);
    builder.writeNumber(source.buyer_fee_bps);
    builder.writeNumber(source.admin_fee_bps);
    builder.writeNumber(source.subscription_id);
    return builder.build();
}

export function dictValueParserVaultConfig(): DictionaryValue<VaultConfig> {
    return {
        serialize: (src, builder) => {
            builder.storeRef(beginCell().store(storeVaultConfig(src)).endCell());
        },
        parse: (src) => {
            return loadVaultConfig(src.loadRef().beginParse());
        }
    }
}

 type PaymentVault_init_args = {
    $$type: 'PaymentVault_init_args';
    admin_wallet: Address;
    platform_wallet: Address;
    log_address: Address;
    trigger_wallet: Address;
    price: bigint;
    buyer_fee_bps: bigint;
    admin_fee_bps: bigint;
    subscription_id: bigint;
}

function initPaymentVault_init_args(src: PaymentVault_init_args) {
    return (builder: Builder) => {
        const b_0 = builder;
        b_0.storeAddress(src.admin_wallet);
        b_0.storeAddress(src.platform_wallet);
        b_0.storeAddress(src.log_address);
        const b_1 = new Builder();
        b_1.storeAddress(src.trigger_wallet);
        b_1.storeInt(src.price, 257);
        b_1.storeInt(src.buyer_fee_bps, 257);
        const b_2 = new Builder();
        b_2.storeInt(src.admin_fee_bps, 257);
        b_2.storeInt(src.subscription_id, 257);
        b_1.storeRef(b_2.endCell());
        b_0.storeRef(b_1.endCell());
    };
}

async function PaymentVault_init(admin_wallet: Address, platform_wallet: Address, log_address: Address, trigger_wallet: Address, price: bigint, buyer_fee_bps: bigint, admin_fee_bps: bigint, subscription_id: bigint) {
    const __code = Cell.fromHex('b5ee9c7241021201000465000228ff008e88f4a413f4bcf2c80bed5320e303ed43d90109020271020702037b20030501c0abe5ed44d0d200018e20fa40fa40fa40d401d0fa40fa00d30fd30fd33ffa00fa0030107a107910786c1a8e2ffa40fa40fa40d401d0fa40810101d700810101d700d430d0810101d700810101d7003010581057105608d155067020e2db3c6ca10400165354a8812710a9045260a001c0aafded44d0d200018e20fa40fa40fa40d401d0fa40fa00d30fd30fd33ffa00fa0030107a107910786c1a8e2ffa40fa40fa40d401d0fa40810101d700810101d700d430d0810101d700810101d7003010581057105608d155067020e2db3c6ca10600022101c1bc936f6a268690000c7107d207d207d206a00e87d207d006987e987e99ffd007d0018083d083c883c360d4717fd207d207d206a00e87d20408080eb80408080eb806a1868408080eb80408080eb8018082c082b882b0468aa833810716d9e36544080010547987547987539801f63001d072d721d200d200fa4021103450666f04f86102f862ed44d0d200018e20fa40fa40fa40d401d0fa40fa00d30fd30fd33ffa00fa0030107a107910786c1a8e2ffa40fa40fa40d401d0fa40810101d700810101d700d430d0810101d700810101d7003010581057105608d155067020e20b925f0be009d70d1f0a0238f2e082218210b94c4535bae3020182107f1710bfbae3025f0bf2c0820b0f04f65b5da8812710a9045340a0820afaf080a0f8416f24135f031ca0530bb9e3025353a8812710a9045360a103a0521da151bba0707072882e51374133146d50436d5033c8cf8580ca00cf8440ce01fa028069cf40025c6e016eb0935bcf819d58cf8680cf8480f400f400cf81e2f400c901fb007072882d03561141330c10100d01e0313a82080f424072705300f823260456104434c8555082103cf20b7a5007cb1f15cb3f5003fa0201fa0201fa0201fa02cb1fc92855205a6d6d40037fc8cf8580ca00cf8440ce01fa028069cf40025c6e016eb0935bcf819d58cf8680cf8480f400f400cf81e2f400c901fb00107955161101fe146d50436d5033c8cf8580ca00cf8440ce01fa028069cf40025c6e016eb0935bcf819d58cf8680cf8480f400f400cf81e2f400c901fb0082080f424072f82327055e3302111102111001c8555082103cf20b7a5007cb1f15cb3f5003fa0201fa0201fa0201fa02cb1fc928431350dd5a6d6d40037fc8cf8580ca00cf8440ce0e00a001fa028069cf40025c6e016eb0935bcf819d58cf8680cf8480f400f400cf81e2f400c901fb0010795516c87f01ca005590509ace17ce15ce03c8ce58fa02cb0f12cb0f12cb3f58fa0258fa02cdc9ed5402d4fa403082009ba9f84227c705f2f48200eadf2ac200f2f40982084c4b40a182008e9d21c200f2f470707288104d103d146d50436d5033c8cf8580ca00cf8440ce01fa028069cf40025c6e016eb0935bcf819d58cf8680cf8480f400f400cf81e2f400c901fb001079551610110000004cc87f01ca005590509ace17ce15ce03c8ce58fa02cb0f12cb0f12cb3f58fa0258fa02cdc9ed5421884f80');
    const builder = beginCell();
    builder.storeUint(0, 1);
    initPaymentVault_init_args({ $$type: 'PaymentVault_init_args', admin_wallet, platform_wallet, log_address, trigger_wallet, price, buyer_fee_bps, admin_fee_bps, subscription_id })(builder);
    const __data = builder.endCell();
    return { code: __code, data: __data };
}

export const PaymentVault_errors = {
    2: { message: "Stack underflow" },
    3: { message: "Stack overflow" },
    4: { message: "Integer overflow" },
    5: { message: "Integer out of expected range" },
    6: { message: "Invalid opcode" },
    7: { message: "Type check error" },
    8: { message: "Cell overflow" },
    9: { message: "Cell underflow" },
    10: { message: "Dictionary error" },
    11: { message: "'Unknown' error" },
    12: { message: "Fatal error" },
    13: { message: "Out of gas error" },
    14: { message: "Virtualization error" },
    32: { message: "Action list is invalid" },
    33: { message: "Action list is too long" },
    34: { message: "Action is invalid or not supported" },
    35: { message: "Invalid source address in outbound message" },
    36: { message: "Invalid destination address in outbound message" },
    37: { message: "Not enough Toncoin" },
    38: { message: "Not enough extra currencies" },
    39: { message: "Outbound message does not fit into a cell after rewriting" },
    40: { message: "Cannot process a message" },
    41: { message: "Library reference is null" },
    42: { message: "Library change action error" },
    43: { message: "Exceeded maximum number of cells in the library or the maximum depth of the Merkle tree" },
    50: { message: "Account state size exceeded limits" },
    128: { message: "Null reference exception" },
    129: { message: "Invalid serialization prefix" },
    130: { message: "Invalid incoming message" },
    131: { message: "Constraints error" },
    132: { message: "Access denied" },
    133: { message: "Contract stopped" },
    134: { message: "Invalid argument" },
    135: { message: "Code of a contract was not found" },
    136: { message: "Invalid standard address" },
    138: { message: "Not a basechain address" },
    36509: { message: "Overage too small to cover refund gas" },
    39849: { message: "Unauthorized: only trigger_wallet" },
    60127: { message: "No overage to refund" },
} as const

export const PaymentVault_errors_backward = {
    "Stack underflow": 2,
    "Stack overflow": 3,
    "Integer overflow": 4,
    "Integer out of expected range": 5,
    "Invalid opcode": 6,
    "Type check error": 7,
    "Cell overflow": 8,
    "Cell underflow": 9,
    "Dictionary error": 10,
    "'Unknown' error": 11,
    "Fatal error": 12,
    "Out of gas error": 13,
    "Virtualization error": 14,
    "Action list is invalid": 32,
    "Action list is too long": 33,
    "Action is invalid or not supported": 34,
    "Invalid source address in outbound message": 35,
    "Invalid destination address in outbound message": 36,
    "Not enough Toncoin": 37,
    "Not enough extra currencies": 38,
    "Outbound message does not fit into a cell after rewriting": 39,
    "Cannot process a message": 40,
    "Library reference is null": 41,
    "Library change action error": 42,
    "Exceeded maximum number of cells in the library or the maximum depth of the Merkle tree": 43,
    "Account state size exceeded limits": 50,
    "Null reference exception": 128,
    "Invalid serialization prefix": 129,
    "Invalid incoming message": 130,
    "Constraints error": 131,
    "Access denied": 132,
    "Contract stopped": 133,
    "Invalid argument": 134,
    "Code of a contract was not found": 135,
    "Invalid standard address": 136,
    "Not a basechain address": 138,
    "Overage too small to cover refund gas": 36509,
    "Unauthorized: only trigger_wallet": 39849,
    "No overage to refund": 60127,
} as const

const PaymentVault_types: ABIType[] = [
    {"name":"DataSize","header":null,"fields":[{"name":"cells","type":{"kind":"simple","type":"int","optional":false,"format":257}},{"name":"bits","type":{"kind":"simple","type":"int","optional":false,"format":257}},{"name":"refs","type":{"kind":"simple","type":"int","optional":false,"format":257}}]},
    {"name":"SignedBundle","header":null,"fields":[{"name":"signature","type":{"kind":"simple","type":"fixed-bytes","optional":false,"format":64}},{"name":"signedData","type":{"kind":"simple","type":"slice","optional":false,"format":"remainder"}}]},
    {"name":"StateInit","header":null,"fields":[{"name":"code","type":{"kind":"simple","type":"cell","optional":false}},{"name":"data","type":{"kind":"simple","type":"cell","optional":false}}]},
    {"name":"Context","header":null,"fields":[{"name":"bounceable","type":{"kind":"simple","type":"bool","optional":false}},{"name":"sender","type":{"kind":"simple","type":"address","optional":false}},{"name":"value","type":{"kind":"simple","type":"int","optional":false,"format":257}},{"name":"raw","type":{"kind":"simple","type":"slice","optional":false}}]},
    {"name":"SendParameters","header":null,"fields":[{"name":"mode","type":{"kind":"simple","type":"int","optional":false,"format":257}},{"name":"body","type":{"kind":"simple","type":"cell","optional":true}},{"name":"code","type":{"kind":"simple","type":"cell","optional":true}},{"name":"data","type":{"kind":"simple","type":"cell","optional":true}},{"name":"value","type":{"kind":"simple","type":"int","optional":false,"format":257}},{"name":"to","type":{"kind":"simple","type":"address","optional":false}},{"name":"bounce","type":{"kind":"simple","type":"bool","optional":false}}]},
    {"name":"MessageParameters","header":null,"fields":[{"name":"mode","type":{"kind":"simple","type":"int","optional":false,"format":257}},{"name":"body","type":{"kind":"simple","type":"cell","optional":true}},{"name":"value","type":{"kind":"simple","type":"int","optional":false,"format":257}},{"name":"to","type":{"kind":"simple","type":"address","optional":false}},{"name":"bounce","type":{"kind":"simple","type":"bool","optional":false}}]},
    {"name":"DeployParameters","header":null,"fields":[{"name":"mode","type":{"kind":"simple","type":"int","optional":false,"format":257}},{"name":"body","type":{"kind":"simple","type":"cell","optional":true}},{"name":"value","type":{"kind":"simple","type":"int","optional":false,"format":257}},{"name":"bounce","type":{"kind":"simple","type":"bool","optional":false}},{"name":"init","type":{"kind":"simple","type":"StateInit","optional":false}}]},
    {"name":"StdAddress","header":null,"fields":[{"name":"workchain","type":{"kind":"simple","type":"int","optional":false,"format":8}},{"name":"address","type":{"kind":"simple","type":"uint","optional":false,"format":256}}]},
    {"name":"VarAddress","header":null,"fields":[{"name":"workchain","type":{"kind":"simple","type":"int","optional":false,"format":32}},{"name":"address","type":{"kind":"simple","type":"slice","optional":false}}]},
    {"name":"BasechainAddress","header":null,"fields":[{"name":"hash","type":{"kind":"simple","type":"int","optional":true,"format":257}}]},
    {"name":"Pay","header":3108783413,"fields":[]},
    {"name":"PaymentLog","header":1022495610,"fields":[{"name":"subscription_id","type":{"kind":"simple","type":"uint","optional":false,"format":64}},{"name":"amount_paid","type":{"kind":"simple","type":"uint","optional":false,"format":"coins"}},{"name":"admin_amount","type":{"kind":"simple","type":"uint","optional":false,"format":"coins"}},{"name":"platform_amount","type":{"kind":"simple","type":"uint","optional":false,"format":"coins"}},{"name":"overage","type":{"kind":"simple","type":"uint","optional":false,"format":"coins"}},{"name":"timestamp","type":{"kind":"simple","type":"uint","optional":false,"format":32}}]},
    {"name":"Refund","header":2132218047,"fields":[{"name":"recipient","type":{"kind":"simple","type":"address","optional":false}}]},
    {"name":"PaymentVault$Data","header":null,"fields":[{"name":"admin_wallet","type":{"kind":"simple","type":"address","optional":false}},{"name":"platform_wallet","type":{"kind":"simple","type":"address","optional":false}},{"name":"log_address","type":{"kind":"simple","type":"address","optional":false}},{"name":"trigger_wallet","type":{"kind":"simple","type":"address","optional":false}},{"name":"price","type":{"kind":"simple","type":"uint","optional":false,"format":"coins"}},{"name":"buyer_fee_bps","type":{"kind":"simple","type":"uint","optional":false,"format":16}},{"name":"admin_fee_bps","type":{"kind":"simple","type":"uint","optional":false,"format":16}},{"name":"subscription_id","type":{"kind":"simple","type":"uint","optional":false,"format":64}},{"name":"overage_held","type":{"kind":"simple","type":"uint","optional":false,"format":"coins"}},{"name":"accumulated_paid","type":{"kind":"simple","type":"uint","optional":false,"format":"coins"}}]},
    {"name":"VaultConfig","header":null,"fields":[{"name":"admin_wallet","type":{"kind":"simple","type":"address","optional":false}},{"name":"platform_wallet","type":{"kind":"simple","type":"address","optional":false}},{"name":"log_address","type":{"kind":"simple","type":"address","optional":false}},{"name":"trigger_wallet","type":{"kind":"simple","type":"address","optional":false}},{"name":"price","type":{"kind":"simple","type":"uint","optional":false,"format":"coins"}},{"name":"buyer_fee_bps","type":{"kind":"simple","type":"uint","optional":false,"format":16}},{"name":"admin_fee_bps","type":{"kind":"simple","type":"uint","optional":false,"format":16}},{"name":"subscription_id","type":{"kind":"simple","type":"uint","optional":false,"format":64}}]},
]

const PaymentVault_opcodes = {
    "Pay": 3108783413,
    "PaymentLog": 1022495610,
    "Refund": 2132218047,
}

const PaymentVault_getters: ABIGetter[] = [
    {"name":"config","methodId":103021,"arguments":[],"returnType":{"kind":"simple","type":"VaultConfig","optional":false}},
    {"name":"overage","methodId":91901,"arguments":[],"returnType":{"kind":"simple","type":"int","optional":false,"format":257}},
    {"name":"required_payment","methodId":91109,"arguments":[],"returnType":{"kind":"simple","type":"int","optional":false,"format":257}},
]

export const PaymentVault_getterMapping: { [key: string]: string } = {
    'config': 'getConfig',
    'overage': 'getOverage',
    'required_payment': 'getRequiredPayment',
}

const PaymentVault_receivers: ABIReceiver[] = [
    {"receiver":"internal","message":{"kind":"typed","type":"Pay"}},
    {"receiver":"internal","message":{"kind":"typed","type":"Refund"}},
]


export class PaymentVault implements Contract {
    
    public static readonly MIN_GAS_RESERVE = 50000000n;
    public static readonly storageReserve = 0n;
    public static readonly errors = PaymentVault_errors_backward;
    public static readonly opcodes = PaymentVault_opcodes;
    
    static async init(admin_wallet: Address, platform_wallet: Address, log_address: Address, trigger_wallet: Address, price: bigint, buyer_fee_bps: bigint, admin_fee_bps: bigint, subscription_id: bigint) {
        return await PaymentVault_init(admin_wallet, platform_wallet, log_address, trigger_wallet, price, buyer_fee_bps, admin_fee_bps, subscription_id);
    }
    
    static async fromInit(admin_wallet: Address, platform_wallet: Address, log_address: Address, trigger_wallet: Address, price: bigint, buyer_fee_bps: bigint, admin_fee_bps: bigint, subscription_id: bigint) {
        const __gen_init = await PaymentVault_init(admin_wallet, platform_wallet, log_address, trigger_wallet, price, buyer_fee_bps, admin_fee_bps, subscription_id);
        const address = contractAddress(0, __gen_init);
        return new PaymentVault(address, __gen_init);
    }
    
    static fromAddress(address: Address) {
        return new PaymentVault(address);
    }
    
    readonly address: Address; 
    readonly init?: { code: Cell, data: Cell };
    readonly abi: ContractABI = {
        types:  PaymentVault_types,
        getters: PaymentVault_getters,
        receivers: PaymentVault_receivers,
        errors: PaymentVault_errors,
    };
    
    constructor(address: Address, init?: { code: Cell, data: Cell }) {
        this.address = address;
        this.init = init;
    }
    
    async send(provider: ContractProvider, via: Sender, args: { value: bigint, bounce?: boolean| null | undefined }, message: Pay | Refund) {
        
        let body: Cell | null = null;
        if (message && typeof message === 'object' && !(message instanceof Slice) && message.$$type === 'Pay') {
            body = beginCell().store(storePay(message)).endCell();
        }
        if (message && typeof message === 'object' && !(message instanceof Slice) && message.$$type === 'Refund') {
            body = beginCell().store(storeRefund(message)).endCell();
        }
        if (body === null) { throw new Error('Invalid message type'); }
        
        await provider.internal(via, { ...args, body: body });
        
    }
    
    async getConfig(provider: ContractProvider) {
        const builder = new TupleBuilder();
        const source = (await provider.get('config', builder.build())).stack;
        const result = loadGetterTupleVaultConfig(source);
        return result;
    }
    
    async getOverage(provider: ContractProvider) {
        const builder = new TupleBuilder();
        const source = (await provider.get('overage', builder.build())).stack;
        const result = source.readBigNumber();
        return result;
    }
    
    async getRequiredPayment(provider: ContractProvider) {
        const builder = new TupleBuilder();
        const source = (await provider.get('required_payment', builder.build())).stack;
        const result = source.readBigNumber();
        return result;
    }
    
}
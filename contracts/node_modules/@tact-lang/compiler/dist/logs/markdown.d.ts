declare const aligns: {
    readonly left: readonly ["padEnd", " "];
    readonly right: readonly ["padStart", ":"];
};
export type Align = keyof typeof aligns;
interface Table<L extends string, M> {
    end: (t: Record<L, M>[]) => M;
    add: <K extends string>(field: K, title: M, align: Align) => Table<K | L, M>;
}
export interface Markdown<M> {
    table: Table<never, M>;
    text: (s: string) => M;
    t: (s: TemplateStringsArray) => M;
    section: (title: M, body: M) => M;
    pre: (text: M) => M;
}
export type GetRow<T> = T extends (x: (infer R)[]) => infer _ ? R : never;
export declare const md: Markdown<string>;
export {};

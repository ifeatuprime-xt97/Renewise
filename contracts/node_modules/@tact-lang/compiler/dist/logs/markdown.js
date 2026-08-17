"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.md = void 0;
const aligns = { left: ["padEnd", " "], right: ["padStart", ":"] };
const build = (display) => ({
    end: (t) => display(t).flat().join("\n") + "\n",
    add: (field, title, align) => build((table) => {
        const column = table.map((row) => row[field]);
        const width = column.reduce((acc, cell) => Math.max(acc, cell.length), title.length);
        const [pad, char] = aligns[align];
        const [header, sep, body] = display(table);
        return [
            `${header} ${title[pad](width)} |`,
            `${sep} ${"-".repeat(width)}${char}|`,
            column.flatMap((cell, i) => body[i] ? [`${body[i]} ${cell[pad](width)} |`] : []),
        ];
    }),
});
const table = build((rows) => ["|", "|", rows.map(() => "|")]);
const text = (s) => s.replace(/([\\*|_])/g, (x) => `\\${x}`);
const subst = (s) => text(s.join(""));
const section = (title, body) => `## ${title}\n${body}`;
const pre = (s) => {
    const maxLen = [...s.matchAll(/(`+)/g)]
        // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition
        .reduce((acc, [_, m]) => Math.max(acc, m?.length ?? 0), 0);
    const quote = "`".repeat(1 + maxLen);
    return `${quote}${s}${quote}`;
};
exports.md = {
    table,
    text,
    t: subst,
    section,
    pre,
};

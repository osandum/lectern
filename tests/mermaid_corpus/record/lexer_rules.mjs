// Dev-time only: record mermaid's compiled flowchart lexer in
// ../recorded/flowchart-lexer.json -- every rule's regex as jison compiled
// it, the rules each state has active, the rule actions, and the token
// names. build-aux/mermaid/generate.py turns it into Lectern's lexer.
//
//   node lexer_rules.mjs
//
// The lexer is taken from the compiled form, not transcribed from
// flow.jison, because jison doesn't compile it the way it reads: an
// unescaped `-` or `=` in a lookahead never matches, and `(?!\n|;|#)`
// comes out as `(?!\n||)`.

import { readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const here = dirname(fileURLToPath(import.meta.url));
const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
const { default: mermaid } = await import("mermaid");
mermaid.initialize({ startOnLoad: false, logLevel: "fatal" });
const version = JSON.parse(await readFile(join(here, "node_modules/mermaid/package.json"), "utf8")).version;

const diagram = await mermaid.mermaidAPI.getDiagramFromText("flowchart LR\n  A");
const parser = diagram.parser.parser ?? diagram.parser;
const lexer = parser.lexer;
const out = join(here, "..", "recorded", "flowchart-lexer.json");
await writeFile(out, JSON.stringify({
  mermaid: version,
  rules: lexer.rules.map((r) => r.source),
  conditions: Object.fromEntries(Object.entries(lexer.conditions).map(
    ([state, c]) => [state, { rules: c.rules, inclusive: c.inclusive }])),
  options: lexer.options,
  performAction: lexer.performAction.toString(),
  terminals: parser.terminals_,
}, null, 1) + "\n");
console.log(`flowchart lexer: ${lexer.rules.length} rules, mermaid ${version}`);

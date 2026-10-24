// Dev-time only: dump mermaid's compiled flowchart lexer -- every rule's
// regex as jison compiled it, and the rules each state has active -- as
// JSON on stdout. Jison's compilation differs from how flow.jison reads
// (an unescaped `-` or `=` in a lookahead never matches), so the lexer is
// taken from the compiled form, not transcribed from the source.

import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
const { default: mermaid } = await import("mermaid");
mermaid.initialize({ startOnLoad: false, logLevel: "fatal" });

const diagram = await mermaid.mermaidAPI.getDiagramFromText("flowchart LR\n  A");
const parser = diagram.parser.parser ?? diagram.parser;
const lexer = parser.lexer;
console.log(JSON.stringify({
  rules: lexer.rules.map((r) => r.source),
  conditions: Object.fromEntries(Object.entries(lexer.conditions).map(
    ([state, c]) => [state, { rules: c.rules, inclusive: c.inclusive }])),
  options: lexer.options,
  performAction: lexer.performAction.toString(),
  terminals: parser.terminals_,
}, null, 1));

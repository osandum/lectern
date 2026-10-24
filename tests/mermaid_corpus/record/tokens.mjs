// Dev-time only: the tokens mermaid's own flowchart lexer makes of a
// source, the ground truth for how its lexer states behave.
//
//   node tokens.mjs 'flowchart LR
//     A -- text --> B'

import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
const { default: mermaid } = await import("mermaid");
mermaid.initialize({ startOnLoad: false, logLevel: "fatal" });

const diagram = await mermaid.mermaidAPI.getDiagramFromText("flowchart LR\n  A");
const parser = diagram.parser.parser ?? diagram.parser;
const lexer = parser.lexer;
const names = Object.fromEntries(Object.entries(parser.symbols_).map(([name, id]) => [id, name]));
diagram.db.clear();
const yy = { ...parser.yy, lex: { firstGraph: () => true } };
// The text as mermaid's parser sees it: front matter, directives and
// comments have already gone, and entities are encoded.
lexer.setInput(process.argv[2].replace(/}\s*\n/g, "}\n") + "\n", yy);
for (;;) {
  let token;
  try {
    token = lexer.lex();
  } catch (error) {
    console.log("LEXICAL ERROR", error.message.split("\n")[0]);
    break;
  }
  const name = names[token] ?? token;
  if (name === "EOF" || token === 1) break;
  console.log(name.padEnd(18), JSON.stringify(lexer.yytext), lexer.conditionStack.at(-1));
}

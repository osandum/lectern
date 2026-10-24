// Dev-time only: collect the diagram sources mermaid's own parser specs
// feed its parser, into ../cases/mermaid-specs.<family>.json.
//
// The specs are fetched at the tag of the pinned mermaid and read
// statically -- every `….parse(<string>)` or `Diagram.fromText(<string>)`
// call whose argument is a string literal, a template literal without
// substitutions, a `+` of those, or a variable last assigned one of them.
// Running the specs instead would mean building mermaid's monorepo;
// sources assembled at run time (loops, it.each tables) are counted as
// skipped rather than guessed at.
//
//   node mine.mjs

import { readFile, writeFile, mkdir } from "node:fs/promises";
import { dirname, basename, join } from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as walk from "acorn-walk";

const here = dirname(fileURLToPath(import.meta.url));
const version = JSON.parse(await readFile(join(here, "package.json"), "utf8"))
  .dependencies.mermaid;
const TAG = `mermaid@${version}`;
const BASE = "packages/mermaid/src/diagrams";

const SPECS = {
  flowchart: [
    "flowchart/parser/flow.spec.js",
    "flowchart/parser/flow-arrows.spec.js",
    "flowchart/parser/flow-comments.spec.js",
    "flowchart/parser/flow-direction.spec.js",
    "flowchart/parser/flow-edges.spec.js",
    "flowchart/parser/flow-interactions.spec.js",
    "flowchart/parser/flow-lines.spec.js",
    "flowchart/parser/flow-md-string.spec.js",
    "flowchart/parser/flow-node-data.spec.js",
    "flowchart/parser/flow-singlenode.spec.js",
    "flowchart/parser/flow-style.spec.js",
    "flowchart/parser/flow-text.spec.js",
    "flowchart/parser/flow-vertice-chaining.spec.js",
    "flowchart/parser/subgraph.spec.js",
  ],
  sequence: ["sequence/sequenceDiagram.spec.js"],
  class: ["class/parser/class.spec.js", "class/classDiagram.spec.ts"],
  er: ["er/parser/erDiagram.spec.js"],
};

async function fetchSpec(path) {
  const cached = join(here, "specs", TAG, path);
  try {
    return await readFile(cached, "utf8");
  } catch {
    const url = `https://raw.githubusercontent.com/mermaid-js/mermaid/${TAG}/${BASE}/${path}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`${url}: ${response.status}`);
    const text = await response.text();
    await mkdir(dirname(cached), { recursive: true });
    await writeFile(cached, text);
    return text;
  }
}

// The string a node evaluates to, if it is a constant one; else null.
// `lookup(name)` gives a variable's constant value at that point, or null.
function constant(node, lookup = () => null) {
  if (node.type === "Literal" && typeof node.value === "string") return node.value;
  if (node.type === "Identifier") return lookup(node.name);
  if (node.type === "TemplateLiteral") {
    let out = node.quasis[0].value.cooked;
    for (let i = 0; i < node.expressions.length; i++) {
      const value = constant(node.expressions[i], lookup);
      if (value === null) return null;
      out += value + node.quasis[i + 1].value.cooked;
    }
    return out;
  }
  if (node.type === "BinaryExpression" && node.operator === "+") {
    const left = constant(node.left, lookup);
    const right = constant(node.right, lookup);
    return left !== null && right !== null ? left + right : null;
  }
  return null;
}

// TypeScript spec files: strip what acorn can't read. Only type
// annotations on simple declarations, `as` casts and `!` non-null
// assertions appear in the ones listed; a file that still won't parse is
// reported, not half-read.
function untype(text) {
  return text
    .replace(/([\w)\]])!(?=[.)\[,;])/g, "$1")
    .replace(/\s+satisfies\s+[^;]+(?=;)/g, "")
    .replace(/^import type .*$/gm, "")
    .replace(/(\b(?:let|const|var)\s+\w+)\s*:\s*[\w.<>[\]|, ]+(?=\s*[=;])/g, "$1")
    .replace(/\)\s*:\s*[\w.<>[\]|, ]+\s*=>/g, ") =>")
    .replace(/\s+as\s+[\w.<>[\]|]+/g, "")
    .replace(/(\(\s*\w+)\s*:\s*[\w.<>[\]|]+/g, "$1");
}

for (const [family, paths] of Object.entries(SPECS)) {
  const cases = [];
  const seen = new Set();
  let skipped = 0;
  for (const path of paths) {
    let text = await fetchSpec(path);
    if (path.endsWith(".ts")) text = untype(text);
    let ast;
    try {
      ast = acorn.parse(text, { ecmaVersion: "latest", sourceType: "module", locations: true });
    } catch (error) {
      console.error(`${path}: unreadable (${error.message}), skipped`);
      continue;
    }
    // Every assignment to a plain variable, in source order: a variable
    // read somewhere takes the last assignment before that point. One that
    // isn't a constant (`str += x`) makes the variable unknown.
    const assigned = [];
    walk.full(ast, (node) => {
      if (node.type === "VariableDeclarator" && node.id.type === "Identifier" && node.init)
        assigned.push({ name: node.id.name, at: node.start, value: node.init });
      else if (node.type === "AssignmentExpression" && node.left.type === "Identifier")
        assigned.push({ name: node.left.name, at: node.start,
                        value: node.operator === "=" ? node.right : null });
    });
    assigned.sort((a, b) => a.at - b.at);
    const valueAt = (name, at) => {
      let last = null;
      for (const entry of assigned) {
        if (entry.at >= at) break;
        if (entry.name === name) last = entry;
      }
      if (!last || !last.value) return null;
      return constant(last.value, (variable) => valueAt(variable, last.at));
    };
    const counts = {};
    walk.full(ast, (node) => {
      if (node.type !== "CallExpression" || !node.arguments.length) return;
      const callee = node.callee;
      const name = callee.type === "MemberExpression" ? callee.property.name : callee.name;
      if (name !== "parse" && name !== "fromText") return;
      const argument = node.arguments[0];
      const source = constant(argument, (variable) => valueAt(variable, node.start));
      if (source === null) {
        skipped += 1;
        return;
      }
      if (seen.has(source)) return;
      seen.add(source);
      const line = node.loc.start.line;
      const key = `${basename(path)}:${line}`;
      counts[key] = (counts[key] || 0) + 1;
      const id = counts[key] > 1 ? `${key}#${counts[key]}` : key;
      cases.push({ id, origin: `${TAG}/${BASE}/${path}#L${line}`, source });
    });
  }
  cases.sort((a, b) => a.origin.localeCompare(b.origin, "en", { numeric: true }));
  const out = join(here, "..", "cases", `mermaid-specs.${family}.json`);
  await mkdir(dirname(out), { recursive: true });
  await writeFile(out, JSON.stringify(cases, null, 2) + "\n");
  console.log(`${family}: ${cases.length} cases, ${skipped} built at run time and skipped`);
}

// Dev-time only: run every corpus case through mermaid's own parser and
// write what it made of it to ../recorded/<family>.json, which the tests
// compare Lectern's parsers against. CI never runs this; it reads the
// committed recordings.
//
//   npm install && node record.mjs
//
// Only the parts of mermaid's model Lectern models too are kept: ids,
// labels, shapes, edges, membership. Render state (DOM ids, styles,
// positions) would only make the recordings churn. A source mermaid
// rejects is recorded as its error: refusing it is something to conform
// to as well.

import { readFile, readdir, writeFile, mkdir } from "node:fs/promises";
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

const values = (collection) => (collection instanceof Map ? [...collection.values()] : Object.values(collection));
const defined = (object) => Object.fromEntries(Object.entries(object).filter(([, v]) => v !== undefined));

const EXTRACT = {
  flowchart: (db) => ({
    direction: db.getDirection(),
    nodes: values(db.getVertices()).map((v) => defined({
      id: v.id, text: v.text, type: v.type, labelType: v.labelType, link: v.link,
    })),
    edges: db.getEdges().map((e) => defined({
      start: e.start, end: e.end, type: e.type, stroke: e.stroke, text: e.text,
      labelType: e.labelType, length: e.length,
    })),
    subgraphs: db.getSubGraphs().map((s) => ({
      id: s.id, title: s.title, nodes: [...s.nodes], labelType: s.labelType,
    })),
  }),
  sequence: (db) => ({
    actors: values(db.getActors()).map((a) => defined({
      name: a.name, description: a.description, type: a.type,
    })),
    messages: db.getMessages().map((m) => defined({
      type: m.type, from: m.from, to: m.to, message: m.message,
      placement: m.placement, activate: m.activate || undefined,
    })),
  }),
  class: (db) => ({
    direction: db.getDirection(),
    classes: values(db.getClasses()).map((c) => defined({
      id: c.id, label: c.label, type: c.type || undefined,
      annotations: c.annotations,
      members: c.members.map((m) => m.text),
      methods: c.methods.map((m) => m.text),
      link: c.link,
    })),
    relations: db.getRelations().map((r) => ({
      id1: r.id1, id2: r.id2, type1: r.relation.type1, type2: r.relation.type2,
      lineType: r.relation.lineType, title: r.title,
      relationTitle1: r.relationTitle1, relationTitle2: r.relationTitle2,
    })),
    notes: values(db.getNotes()).map((n) => ({ class: n.class, text: n.text })),
    namespaces: values(db.getNamespaces()).map((n) => ({
      id: n.id, classes: [...(n.classes?.keys?.() ?? [])],
    })),
  }),
  er: (db) => {
    const entities = db.getEntities();
    const names = new Map([...entities].map(([name, e]) => [e.id, name]));
    return {
      direction: db.getDirection(),
      entities: [...entities].map(([name, e]) => ({
        name, alias: e.alias, attributes: e.attributes.map((a) => defined({
          type: a.type, name: a.name, keys: a.keys?.length ? a.keys : undefined, comment: a.comment || undefined,
        })),
      })),
      relationships: db.getRelationships().map((r) => ({
        entityA: names.get(r.entityA) ?? r.entityA, entityB: names.get(r.entityB) ?? r.entityB,
        roleA: r.roleA, cardA: r.relSpec.cardA, cardB: r.relSpec.cardB, relType: r.relSpec.relType,
      })),
    };
  },
};

// mermaid's diagram type -> the family above that models it.
const FAMILY = {
  "flowchart-v2": "flowchart", flowchart: "flowchart", sequence: "sequence",
  classDiagram: "class", class: "class", er: "er",
};

const casesDir = join(here, "..", "cases");
const byFamily = {};
for (const file of (await readdir(casesDir)).filter((f) => f.endsWith(".json")).sort()) {
  const family = file.split(".").at(-2);
  for (const c of JSON.parse(await readFile(join(casesDir, file), "utf8")))
    (byFamily[family] ??= []).push(c);
}

for (const [family, cases] of Object.entries(byFamily)) {
  const results = {};
  for (const { id, source } of cases) {
    if (id in results) throw new Error(`duplicate case id ${id}`);
    try {
      const diagram = await mermaid.mermaidAPI.getDiagramFromText(source);
      // A case of another type than its file's (a spec testing the type
      // detection) records just the type.
      results[id] = FAMILY[diagram.type] === family
        ? { type: diagram.type, ...EXTRACT[family](diagram.db) }
        : { type: diagram.type };
    } catch (error) {
      results[id] = { error: String(error.message ?? error).split("\n")[0] };
    }
  }
  const out = join(here, "..", "recorded", `${family}.json`);
  await mkdir(dirname(out), { recursive: true });
  await writeFile(out, JSON.stringify({ mermaid: version, results }, null, 1) + "\n");
  const failed = Object.values(results).filter((r) => r.error).length;
  console.log(`${family}: ${cases.length} recorded, ${failed} rejected by mermaid`);
}

# Mermaid conformance corpus

Diagram sources, and what mermaid's own parser makes of each, so that
`tests/test_mermaid_conformance.py` can check Lectern's parsers against
mermaid's without running mermaid. See #41.

- `cases/mermaid-specs.<family>.json`: sources mined from mermaid's
  parser specs at the pinned version (`record/mine.mjs`). Generated, so
  don't edit them by hand.
- `cases/lectern.<family>.json`: Lectern's own cases, such as regressions
  and fixture diagrams. Add new cases here.
- `recorded/<family>.json`: mermaid's reading of every case, written by
  `record/record.mjs`. Generated and committed, so CI needs no Node.
- `deviations.json`: cases where Lectern currently deviates from
  mermaid, with the reason for each. Their tests are expected to fail.
- `conformance.py`: reduces both sides to the same plain form and
  compares them. Run it directly to see where each family stands:
  `python tests/mermaid_corpus/conformance.py`.

Only real or illustrative diagrams belong here. Nothing private: this
repository is public.

## Adding a case, or moving to a new mermaid

Both need Node, at dev time only.

```sh
cd tests/mermaid_corpus/record
npm install
node mine.mjs      # only when the pinned mermaid in package.json changes
node record.mjs    # after adding cases, or after changing the pin
```

Then run the tests. A case that newly deviates either gets fixed or
goes into `deviations.json`, with its reason.

Two more tools look inside mermaid's flowchart lexer. `node tokens.mjs
'<source>'` prints the tokens mermaid's lexer makes of a source, with
the lexer state after each. `node lexer_rules.mjs` dumps the compiled
lexer as JSON: every rule's regex, the rules each state has active, and
the rule actions. Read the compiled rules rather than `flow.jison`,
because jison doesn't compile them the way they read. For example, an
unescaped `-` or `=` inside a lookahead never matches, and jison drops
the `;` and `#` from `(?!\n|;|#)`.

## Outcomes

| Outcome | Meaning | Test |
|---|---|---|
| agree | both read it the same way, or both refuse it | passes |
| unsupported | mermaid reads it, Lectern refuses it and falls back to the source | passes: Lectern draws a subset by design |
| rejected | mermaid refuses it, Lectern draws something | fails |
| differs | both read it, differently: a silent mis-parse | fails |

The cases mined from mermaid's specs are copies of mermaid's test
sources, under mermaid's MIT license: see `cases/LICENSE.mermaid`.

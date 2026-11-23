#!/usr/bin/env python3
"""Generate the flowchart and state parsers' tables. Dev-time only: the
output is committed, so nothing here runs in a build or at run time.

    generate.py lexer   recorded/<family>-lexer.json -> _<family>_lexer.py
    generate.py tables  <family>.lark -> _<family>_tables.py (needs Lark)

for each family (flowchart, state), or only the one `--family` names.

`--check` compares with the committed file instead of writing it, which
is what tests/test_mermaid_generated.py does.

The lexer is mermaid's own, translated from its compiled form, which
tests/mermaid_corpus/record/lexer_rules.mjs records (with Node). The
parse table is Lark's LALR(1) table for the family's grammar, written out as
plain data, so the parser needs neither Lark nor its runtime: parsing.py
runs both tables.
"""
import argparse
import json
import pathlib
import pprint
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "lectern" / "mermaid"
RECORDED = ROOT / "tests" / "mermaid_corpus" / "recorded"


# -- the lexer -------------------------------------------------------------

# What JavaScript's \s matches: WhiteSpace and LineTerminator. Python's
# own \s differs (it takes \x1c-\x1f and \x85, and not ﻿).
_JS_SPACE = r"\t\n\v\f\r    -     　﻿"


def python_regex(pattern):
    """A regex as JavaScript reads it (no flags), spelled for Python's re
    with re.ASCII. JavaScript's \\b, \\w and \\d are ASCII, which re.ASCII
    gives; its \\s is Unicode and its `.` excludes four line terminators,
    which are spelled out; and its `$` is the end of the input."""
    out, i, in_class = [], 0, False
    while i < len(pattern):
        c = pattern[i]
        if not in_class and pattern.startswith(r"[\s\S]", i):
            out.append(r"[\s\S]")       # any character at all, in either
            i += len(r"[\s\S]")
            continue
        if c == "\\" and i + 1 < len(pattern):
            escape = pattern[i + 1]
            if escape == "s":
                out.append(_JS_SPACE if in_class else f"[{_JS_SPACE}]")
            elif escape == "S":
                if in_class:
                    raise ValueError(f"\\S in a class: {pattern!r}")
                out.append(f"[^{_JS_SPACE}]")
            else:
                out.append(c + escape)
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
            out.append(c)
        elif c == "[":
            in_class = True
            out.append(c)
            # A `]` or `^]` right after the opening is a literal.
            if pattern[i + 1:i + 2] == "^":
                out.append("^")
                i += 1
            if pattern[i + 1:i + 2] == "]":
                out.append("\\]")
                i += 1
        elif c == ".":
            out.append(r"[^\n\r  ]")
        elif c == "$":
            out.append(r"\Z")
        else:
            out.append(c)
        i += 1
    return "".join(out)


_STATEMENT = re.compile(r"""
    (?P<first>if\s*\(yy\.lex\.firstGraph\(\)\)\s*\{\s*this\.begin\("(?P<fstate>\w+)"\);\s*\})
  | this\.begin\("(?P<begin>\w+)"\);
  | this\.pushState\("(?P<push>\w+)"\);
  | (?P<pop>this\.popState\(\);)
  | return\s+(?P<ret>\d+|"[^"]*"|'[^']*');
  | (?P<empty>yy_\.yytext\s*=\s*"";)
  | (?P<pid>if\s*\(!processId\(\)\)\s*return;)
  | (?P<slice>yy_\.yytext\s*=\s*yy_\.yytext\.(?:slice\((?P<a>-?\d+),\s*(?P<b>-?\d+)\)
                                              |substr\((?P<c>\d+)\))(?P<strim>\.trim\(\))?;)
  | (?P<trim>yy_\.yytext\s*=\s*yy_\.yytext\.trim\(\);)
  | (?P<throw>throw\s+new\s+Error\((?P<message>'[^']*')[^;]*;)
  | (?P<br>const\s+re\s*=\s*/\\n\\s\*/g;\s*yy_\.yytext\s*=\s*yy_\.yytext\.replace\(re,\s*"<br/>"\);)
  | (?P<brk>break;)
  | (?P<ws>\s+)
""", re.X)

# jison's token names -> the grammars'. Anything not listed keeps its
# jison name, upper-cased with its punctuation spelled out (Lark wants a
# terminal's name in capitals).
_FLOWCHART_NAMES = {"(-": "ELLIPSESTART", "-)": "ELLIPSEEND", "subgraph": "SUBGRAPH", "end": "END",
          "acc_title": "ACC_TITLE", "acc_title_value": "ACC_TITLE_VALUE",
          "acc_descr": "ACC_DESCR", "acc_descr_value": "ACC_DESCR_VALUE",
          "acc_descr_multiline_value": "ACC_DESCR_MULTILINE_VALUE",
          "direction_tb": "DIRECTION", "direction_bt": "DIRECTION", "direction_rl": "DIRECTION",
          "direction_lr": "DIRECTION", "direction_td": "DIRECTION"}
_STATE_NAMES = {"-->": "ARROW", ";": "SEMI", "classDef": "CLASSDEF"}
_PUNCTUATION = {"-->": "ARROW", ";": "SEMI"}


def _token_name(family, name):
    if family == "flowchart":
        return _FLOWCHART_NAMES.get(name, name)
    return _STATE_NAMES.get(name, _PUNCTUATION.get(name, name.upper()))


def lexer_module(family):
    dump = json.loads((RECORDED / f"{family}-lexer.json").read_text())
    terminals = {int(k): v for k, v in dump["terminals"].items()}
    cases = {int(m.group(1)): m.group(2) for m in re.finditer(
        r"case (\d+):(.*?)(?=case \d+:|default:|\}\s*\}\s*$)", dump["performAction"], re.S)}
    actions = []
    for index in range(len(dump["rules"])):
        code, steps, token, pos = cases.get(index, ""), [], None, 0
        while pos < len(code):
            match = _STATEMENT.match(code, pos)
            if not match:
                raise SystemExit(f"rule {index}: unknown action {code[pos:pos + 60]!r}")
            pos = match.end()
            if match.group("first"):
                steps.append(("begin_once", match.group("fstate")))
            elif match.group("begin") or match.group("push"):
                steps.append(("push", match.group("begin") or match.group("push")))
            elif match.group("pop"):
                steps.append(("pop",))
            elif match.group("empty"):
                steps.append(("text", ""))
            elif match.group("br"):
                steps.append(("br",))
            elif match.group("pid"):
                steps.append(("process_id",))
            elif match.group("slice"):
                if match.group("c") is not None:
                    steps.append(("slice", int(match.group("c")), None))
                else:
                    steps.append(("slice", int(match.group("a")), int(match.group("b"))))
                if match.group("strim"):
                    steps.append(("trim",))
            elif match.group("trim"):
                steps.append(("trim",))
            elif match.group("throw"):
                steps.append(("error", match.group("message")[1:-1]))
                break
            elif match.group("ret"):
                value = match.group("ret")
                name = terminals[int(value)] if value.isdigit() else value[1:-1]
                token = _token_name(family, name)
                break
        actions.append((tuple(steps), token))
    rules = []
    for source in dump["rules"]:
        if not (source.startswith("^(?:") and source.endswith(")")):
            raise SystemExit(f"unexpected rule shape {source!r}")
        rules.append(python_regex(source[len("^(?:"):-1]))
    conditions = {state: c["rules"] for state, c in dump["conditions"].items()}
    flags = (["# jison's case-insensitive option: every rule is matched ignoring case.",
              "IGNORECASE = True", ""] if dump["options"].get("case-insensitive") else [])
    return "\n".join([
        f'"""mermaid {dump["mermaid"]}\'s {family} lexer, from its compiled form.',
        "",
        "Generated by build-aux/mermaid/generate.py from",
        f'tests/mermaid_corpus/recorded/{family}-lexer.json. Don\'t edit."""',
        "",
        f"MERMAID = {dump['mermaid']!r}",
        "",
        *flags,
        "# Each rule's pattern, for re with re.ASCII.",
        f"RULES = {pprint.pformat(rules, width=100)}",
        "",
        "# Each rule's (state changes, token or None).",
        f"ACTIONS = {pprint.pformat(actions, width=100)}",
        "",
        "# Lexer state -> its rules, in the order they are tried.",
        f"CONDITIONS = {pprint.pformat(conditions, width=100, compact=True)}",
        "",
    ])


# -- the parse table -------------------------------------------------------

def tables_module(family):
    from lark import Lark
    from lark.lexer import Lexer

    class _Tokens(Lexer):        # the tokens come from elsewhere
        def __init__(self, conf):
            pass

        def lex(self, data):
            return iter(())

    grammar = (PACKAGE / f"{family}.lark").read_text()
    lark = Lark(grammar, parser="lalr", lexer=_Tokens)
    table = lark.parser.parser.parser.parse_table
    # Lark numbers its states in an order that follows Python's string
    # hashing, which differs from run to run. So the rules are sorted and
    # the states renumbered breadth-first from the start, in symbol order:
    # the same grammar always writes the same file.
    rules = sorted(lark.rules, key=lambda r: (str(r.origin.name), [str(s.name) for s in r.expansion],
                                              str(r.alias)))
    number = {rule: i for i, rule in enumerate(rules)}
    start = table.start_states["start"]
    renumbered, queue = {start: 0}, [start]
    for state in queue:
        for symbol, (kind, arg) in sorted(table.states[state].items(), key=lambda a: str(a[0])):
            if kind.name == "Shift" and arg not in renumbered:
                renumbered[arg] = len(renumbered)
                queue.append(arg)
    states = {}
    for state in queue:
        states[renumbered[state]] = {
            str(symbol): (renumbered[arg] if kind.name == "Shift" else ~number[arg])
            for symbol, (kind, arg) in sorted(table.states[state].items(), key=lambda a: str(a[0]))}
    # How each rule's value is made: "splice" for Lark's helper rules
    # (x*, x+, `_rule`), whose children join the parent's; "inline:" for
    # a `?rule`, which is its only child when it has one; otherwise the
    # action of that name.
    shapes = []
    for rule in rules:
        name = str(rule.origin.name)
        if name.startswith("_"):
            how = "splice"
        elif rule.options.expand1:
            how = "inline:" + name
        else:
            how = str(rule.alias or name)
        shapes.append((name, len(rule.expansion), how))
    return "\n".join([
        f'"""The {family} parser\'s LALR(1) table, for parsing.parse.',
        "",
        f"Generated by build-aux/mermaid/generate.py from {family}.lark, with",
        'Lark. Don\'t edit."""',
        "",
        "START = 0",
        f"END = {renumbered[table.end_states['start']]}",
        "",
        "# Rule -> (name, length, how its value is made).",
        f"RULES = {pprint.pformat(shapes, width=100)}",
        "",
        "# State -> {symbol: state to shift or go to, or ~rule to reduce by}.",
        f"STATES = {pprint.pformat(states, width=100, compact=True)}",
        "",
    ])


FAMILIES = ("flowchart", "state")
TARGETS = {
    "lexer": (lexer_module, "_{}_lexer.py"),
    "tables": (tables_module, "_{}_tables.py"),
}


def target(kind, family):
    """(a function making the module's text, the path it goes to)."""
    make, name = TARGETS[kind]
    return (lambda: make(family)), PACKAGE / name.format(family)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("target", choices=TARGETS)
    parser.add_argument("--family", choices=FAMILIES)
    parser.add_argument("--check", action="store_true",
                        help="fail if the committed file isn't what would be generated")
    args = parser.parse_args()
    for family in [args.family] if args.family else FAMILIES:
        make, path = target(args.target, family)
        text = make()
        if args.check:
            if path.read_text() != text:
                sys.exit(f"{path.name} is out of date: run build-aux/mermaid/generate.py {args.target}")
            continue
        path.write_text(text)
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

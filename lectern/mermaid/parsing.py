"""Running a generated mermaid parser: a jison-style lexer over a table of
rules, and an LALR(1) parser over a parse table.

Both tables are generated (build-aux/mermaid/generate.py) and committed;
this module only runs them, and knows nothing about any one diagram type.
The lexer is mermaid's own, taken from its compiled form, because what
mermaid accepts is decided as much by its lexer as by its grammar: jison
lexes ahead of the parser, taking in each lexer state the first rule that
matches whether or not the parser can use the token -- which is where
most of mermaid's quirks and most of its rejections come from. The parse
table is Lark's, built from a grammar transcribed from mermaid's, and
written out as plain data so that Lark is needed to build it, not to run
it.
"""
import re

from .common import Unsupported


class Token:
    __slots__ = ("type", "value", "pos")

    def __init__(self, type, value, pos):
        self.type = type
        self.value = value
        self.pos = pos

    def __repr__(self):
        return f"Token({self.type!r}, {self.value!r})"


# Compiled lazily: a diagram reaches only some of a lexer's rules, and
# one of them is a character class covering most of Unicode.
_compiled = {}


def _rule(pattern):
    regex = _compiled.get(pattern)
    if regex is None:
        regex = _compiled[pattern] = re.compile(pattern, re.ASCII)
    return regex


def lex(lexer, text):
    """The tokens jison's lexer would make of `text`, given a generated
    lexer table. In each state, the first of its rules that matches wins
    -- jison's default, not the longest match. Raises Unsupported where
    jison's lexer would fail."""
    stack = ["INITIAL"]
    first = True        # a rule's begin_once happens the first time only
    pos = stalled = 0
    while True:
        for index in lexer.CONDITIONS[stack[-1]]:
            match = _rule(lexer.RULES[index]).match(text, pos)
            if match:
                break
        else:
            raise Unsupported(f"lexical error at {text[pos:pos + 16]!r}")
        value = match.group()
        steps, token = lexer.ACTIONS[index]
        for step in steps:
            if step[0] == "push":
                stack.append(step[1])
            elif step[0] == "pop":
                if len(stack) > 1:
                    stack.pop()
            elif step[0] == "begin_once":
                if first:
                    first = False
                    stack.append(step[1])
            elif step[0] == "text":
                value = step[1]
            elif step[0] == "br":
                value = re.sub(r"\n\s*", "<br/>", value)
        # An empty match that changes nothing would match again forever.
        stalled = stalled + 1 if match.end() == pos else 0
        if stalled > 3:
            raise Unsupported(f"lexical error at {text[pos:pos + 16]!r}")
        pos = match.end()
        if token == "EOF":
            return
        if token is not None:
            yield Token(token, value, match.start())


class _Splice(list):
    """A helper rule's children, waiting to join its parent's."""


def parse(tables, tokens, actions):
    """Run a generated parse table over `tokens`, calling
    `actions.<name>(children)` for each reduction, in the order jison runs
    a grammar's actions. A rule with no method of its name reduces to
    None. Returns what the start rule reduced to. Raises Unsupported on a
    syntax error, as mermaid's parser would fail."""
    states, values = [tables.START], []
    for token in _with_end(tokens):
        at_end = token.type == "$END"
        while True:
            action = tables.STATES[states[-1]].get(token.type)
            if action is None:
                raise Unsupported(f"syntax error at {token.type} {token.value!r}")
            if action >= 0:          # shift; $END never is
                states.append(action)
                values.append(token)
                break
            name, size, how = tables.RULES[~action]
            children = []
            for child in values[len(values) - size:]:
                children.extend(child) if isinstance(child, _Splice) else children.append(child)
            del values[len(values) - size:]
            del states[len(states) - size:]
            if how == "splice":
                value = _Splice(children)
            elif how.startswith("inline:") and len(children) == 1:
                value = children[0]
            else:
                method = getattr(actions, how.removeprefix("inline:"), None)
                value = method(children) if method is not None else None
            values.append(value)
            states.append(tables.STATES[states[-1]][name])
            if at_end and states[-1] == tables.END:
                return values[-1]


def _with_end(tokens):
    yield from tokens
    yield Token("$END", "", -1)

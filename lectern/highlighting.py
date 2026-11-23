"""Pygments wrapper: turns fenced-code text into (text, tag_name) runs.

Deliberately does not embed a GtkSourceView per code block -- tagging text
directly in the shared TextBuffer is far cheaper for documents with many
fences, and keeps the print model (tags.py) as the single source of truth
for how a token type is colored on screen vs. on paper.

All `pygments` imports are deliberately deferred into highlight_runs()
(mirroring document.py's lazy markdown_it import) -- a document with no
fenced code blocks, or a bare `lectern` launch, shouldn't pay for pygments'
lexer registry at all.
"""
from .tags import PYGMENTS_TAG_NAMES

_token_tag_map = None  # built on first use by _build_token_tag_map()


def _build_token_tag_map():
    from pygments.token import Token

    # Ordered most-specific-first: `in` checks against Pygments'
    # hierarchical Token types, so a subtype (e.g.
    # Token.Name.Function.Magic) matches its parent bucket automatically.
    table = [
        (Token.Comment, "pyg-comment"),
        (Token.String, "pyg-string"),
        (Token.Number, "pyg-number"),
        (Token.Name.Builtin, "pyg-builtin"),
        (Token.Name.Function, "pyg-function"),
        (Token.Name.Class, "pyg-class"),
        (Token.Name.Decorator, "pyg-decorator"),
        (Token.Keyword, "pyg-keyword"),
        (Token.Operator, "pyg-operator"),
    ]
    # Derived, not hand-retyped -- tags.py's PYGMENTS_TAG_NAMES and this
    # map's target names are one fact checked two ways, not two facts kept
    # in sync by hand.
    assert {name for _base, name in table} == set(PYGMENTS_TAG_NAMES)
    return table


def _tag_for_token(ttype):
    for base, name in _token_tag_map:
        if ttype in base:
            return name
    return None


def highlight_runs(code, language):
    """Yield (text, tag_name_or_None) runs covering `code` exactly.

    A fence with no language, or one Pygments doesn't know, is a single
    untagged run, as on GitHub: there is no guessing. `guess_lexer` reads
    ASCII art full of `@@` as Transact-SQL and colours it at random, and
    callers can always just concatenate the yielded text and get `code`
    back unchanged.
    """
    global _token_tag_map
    from pygments import lex
    from pygments.lexers import get_lexer_by_name
    from pygments.util import ClassNotFound

    if _token_tag_map is None:
        _token_tag_map = _build_token_tag_map()

    lexer = None
    if language:
        try:
            lexer = get_lexer_by_name(language, stripnl=False, stripall=False)
        except ClassNotFound:
            lexer = None
    if lexer is None:
        yield code, None
        return
    for ttype, value in lex(code, lexer):
        if not value:
            continue
        yield value, _tag_for_token(ttype)

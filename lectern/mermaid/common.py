"""What more than one diagram parser needs, kept out of any one of them
so the others don't have to import a type they don't use."""
import re

_BR_RE = re.compile(r"<br\s*/?>", re.I)

# The link forms of mermaid's interaction statement, shared by flowcharts
# (`click`) and class diagrams (`click`, or `link` without the keyword):
#   click A href "url" ["tooltip"] [_target]
#   click A "url" ["tooltip"] [_target]
#   link A "url" ["tooltip"] [_target]
# The callback forms (`click A callback`, `click A call fn()`) name a
# JavaScript function in the page that hosts mermaid, which means nothing
# here -- they don't match, and the parsers go on ignoring them.
_CLICK_RE = re.compile(r"""
    ^(?:click\s+(?P<click_key>[\w-]+)\s+(?:href\s+)?
       |link\s+(?P<link_key>[\w-]+)\s+)
    "(?P<href>[^"]*)"
    (?:\s+"[^"]*")?
    (?:\s+_\w+)?
    \s*$
""", re.X | re.I)


class Unsupported(Exception):
    """This source is mermaid we don't draw. The caller (renderer.py)
    falls back to rendering the fence as a highlighted code block."""


def parse_label(text):
    """Unquote a label and turn mermaid's two line-break spellings into
    real newlines.

    Both are ordinary in hand-written diagrams: `<br>` because mermaid's
    labels are HTML, and a literal backslash-n because mermaid accepts it
    too (verified against mermaid 11 in a browser -- it splits the label
    into one line per `\\n`). Everything else is left exactly as written
    rather than half-stripped: showing a stray tag is at least visibly
    wrong, where quietly dropping the text inside one is not. Bold and
    italic tags survive to be drawn as such (text.label_markup).
    """
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    return _BR_RE.sub("\n", text).replace("\\n", "\n").strip()


def parse_click(statement):
    """`(node key, href)` for a statement that links a node to a URL, else
    None -- including for the callback forms, which have nowhere to go
    outside a browser. The tooltip and target are dropped: the window's
    link status shows where a link goes, and a link opens the way any
    other link in the document does."""
    match = _CLICK_RE.match(statement)
    if not match or not match.group("href"):
        return None
    return match.group("click_key") or match.group("link_key"), match.group("href")

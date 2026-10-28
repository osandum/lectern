"""What more than one diagram parser needs, kept out of any one of them
so the others don't have to import a type they don't use."""
import json
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


# `%%{init: {...}}%%`, mermaid's in-diagram configuration directive.
_DIRECTIVE_RE = re.compile(r"%%\{\s*(?:init|initialize)\s*:\s*(\{.*?\})\s*\}%%", re.S)
_BARE_KEY_RE = re.compile(r"([{,]\s*)([A-Za-z_][\w-]*)\s*:")
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


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
    return label_lines(text)


def label_lines(text):
    """`text` with mermaid's two line-break spellings turned into real
    newlines (see parse_label), and nothing else done to it -- quotes
    included, which only a flowchart label loses."""
    return _BR_RE.sub("\n", text.strip()).replace("\\n", "\n").strip()


# What mermaid does to every diagram's source before its parser sees it
# (preprocessDiagram, then Diagram.fromText).
_FRONT_MATTER_RE = re.compile(r"^-{3}\s*[\n\r](.*?[\n\r])-{3}\s*[\n\r]+", re.S)
_ANY_DIRECTIVE_RE = re.compile(
    r"%{2}{\s*(?:(\w+)\s*:|(\w+))\s*(?:(\w+)|((?:(?!}%{2}).|\r?\n)*))?\s*(?:}%{2})?", re.I)
_COMMENT_LINE_RE = re.compile(r"^\s*%%(?!{)[^\n]+\n?", re.M)
_TAG_RE = re.compile(r"<(\w+)([^>]*)>")


def preprocess(source):
    """A source as mermaid's parser gets it: line endings normalized,
    front matter, directives and whole-line `%%` comments removed, and
    `#name;` entities encoded so their `#` and `;` aren't read as syntax
    (decode_entities undoes that), with a newline appended."""
    text = source.replace("\r\n", "\n").replace("\r", "\n")
    text = _TAG_RE.sub(lambda m: "<" + m.group(1) + re.sub(r'="([^"]*)"', r"='\1'", m.group(2)) + ">",
                       text)
    text = _FRONT_MATTER_RE.sub("", text, count=1)
    text = _ANY_DIRECTIVE_RE.sub("", text)
    text = _COMMENT_LINE_RE.sub("", text).lstrip()
    text = re.sub(r"style.*:\S*#.*;", lambda m: m.group()[:-1], text)
    text = re.sub(r"classDef.*:\S*#.*;", lambda m: m.group()[:-1], text)
    text = re.sub(r"#\w+;", lambda m: ("ﬂ°°" if re.fullmatch(r"\+?\d+", m.group()[1:-1])
                                       else "ﬂ°") + m.group()[1:-1] + "¶ß", text)
    return text + "\n"


def decode_entities(text):
    """Undo preprocess's entity encoding, back to mermaid's own `#name;`
    spelling -- which label drawing decodes (text.label_markup)."""
    return text.replace("ﬂ°°", "#").replace("ﬂ°", "#").replace("¶ß", ";")


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


def parse_init(source):
    """The settings in a diagram's `%%{init: ...}%%` directives, merged
    (later wins), or {} if there are none.

    Mermaid reads the directive leniently -- single quotes and bare keys
    are as common in the wild as strict JSON -- so it is coaxed into JSON
    before parsing. One that still won't parse is dropped rather than
    failing the diagram: it is styling, and the diagram it styles is all
    there without it.
    """
    settings = {}
    for body in _DIRECTIVE_RE.findall(source):
        text = _TRAILING_COMMA_RE.sub(r"\1", _BARE_KEY_RE.sub(r'\1"\2":', body.replace("'", '"')))
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict):
            settings.update(value)
    return settings


def curve_setting(source, section, default):
    """The edge curve a directive asks `section` (e.g. "flowchart") for:
    "linear" as asked, mermaid's step curves as right angles, and every
    other curve as the basis spline -- they are all ways of rounding the
    corners, and a slightly different curve misstates nothing. `default`
    if none is asked for."""
    config = parse_init(source).get(section)
    curve = config.get("curve") if isinstance(config, dict) else None
    if curve is None:
        return default
    if curve == "linear":
        return "linear"
    if isinstance(curve, str) and curve.startswith("step"):
        return "orthogonal"
    return "basis"

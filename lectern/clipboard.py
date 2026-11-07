"""Copy: put text/html and a text/markdown flavour on the clipboard
alongside plain text, so pasting a Lectern selection into a rich-text-
aware app (mail, chat, office) or a Markdown editor keeps its
formatting. Gtk.TextView's default copy-clipboard handler only ever
puts plain text on the clipboard, discarding every Gtk.TextTag.

Images and diagrams get their real pixels embedded as a data: URI
directly in the HTML (any number of them, not just one -- see
_image_data_uri/_diagram_data_uri), and a loaded image additionally
gets its own image/png clipboard flavour when the selection is
*exactly* one (make_content_provider's image_texture, for a paste
target that only understands raw image data, not HTML at all -- no
well-defined single image/* payload exists for more than one).

Driven off the live Gtk.TextBuffer and its tags for the *selected
range only* -- unlike printing.py, which works off the whole
document's print_model instead. A selection is a buffer range, not a
print item, and print_model carries no buffer offsets to slice by, so
there's nothing to reuse there; tag names are the one place formatting
lives on screen, so that's what gets walked here too.

A table is reconstructed (a real <table> / GFM pipe table) from the
plain-text grid MarkdownRenderer.anchor_descriptors carries for it --
the buffer itself only holds a single object-replacement character
standing in for the whole thing, but a table's cell text is readily
available outside the buffer. Each cell is also already separately,
natively copyable on its own (every cell is a selectable Gtk.Label,
which binds Ctrl+C itself; see tables.py) -- this module's
reconstruction is for a selection spanning the *whole* table, not a
replacement for that.

A mermaid diagram has no reasonable *text* form at all -- rasterized
into a PNG data URI for HTML instead (same idea as an image's embedded
pixels, see _diagram_data_uri), but left out of the Markdown flavour
entirely: a bare base64 blob with no alt text worth keeping would only
bloat what's supposed to be readable source, unlike HTML, where the
src attribute is already URL-shaped machinery rather than human-facing
text.
"""
import base64
import html as html_lib
import math
import re

import gi
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, GObject

# Character styles a run's active tags are reduced to, in a fixed
# canonical order -- so two runs with the same *combination* always
# produce the same tuple regardless of which of their several tags
# Gtk.TextIter.get_tags() happened to report first (that order tracks
# tag-table insertion, not anything meaningful here). Table-header/
# pygments-token tags are deliberately not among them: they're
# presentation only, with no Markdown/HTML equivalent worth reproducing
# in a copied selection.
_INLINE_STYLES = ("strong", "em", "strike", "code-inline")
_HTML_TAGS = {"strong": "strong", "em": "em", "strike": "s", "code-inline": "code"}
_HEADING_NAMES = tuple(f"heading{n}" for n in range(1, 7))
_MARKER_TAGS = ("list-marker", "task-checked-glyph", "task-unchecked-glyph")

_ORDINAL_RE = re.compile(r"^\s*(\d+)\.\s*$")
_MD_ESCAPE_RE = re.compile(r"([\\`*_\[\]<>])")


# -- walking the buffer into lines -------------------------------------------

class _Line:
    __slots__ = ("path", "kind", "runs")

    def __init__(self, path, kind, runs):
        # `path`: nesting, outermost first -- () | ("bq",) for a
        # blockquote entry, ("list", level) for a list level, both
        # possibly repeated/combined for nested structures.
        self.path = path
        # `kind`: "paragraph" | "code-block" | "heading1".."heading6" --
        # what this line's *own* content is, independent of what
        # contains it.
        self.kind = kind
        # [("text", text, style_key), ...] | [("anchor", anchor), ...],
        # in document order. style_key is (styles_tuple, link_tag_or_
        # None, footnote_tag_or_None, marker_tag_or_None) -- see
        # _style_key.
        self.runs = runs


def _tag_names(it):
    return {t.get_property("name") or "" for t in it.get_tags()}


def _has_code_block(it, end):
    """True if `it` (within [.., end)) is itself "code-block"-tagged --
    used to peek at the character *after* a code-tagged newline, to
    tell an internal fence line break from the fence's true end."""
    return it.compare(end) < 0 and "code-block" in _tag_names(it)


def _nesting_path(names):
    path = []
    if "blockquote" in names:
        path.append(("bq",))
    levels = sorted({
        int(name.rsplit("-", 1)[1])
        for name in names
        if name.startswith("list-indent-") or name.startswith("list-body-")
    })
    path.extend(("list", level) for level in levels)
    return tuple(path)


def _line_kind(names):
    for name in _HEADING_NAMES:
        if name in names:
            return name
    return "code-block" if "code-block" in names else "paragraph"


def _style_key(names):
    styles = tuple(s for s in _INLINE_STYLES if s in names)
    link_id = next((n for n in names if n.startswith("link-")), None)
    footnote_id = next(
        (n for n in names if n.startswith("footnote-ref-") or n.startswith("footnote-back-")),
        None,
    )
    marker = next((m for m in _MARKER_TAGS if m in names), None)
    return (styles, link_id, footnote_id, marker)


def walk_lines(buffer, start, end):
    """[_Line, ...] for the buffer range [start, end).

    A "line" here is a run of buffer content between two untagged
    newlines -- every block terminator renderer.py inserts (paragraph,
    heading, hr, code-block, table, diagram, a marker-only list item)
    is a bare `buffer.insert(it, "\\n")` with no tags at all, which is
    otherwise never true: real content always carries at least "prose"
    (seeded into the root RenderCtx and inherited by everything, since
    push_block only ever appends). A hard line break within a paragraph
    is tagged the same as its surrounding text, so it doesn't split
    here -- it stays embedded as a literal "\\n" in one run's text,
    same as an internal code-block newline (recognisable by "code-block"
    remaining in its tag set, which no ordinary newline in prose carries
    at all).
    """
    lines = []
    it = start.copy()
    path = kind = None
    runs = []
    text_buf, text_key = [], None

    def flush_run():
        nonlocal text_buf, text_key
        if text_buf:
            runs.append(("text", "".join(text_buf), text_key))
        text_buf, text_key = [], None

    def flush_line():
        nonlocal runs, path, kind
        flush_run()
        if runs:
            lines.append(_Line(path or (), kind or "paragraph", runs))
        runs = []
        path = kind = None

    while it.compare(end) < 0:
        names = _tag_names(it)
        if "fence-caption" in names:
            # Lectern's own note on why a diagram wasn't drawn, not the
            # author's: it has no source form. Its bare terminator then
            # closes a line with no runs, which flush_line drops.
            it.forward_char()
            continue
        if path is None:
            path, kind = _nesting_path(names), _line_kind(names)
        anchor = it.get_child_anchor()
        nxt = it.copy()
        nxt.forward_char()
        if anchor is not None:
            flush_run()
            runs.append(("anchor", anchor))
            it = nxt
            continue
        ch = buffer.get_text(it, nxt, True)
        if ch == "\n" and (not names or ("code-block" in names and not _has_code_block(nxt, end))):
            # A block terminator: either bare (every ordinary block ends
            # this way -- paragraph, heading, hr, marker-only list item,
            # a fence whose own content didn't already end in "\n") or
            # the *last* tagged newline of a fence whose content already
            # did, which never got a bare one appended after it (see
            # renderer.py's _emit_code_block) and so has to be
            # recognised by what follows it instead.
            it = nxt
            flush_line()
            continue
        key = _style_key(names)
        if key != text_key and text_buf:
            flush_run()
        text_key = key
        text_buf.append(ch)
        it = nxt
    flush_line()
    return lines


def _marker_kind(line):
    """None, or one of "bullet"/"ordered"/"task-checked"/"task-unchecked"
    -- what _Line.runs[0] opens, if it's a marker run at all. A
    continuation line (e.g. a loose list item's second paragraph, tagged
    "list-body-N" rather than "list-indent-N") carries no marker run and
    yields None here, same as an ordinary paragraph."""
    if not line.runs or line.runs[0][0] != "text":
        return None
    _kind, text, key = line.runs[0]
    marker = key[3]
    if marker == "task-checked-glyph":
        return "task-checked"
    if marker == "task-unchecked-glyph":
        return "task-unchecked"
    if marker == "list-marker":
        return "ordered" if _ORDINAL_RE.match(text) else "bullet"
    return None


def _list_type(marker):
    """"bullet"/"ordered"/"task" (checked and unchecked share one type,
    since a real task list mixes both) -- or None for a continuation
    line's marker (there isn't one).

    A list level's tag name is reused verbatim across every *separate*
    list at that level in the whole document (level 0 is always
    "list-indent-0"/"list-body-0", whichever list it belongs to), so two
    back-to-back top-level lists with nothing else between them are
    otherwise indistinguishable from one continuous list sharing the
    same container -- there's no per-instance id to key off. A change
    of type is the one signal available for telling them apart; it
    doesn't catch two directly-adjacent lists of the *same* type (two
    bullet lists in a row, say), which is a known residual gap.
    """
    if marker in ("task-checked", "task-unchecked"):
        return "task"
    return marker


def _ordinal_text(line):
    match = _ORDINAL_RE.match(line.runs[0][1])
    return match.group(1) if match else "1"


# -- inline rendering ---------------------------------------------------------

def _content_runs(line, marker):
    """`line.runs`, minus its own marker run (rendered separately as the
    list-item/task-checkbox prefix, not as content).

    A task item's marker text is just its glyph, with none of the
    trailing space "•  "/"N. " bake in for a bullet/ordinal item --
    mdit_py_plugins' checkbox syntax ("[ ] "/"[x] ") consumes only the
    brackets, leaving its own space in the paragraph's actual text,
    which is what supplies the gap on screen. The marker/token this
    module writes back out already supplies one of its own (matching
    bullet/ordinal), so that leftover would otherwise double up.
    """
    if marker is None:
        return line.runs
    runs = line.runs[1:]
    if marker in ("task-checked", "task-unchecked") and runs and runs[0][0] == "text":
        _kind, text, key = runs[0]
        if text.startswith(" "):
            runs = [("text", text[1:], key)] + runs[1:]
    return runs


def _html_escape(text):
    return html_lib.escape(text, quote=False)


def _padded_rows(rows):
    """`rows`, every row padded out to the widest one with empty cells
    -- ragged input is possible (tables.py's layout and printing.py's
    tolerate it too), and a pipe table/<table> both need a rectangular
    grid."""
    width = max((len(row) for row in rows), default=0)
    return [row + [""] * (width - len(row)) for row in rows]


def _table_html(rows):
    rows = _padded_rows(rows)
    if not rows:
        return ""
    head, body = rows[0], rows[1:]
    thead = "<tr>" + "".join(f"<th>{_html_escape(c)}</th>" for c in head) + "</tr>"
    tbody = "".join(
        "<tr>" + "".join(f"<td>{_html_escape(c)}</td>" for c in row) + "</tr>"
        for row in body
    )
    return f"<table><thead>{thead}</thead><tbody>{tbody}</tbody></table>"


def _image_data_uri(view):
    """A data: URI for `view`'s already-loaded pixels, or None if it
    hasn't got any (a remote image the reader hasn't opted to fetch,
    or one that failed) -- see the module docstring on why this beats
    a src reference for *any* number of images, not just when the
    selection is exactly one.

    Confirmed against real mail-client behaviour, not just spec
    reading: composing a message in Thunderbird (pasting, or even just
    saving the draft) lifts each data: URI back out into its own
    multipart/related MIME part addressed by a generated cid:, rather
    than sending the URI verbatim -- exactly the point of a data: URI
    at this end, and further proof there's no reason to also try to
    reproduce that cid: rewriting here ourselves.
    """
    texture = view.texture if view is not None else None
    if texture is None:
        return None
    encoded = base64.b64encode(texture.save_to_png_bytes().get_data()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _diagram_data_uri(view):
    """A data: URI rasterizing `view`'s already-built mermaid scene, or
    None if it hasn't got a usable one. Cairo/mermaid are imported
    here rather than at module level for the same reason renderer.py
    defers them: most documents have no diagrams, and this only runs
    at copy time regardless.

    Always the light palette against a white background, regardless of
    Lectern's current theme: this is content headed somewhere else
    entirely (an email, a document), and the dark palette's light-on-
    dark text would risk landing illegible against whatever background
    the receiving app actually has, rather than the one it was drawn
    for.
    """
    scene = view.scene if view is not None else None
    if scene is None or scene.width <= 0 or scene.height <= 0:
        return None
    import io
    import cairo
    from . import mermaid
    from . import tags as tagdefs
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, math.ceil(scene.width), math.ceil(scene.height))
    cr = cairo.Context(surface)
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    try:
        mermaid.draw.draw_scene(cr, scene, tagdefs.diagram_palette(dark=False), mermaid.ui_font())
    except Exception:
        # Same reasoning as printing.py's _diagram_block: a diagram
        # that trips over a layout bug here must not take the whole
        # copy down with it.
        return None
    buf = io.BytesIO()
    surface.write_to_png(buf)
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


def _anchor_html(anchor, descriptors):
    info = descriptors.get(anchor)
    kind = info["kind"] if info else None
    if kind == "hr":
        return "<hr>"
    if kind == "image":
        # A data: URI travels with the clipboard payload itself, unlike
        # the original src: a relative path resolves against *this*
        # document's own directory (images.py), which means nothing to
        # whatever the paste target resolves it against -- pasting a
        # multi-image selection into a rich-text app otherwise shows
        # broken images even though the HTML/Markdown is well-formed.
        src = _image_data_uri(info.get("view")) or _html_escape(info["src"])
        return f'<img src="{src}" alt="{_html_escape(info["alt"])}">'
    if kind == "table":
        return _table_html(info["rows"])
    if kind == "diagram":
        uri = _diagram_data_uri(info.get("view"))
        return f'<img src="{uri}" alt="diagram">' if uri else ""
    return ""


def _html_layers(styles, link_id):
    """The cross-run tag stack this run wants open, outermost first --
    same shape and same reason as clipboard.py's markdown counterpart,
    _md_layers: reusing an already-open tag across adjacent runs rather
    than closing and reopening it isn't a *correctness* issue for HTML
    the way it is for Markdown's ambiguous-looking delimiters, but it's
    needlessly verbose without it, and the two renderers might as well
    agree on how nesting works."""
    layers = [("link", link_id)] if link_id else []
    layers.extend(("style", s) for s in styles)
    return tuple(layers)


def _inline_html(runs, dispatch_targets, anchor_descriptors):
    parts = []
    open_layers = ()  # currently open, outermost first

    def close_to(common):
        nonlocal open_layers
        for layer in reversed(open_layers[common:]):
            parts.append("</a>" if layer[0] == "link" else f"</{_HTML_TAGS[layer[1]]}>")
        open_layers = open_layers[:common]

    def open_from(common, target):
        nonlocal open_layers
        for layer in target[common:]:
            if layer[0] == "link":
                href = dispatch_targets.get(layer[1], {}).get("href", "")
                parts.append(f'<a href="{_html_escape(href)}">')
            else:
                parts.append(f"<{_HTML_TAGS[layer[1]]}>")
        open_layers = target

    for run in runs:
        if run[0] == "anchor":
            close_to(0)
            parts.append(_anchor_html(run[1], anchor_descriptors))
            continue
        _kind, text, key = run
        styles, link_id, footnote_id, _marker = key
        target = _html_layers(styles, link_id)
        common = 0
        while (common < len(open_layers) and common < len(target)
               and open_layers[common] == target[common]):
            common += 1
        close_to(common)
        open_from(common, target)
        rendered = _html_escape(text).replace("\n", "<br>\n")
        parts.append(f"<sup>{rendered}</sup>" if footnote_id else rendered)
    close_to(0)
    return "".join(parts)


def _md_escape(text):
    return _MD_ESCAPE_RE.sub(r"\\\1", text)


def _md_code_span(text):
    """CommonMark code-span delimiter: one more backtick than the
    longest backtick run already in `text`, padded with a space on
    each side if that would otherwise merge with a leading/trailing
    backtick (or the content is all spaces)."""
    longest = max((len(m.group()) for m in re.finditer("`+", text)), default=0)
    fence = "`" * (longest + 1)
    if text.startswith("`") or text.endswith("`") or (text and text.strip() == ""):
        text = f" {text} "
    return f"{fence}{text}{fence}"


def _md_code_fence(text):
    longest = max((len(m.group()) for m in re.finditer("`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _md_table_cell(text):
    # _md_escape already backslash-escapes a literal backslash, so it's
    # safe to escape "|" (not one of _md_escape's own characters, a
    # pipe table's own delimiter rather than markdown's) afterwards
    # without risking a double-escape. GFM table cells can't contain a
    # real line break either.
    return _md_escape(text).replace("|", "\\|").replace("\n", " ")


def _table_markdown(rows):
    rows = _padded_rows(rows)
    if not rows:
        return ""
    escaped = [[_md_table_cell(cell) for cell in row] for row in rows]
    widths = [max(3, max(len(row[i]) for row in escaped)) for i in range(len(escaped[0]))]

    def format_row(row):
        return "| " + " | ".join(cell.ljust(width) for cell, width in zip(row, widths)) + " |"

    lines = [format_row(escaped[0]), "| " + " | ".join("-" * w for w in widths) + " |"]
    lines.extend(format_row(row) for row in escaped[1:])
    return "\n".join(lines)


def _anchor_markdown(anchor, descriptors):
    info = descriptors.get(anchor)
    kind = info["kind"] if info else None
    if kind == "hr":
        return "---"
    if kind == "image":
        return f'![{info["alt"]}]({info["src"]})'
    if kind == "table":
        return _table_markdown(info["rows"])
    return ""  # diagram: see module docstring


# Delimiter-based styles participate in a cross-run open/close stack
# (see _inline_markdown); code-inline deliberately doesn't. A code_inline
# node is always a leaf -- its content comes from one indivisible
# `child.content` in renderer.py's _walk_inline, never split across
# several runs the way strong/em/link's *nested* content can be -- so it
# can only ever be a single run's own, self-contained wrapping, applied
# fresh around just that run's text without needing to coordinate with
# whatever comes before or after it.
_MD_STACK_STYLES = ("strong", "em", "strike")
_MD_DELIM = {"strong": "**", "em": "*", "strike": "~~"}


def _md_layers(styles, link_id):
    """The cross-run stack this run wants open, outermost first: a link
    wraps around its styled text, so it always goes first."""
    layers = [("link", link_id)] if link_id else []
    layers.extend(("style", s) for s in styles if s in _MD_STACK_STYLES)
    return tuple(layers)


def _inline_markdown(runs, dispatch_targets, anchor_descriptors):
    """Renders `runs` with a shared open/close stack across them (see
    _md_layers), rather than wrapping each run's delimiters in
    isolation -- two adjacent runs that are each, say, "strong" (one
    plain, the next also "em") otherwise close and reopen "**" back to
    back, e.g. "**bold ** ***nested italic***" collapsing into a run of
    five bare asterisks, which is no longer unambiguously parseable as
    the same markdown back.
    """
    parts = []
    open_layers = ()  # currently open, outermost first

    def close_to(common):
        nonlocal open_layers
        for layer in reversed(open_layers[common:]):
            if layer[0] == "link":
                href = dispatch_targets.get(layer[1], {}).get("href", "")
                parts.append(f"]({href})")
            else:
                parts.append(_MD_DELIM[layer[1]])
        open_layers = open_layers[:common]

    def open_from(common, target):
        nonlocal open_layers
        for layer in target[common:]:
            parts.append("[" if layer[0] == "link" else _MD_DELIM[layer[1]])
        open_layers = target

    for run in runs:
        if run[0] == "anchor":
            close_to(0)
            parts.append(_anchor_markdown(run[1], anchor_descriptors))
            continue
        _kind, text, key = run
        styles, link_id, footnote_id, _marker = key
        if footnote_id and footnote_id.startswith("footnote-back-"):
            continue  # the "return to reference" arrow has no source form
        if footnote_id and footnote_id.startswith("footnote-ref-"):
            close_to(0)
            parts.append(f"[^{text}]")
            continue
        target = _md_layers(styles, link_id)
        common = 0
        while (common < len(open_layers) and common < len(target)
               and open_layers[common] == target[common]):
            common += 1
        close_to(common)
        open_from(common, target)
        if "code-inline" in styles:
            parts.append(_md_code_span(text))
        else:
            parts.append(_md_escape(text).replace("\n", "  \n"))
    close_to(0)
    return "".join(parts)


# -- HTML: a stack of open containers, diffed line to line -------------------
#
# <ul>/<ol>/<blockquote> stay open across every line that shares their
# path element, however many sibling items pass through. <li> is
# tracked alongside them (see render_html's open_li) rather than being
# self-contained per line: a nested list started right after an item's
# marker line has to land *inside* that item's <li>, which means it
# can't have already been closed by the time the nested list opens.

def _diff(prev_path, new_path):
    """(pop_count, elements_to_push) turning a stack shaped like
    `prev_path` into one shaped like `new_path`."""
    common = 0
    while (common < len(prev_path) and common < len(new_path)
           and prev_path[common] == new_path[common]):
        common += 1
    return len(prev_path) - common, new_path[common:]


_HTML_HEADING_TAGS = {name: f"h{name[-1]}" for name in _HEADING_NAMES}


def _html_line_body(line, marker, dispatch_targets, anchor_descriptors):
    """The line's own rendered content -- with no <li> wrapper for a
    fresh list item. render_html manages that one itself, opening it
    before this line's content and closing it only once it's sure
    nothing else (a nested list, most commonly) still belongs inside
    it -- self-closing it here instead would leave a following nested
    <ul> as the <li>'s *sibling* rather than its child, which most
    renderers still show indented but isn't valid HTML."""
    runs = _content_runs(line, marker)
    if line.kind == "code-block":
        text = "".join(r[1] for r in runs if r[0] == "text")
        return f"<pre><code>{_html_escape(text)}</code></pre>"
    if len(runs) == 1 and runs[0][0] == "anchor":
        # hr and table are block-level on their own -- <table>/<hr>
        # inside a <p> is invalid HTML (browsers silently fix it up,
        # but there's no reason to hand them broken markup to begin
        # with). An image anchor falls through to the <p> case below
        # instead: images are inline, and one alone in its own
        # paragraph is exactly how CommonMark itself renders it.
        kind = anchor_descriptors.get(runs[0][1], {}).get("kind")
        if kind in ("hr", "table"):
            return _anchor_html(runs[0][1], anchor_descriptors)
    inline = _inline_html(runs, dispatch_targets, anchor_descriptors)
    tag = _HTML_HEADING_TAGS.get(line.kind)
    if tag:
        return f"<{tag}>{inline}</{tag}>"
    if marker is not None and line.path and line.path[-1][0] == "list":
        return inline
    return f"<p>{inline}</p>"


def _html_open_li(marker):
    if marker in ("task-checked", "task-unchecked"):
        checked = " checked" if marker == "task-checked" else ""
        return f'<li><input type="checkbox" disabled{checked}> '
    return "<li>"


def _container_path(line, open_type):
    """`line.path`, with every ("list", level) element carrying a third
    component: the list's type (see _list_type). Only the *deepest*
    such element can be this line's own marker's type -- an ancestor
    level's marker belongs to some earlier line, never this one -- so
    every other list position just inherits whatever type is currently
    open there, always comparing equal to it; only a genuine type
    *change* on a fresh item should ever look like a different list to
    render_html's stack diff."""
    marker = _marker_kind(line)
    deepest = len(line.path) - 1
    path = list(line.path)
    for index, element in enumerate(path):
        if element[0] != "list":
            continue
        level = element[1]
        this_type = _list_type(marker) if index == deepest and marker is not None else open_type.get(level)
        path[index] = ("list", level, this_type)
    return tuple(path)


def render_html(lines, dispatch_targets, anchor_descriptors):
    stack = []  # [(path_element, close_tag), ...] -- <ul>/<ol>/<blockquote>
    open_type = {}   # list level -> type of the list currently open there
    open_li = set()  # list levels with a currently-open, not-yet-closed <li>
    out = []
    prev_path = ()
    for line in lines:
        marker = _marker_kind(line)
        path = _container_path(line, open_type)
        is_item = marker is not None and path and path[-1][0] == "list"
        item_level = path[-1][1] if is_item else None

        pop_count, to_push = _diff(prev_path, path)
        for _ in range(pop_count):
            element, close_tag = stack.pop()
            if element[0] == "list" and element[1] in open_li:
                out.append("</li>")
                open_li.discard(element[1])
            out.append(close_tag)

        # A fresh item at a level whose <li> is still open (a sibling of
        # the previous one, not something a nested list just opened
        # under) closes that <li> first.
        if is_item and item_level in open_li:
            out.append("</li>")
            open_li.discard(item_level)

        for element in to_push:
            if element[0] == "bq":
                out.append("<blockquote>")
                stack.append((element, "</blockquote>"))
            else:
                _kind, level, list_type = element
                open_type[level] = list_type
                ordered = list_type == "ordered"
                out.append("<ol>" if ordered else "<ul>")
                stack.append((element, "</ol>" if ordered else "</ul>"))

        if is_item:
            out.append(_html_open_li(marker))
            open_li.add(item_level)
        out.append(_html_line_body(line, marker, dispatch_targets, anchor_descriptors))
        prev_path = path

    for element, close_tag in reversed(stack):
        if element[0] == "list" and element[1] in open_li:
            out.append("</li>")
            open_li.discard(element[1])
        out.append(close_tag)
    # Flat, not pretty-printed -- this HTML is a paste payload, not
    # source for a human to read, and a newline dropped between an
    # open tag and its own content (the task-checkbox prefix and the
    # item text after it, say) would otherwise render as a stray space.
    return "".join(out)


# -- Markdown: a per-line prefix, no open/close state needed -----------------

_MARKER_TOKEN = {
    "bullet": "- ",
    "task-checked": "- [x] ",
    "task-unchecked": "- [ ] ",
}


def _md_line_prefix(path, marker, ordinal_text):
    prefix = "> " * sum(1 for e in path if e[0] == "bq")
    list_levels = [e for e in path if e[0] == "list"]
    if marker is None:
        return prefix + "    " * len(list_levels)
    ancestors = max(0, len(list_levels) - 1)
    prefix += "    " * ancestors
    token = _MARKER_TOKEN.get(marker, f"{ordinal_text}. " if marker == "ordered" else "- ")
    return prefix + token


def _md_line(line, dispatch_targets, anchor_descriptors):
    marker = _marker_kind(line)
    runs = _content_runs(line, marker)
    prefix = _md_line_prefix(line.path, marker, _ordinal_text(line) if marker == "ordered" else "")
    if line.kind == "code-block":
        text = "".join(r[1] for r in runs if r[0] == "text")
        fence = _md_code_fence(text)
        body_lines = [f"{prefix}{fence}"] + [f"{prefix}{ln}" for ln in text.split("\n")] + [f"{prefix}{fence}"]
        return "\n".join(body_lines)
    inline = _inline_markdown(runs, dispatch_targets, anchor_descriptors)
    heading_level = line.kind[-1] if line.kind in _HEADING_NAMES else None
    if heading_level:
        return f"{prefix}{'#' * int(heading_level)} {inline}"
    return f"{prefix}{inline}"


def render_markdown(lines, dispatch_targets, anchor_descriptors):
    out = []
    prev = None
    prev_marker = None
    for line in lines:
        marker = _marker_kind(line)
        if out:
            # A single blank line between blocks, same as everywhere
            # else -- except two sibling items of the same list, which
            # need no blank line between them at all (and reproducing
            # one would turn a tight list loose in most renderers). Same
            # type as well as same path: two back-to-back *separate*
            # lists at the same level share a path too (see
            # _list_type's docstring), and gluing them together with no
            # blank line would misread as one list even though the
            # marker change alone is enough for a parser to split them
            # back apart correctly.
            same_list_siblings = (
                prev is not None and prev.path and prev.path == line.path
                and marker is not None and _list_type(marker) == _list_type(prev_marker)
            )
            out.append("\n" if same_list_siblings else "\n\n")
        out.append(_md_line(line, dispatch_targets, anchor_descriptors))
        prev, prev_marker = line, marker
    return "".join(out)


# -- public API ---------------------------------------------------------------

def selection_to_html_and_markdown(buffer, dispatch_targets, anchor_descriptors, start, end):
    """(html, markdown) for the buffer range [start, end)."""
    lines = walk_lines(buffer, start, end)
    html = render_html(lines, dispatch_targets, anchor_descriptors)
    markdown = render_markdown(lines, dispatch_targets, anchor_descriptors)
    return html, markdown


def selection_image_texture(buffer, anchor_descriptors, start, end):
    """The Gdk.Texture backing the *one* image anchor in [start, end),
    or None if the selection has none, more than one, or its one image
    hasn't got pixels to give (a remote image the reader hasn't opted
    to load, or one that failed) -- reference-based markup
    (_anchor_markdown/_anchor_html) is the fallback for all of those,
    same as it always has been. A multi-image selection could in
    principle offer several, but most clipboard consumers only look at
    a single image/* flavour, so there's no well-defined "the" image to
    hand them in that case.

    A plain forward walk rather than routing through walk_lines: this
    only needs to notice child anchors, not reconstruct paragraph/list/
    heading structure around them.
    """
    it = start.copy()
    texture = None
    seen = 0
    while it.compare(end) < 0:
        anchor = it.get_child_anchor()
        if anchor is not None:
            info = anchor_descriptors.get(anchor)
            if info is not None and info.get("kind") == "image":
                seen += 1
                texture = info["view"].texture
        if not it.forward_char():
            break
    return texture if seen == 1 else None


def make_content_provider(plain_text, html, markdown, image_texture=None):
    """A Gdk.ContentProvider offering all flavours at once -- receiving
    apps pick whichever they understand, plain text as the universal
    fallback. new_for_value(a string GValue) is used for plain text
    rather than hand-building text/plain bytes: GDK already knows how
    to serialize a string GValue to every plain-text mimetype a
    receiver might ask for (text/plain;charset=utf-8, UTF8_STRING,
    STRING, ...), which is exactly what Gdk.Clipboard.set_text() relies
    on internally.

    `image_texture`, if given, adds real image/png bytes alongside the
    reference-based markup already in `html`/`markdown` (an `<img>`
    tag / a `![alt](src)`) -- see selection_image_texture for when
    that's possible at all. Always PNG regardless of the image's own
    original format: a Gdk.Texture only ever holds decoded pixels, not
    the source bytes, so there's no vector data left to hand an SVG
    source back out even if a receiver would have preferred it.
    """
    providers = [
        Gdk.ContentProvider.new_for_value(_string_value(plain_text)),
        Gdk.ContentProvider.new_for_bytes("text/html", GLib.Bytes.new(html.encode("utf-8"))),
        Gdk.ContentProvider.new_for_bytes("text/markdown", GLib.Bytes.new(markdown.encode("utf-8"))),
    ]
    if image_texture is not None:
        providers.append(Gdk.ContentProvider.new_for_bytes("image/png", image_texture.save_to_png_bytes()))
    return Gdk.ContentProvider.new_union(providers)


def _string_value(text):
    value = GObject.Value()
    value.init(str)
    value.set_string(text)
    return value

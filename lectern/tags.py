"""Declarative Gtk.TextTag definitions shared by the on-screen renderer and
the print pipeline (see printing.py's use of tag_style_props), so the two
can never visually drift apart.
"""
import functools

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Pango, Gdk

# Colors that must adapt to the system light/dark preference. Keep this
# small and flat rather than trying to pull live theme colors out of GTK's
# CSS machinery -- a fixed, hand-picked pair of palettes is simpler and good
# enough for a text viewer.
_LIGHT = {
    "dim": "#5e5c64",
    "link": "#1a5fb4",
    "code-bg": "#f6f5f4",
    "heading-rule": "#d8dee4",
    "pyg-keyword": "#a51e75",
    "pyg-string": "#26a269",
    "pyg-comment": "#77767b",
    "pyg-number": "#c64600",
    "pyg-builtin": "#12488b",
    "pyg-function": "#1a5fb4",
    "pyg-class": "#9a5b00",
    "pyg-operator": "#5e5c64",
    "pyg-decorator": "#813d9c",
    # Mermaid diagrams (mermaid/draw.py). Cooler and flatter than
    # mermaid's own lilac default so a diagram reads as part of an
    # Adwaita document rather than an embedded screenshot of a web page.
    "diagram-node": "#e8f0fb",
    "diagram-node-alt": "#d7e3f4",
    "diagram-cluster": "#f2f3f5",
    "diagram-edge": "#5c7799",
    "diagram-fg": "#1b1f24",
    "diagram-label-bg": "#fafafa",
    "diagram-note": "#fbf5d6",
}

_DARK = {
    "dim": "#9a9996",
    "link": "#62a0ea",
    "code-bg": "#2d2d2d",
    "heading-rule": "#3d444d",
    "pyg-keyword": "#dc8add",
    "pyg-string": "#8ff0a4",
    "pyg-comment": "#9a9996",
    "pyg-number": "#ffa348",
    "pyg-builtin": "#99c1f1",
    "pyg-function": "#62a0ea",
    "pyg-class": "#f8e45c",
    "pyg-operator": "#c0bfbc",
    "pyg-decorator": "#dc8add",
    "diagram-node": "#2b3746",
    "diagram-node-alt": "#35455a",
    "diagram-cluster": "#282b30",
    "diagram-edge": "#8cb2dc",
    "diagram-fg": "#f6f5f4",
    "diagram-label-bg": "#242424",
    "diagram-note": "#3b3722",
}

# Heading scales, largest first (heading1 == h1), aligned to GitHub's
# typography ratios.
_HEADING_SCALE = [2.0, 1.5, 1.25, 1.0, 0.875, 0.85]

# Every length below is in nominal 96dpi pixels against this base font
# size -- one em -- and so is only correct as written at 100% zoom.
# apply_metrics() scales them; printing.py is pinned to 100% and spends
# them as written.
BASE_FONT_PX = 16.0

# Widest the body text column is allowed to get, in em: past this a wider
# window buys wider gutters rather than a longer line. GitHub's own cap
# (1012px against a 16px base).
MAX_MEASURE_EM = 1012 / 16

# Body line-height, in em -- a ratio rather than a pixel count, because
# that is what a line-height is. A Gtk.TextView has none: its lines are as
# tall as the font makes them, where CSS pads every line box out to this.
# The difference (line_leading below) is space the browser puts between
# *every* pair of lines, block boundaries included, so without it the whole
# document renders tighter than GitHub -- and a nested list, whose only
# separation from its parent item is the line-height, gets no gap at all.
LINE_HEIGHT = 1.5

# Space above every list item after a list's first: GitHub's li + li. Only
# correct alongside the leading above.
LIST_ITEM_GAP = 4

# The Gtk.TextView's own margin on all four sides (decorated_textview.py
# applies it), i.e. where the content column starts when the window is
# narrow enough not to be capped at MAX_MEASURE_EM. Lives here because
# Gtk.TextTag's left-margin/right-margin *replace* the view's rather than
# adding to it, so any tag wanting an inset relative to the content column
# has to spell out the sum itself -- see "code-block" below -- and, for the
# same reason, has to be moved along by hand when the column is centred
# (the `gutter` argument to apply_metrics).
CONTENT_MARGIN = 16

# Chrome that Gtk.TextTag can't express and decorated_textview.py paints
# by hand (see that module's docstring). Kept here next to the tag
# definitions they have to stay in step with: CODE_BLOCK_PADDING is also
# what renderer.py's fence block margins reserve room for, and
# HEADING_RULE_PAD is what heading1/heading2's pixels-below-lines below
# reserves. GitHub's values, in px at a 16px base.
CODE_BLOCK_PADDING = 16
CODE_BLOCK_RADIUS = 6
INLINE_CODE_PAD_X = 4
INLINE_CODE_PAD_Y = 2
INLINE_CODE_RADIUS = 4
HEADING_RULE_PAD = 6
HEADING_RULE_WIDTH = 1
# Space between a fence's code panel and the caption saying why a mermaid
# fence was shown as source instead of drawn (renderer._emit_code_block),
# on top of the CODE_BLOCK_PADDING the panel itself reaches down into.
FENCE_CAPTION_GAP = 6

# Indent step, in pixels, per nesting level of list content (~2em).
LIST_INDENT_STEP = 30
LIST_HANGING_INDENT = -16
BLOCKQUOTE_INDENT = 24

# Pygments token-type tag names this module knows how to color. Kept in one
# place with highlighting.py's TOKEN_TAG_NAMES so the two can't disagree.
PYGMENTS_TAG_NAMES = [
    "pyg-keyword", "pyg-string", "pyg-comment", "pyg-number",
    "pyg-builtin", "pyg-function", "pyg-class", "pyg-operator",
    "pyg-decorator",
]


def _rgba(spec):
    rgba = Gdk.RGBA()
    rgba.parse(spec)
    return rgba


def line_leading(font_px, natural_line_px):
    """How much space a line needs *added* to reach LINE_HEIGHT.

    Never a constant: the natural line box is font-dependent (19px for
    16px Adwaita Sans, different again in DejaVu Sans), so callers measure
    live metrics -- decorated_textview.py's Pango context on screen,
    printing.py's own font on paper. Stating it as "pad up to 1.5em" also
    keeps it right at every zoom level. Clamped at zero so a font with a
    tall line box keeps its own metrics rather than overlapping.
    """
    return max(0, round(LINE_HEIGHT * font_px) - round(natural_line_px))


def list_marker_column(level):
    """Where a list item's bullet/number sits at `level`, as a left-margin.
    Absolute rather than a per-level increment, because `left-margin`
    doesn't accumulate -- see ensure_list_indent_tag."""
    return LIST_INDENT_STEP * (level + 1)


def list_text_column(level):
    """Where the item's *text* sits: one hanging indent further in than its
    marker column, where wrapped lines and continuation blocks land."""
    return list_marker_column(level) - LIST_HANGING_INDENT


def link_color_hex(dark):
    """The raw hex string backing the "link" tag's color, for callers that
    need to build Pango markup directly (tables.py's clickable cell
    links) rather than going through the shared Gtk.TextTag table. Table
    cells are separate Gtk.Label widgets outside that table, so unlike
    buffer text they won't repaint automatically via update_tag_colors()
    if the desktop theme flips light/dark mid-session -- accepted as a
    minor known limitation rather than building live-recolor plumbing
    for it."""
    return (_DARK if dark else _LIGHT)["link"]


def heading_rule_rgba(dark):
    return _rgba((_DARK if dark else _LIGHT)["heading-rule"])


# Scene role (mermaid/scene.py) -> palette key. The roles are named for
# what they mean in a diagram; this is the one place that says what colour
# each of them takes, for the same reason the tag table above is: the
# on-screen widget and the print job both read it, so a diagram can't be
# one colour on screen and another on paper.
_DIAGRAM_ROLE_COLORS = {
    "node": "diagram-node",
    "node-alt": "diagram-node-alt",
    "cluster": "diagram-cluster",
    "edge": "diagram-edge",
    "fg": "diagram-fg",
    "dim": "dim",
    "label-bg": "diagram-label-bg",
    "note": "diagram-note",
}


def diagram_palette(dark):
    """Role -> (r, g, b) floats, ready for cairo."""
    palette = _DARK if dark else _LIGHT
    colors = {}
    for role, key in _DIAGRAM_ROLE_COLORS.items():
        rgba = _rgba(palette[key])
        colors[role] = (rgba.red, rgba.green, rgba.blue)
    return colors


@functools.lru_cache(maxsize=2)
def tag_style_props(dark):
    """The single source of truth for tag -> GObject-property-value pairs,
    shared by create_tag_table/update_tag_colors below *and* by
    printing.py (which reuses it to build matching Pango run attributes,
    so on-screen and printed styling can't drift apart). Cached: there are
    only ever two possible results (light/dark), and this gets called on
    every reload, print job and theme change.

    Mixes run-level properties (weight, style, scale, family, underline,
    rise, foreground-rgba -- what printing.py's _PROP_TO_ATTR_CTOR
    understands) with block-level ones (left-margin, pixels-above-lines,
    paragraph-background-rgba, wrap-mode -- meaningful only to
    Gtk.TextTag/TextView, which printing.py handles separately via its own
    page-layout math). Callers on the print side are expected to pick out
    only what applies to them.
    """
    palette = _DARK if dark else _LIGHT
    props = {
        # Always present in every top-level RenderCtx (see renderer.py),
        # so it's the one tag guaranteed to cover every line of every
        # block -- including inside blockquotes/list items, since
        # push_block() only ever appends to block_tags, never replaces
        # them. That makes it where the line-height leading hangs;
        # apply_metrics fills it in from live font metrics.
        #
        # Below-lines rather than above: a block gap is a
        # pixels-above-lines tag on the same line, and two tags setting
        # the *same* property don't add -- GTK takes the highest-priority
        # one, so leading spelled above-lines would be silently replaced
        # by every block gap in the document. Below plus above are
        # different properties, and those do add (verified).
        "prose": {},
        "em": {"style": Pango.Style.ITALIC},
        "strong": {"weight": Pango.Weight.BOLD},
        "strike": {"strikethrough": True},
        "fence-caption": {"foreground-rgba": _rgba(palette["dim"]), "scale": 0.85},
        "code-inline": {
            "family": "monospace",
            "scale": 0.92,
            "background-rgba": _rgba(palette["code-bg"]),
        },
        # Inset one CODE_BLOCK_PADDING *inside* the content column, so the
        # panel decorated_textview.py paints behind it can span the column
        # edge-to-edge with even padding on all four sides. The vertical
        # half of that padding is reserved by renderer.py's fence block
        # margins instead -- pixels-above/below-lines here would apply to
        # every line of the block, not just its first and last.
        "code-block": {
            "family": "monospace",
            "scale": 0.92,
            "left-margin": CONTENT_MARGIN + CODE_BLOCK_PADDING,
            "right-margin": CONTENT_MARGIN + CODE_BLOCK_PADDING,
            "wrap-mode": Gtk.WrapMode.NONE,
        },
        # Renderer-created dynamic block-gap-* tags carry the actual
        # inter-block top spacing value.
        "list-item-gap": {"pixels-above-lines": LIST_ITEM_GAP},
        # Pulls the marker's own line back into the margin the item's
        # text block starts at, so the bullet/number hangs to its left
        # and wrapped lines align under the text. Applied by renderer.py
        # to that one line only -- on the list-indent tag it would also
        # hang the first line of a *continuation* paragraph, which has no
        # marker to put there.
        "list-hang": {"indent": LIST_HANGING_INDENT},
        "blockquote": {
            "left-margin": BLOCKQUOTE_INDENT,
            "foreground-rgba": _rgba(palette["dim"]),
            "style": Pango.Style.ITALIC,
        },
        # "tnum" (OpenType tabular numerals) makes every digit render at
        # the same fixed width -- without it, digit "1" commonly renders
        # visibly narrower than "2"-"9" in proportional UI fonts, so two
        # same-length ordered-list markers (e.g. "1." and "2.") can end up
        # different pixel widths, offsetting where their item text starts
        # even though nothing else about them differs. (A generalized
        # Pango-tab-stop-based fix for the further case of *different*
        # digit counts, e.g. "9." vs "10.", was tried and reverted: it
        # broke wrapped continuation-line alignment for multi-line items,
        # a worse regression than the one it fixed.)
        "list-marker": {"foreground-rgba": _rgba(palette["dim"]), "font-features": "tnum 1"},
        "link": {
            "foreground-rgba": _rgba(palette["link"]),
            "underline": Pango.Underline.SINGLE,
        },
        "footnote-ref": {
            "rise": 6 * Pango.SCALE,
            "scale": 0.75,
            "foreground-rgba": _rgba(palette["link"]),
        },
        "task-checked-glyph": {"foreground-rgba": _rgba(palette["dim"])},
        "task-unchecked-glyph": {"foreground-rgba": _rgba(palette["dim"])},
        # Shared by tables.py (on-screen Gtk.Label markup) and printing.py
        # (Pango run attribute) so "table headers are bold" is one fact,
        # not two separately-hardcoded ones.
        "table-header": {"weight": Pango.Weight.BOLD},
    }
    for i in range(6):
        props[f"heading{i + 1}"] = {
            "weight": Pango.Weight.BOLD,
            "scale": _HEADING_SCALE[i],
        }
    # h1/h2 carry a bottom rule that decorated_textview.py draws
    # HEADING_RULE_PAD below the text; reserve that much plus the stroke
    # so the rule sits in space of its own rather than on the next block.
    for name in ("heading1", "heading2"):
        props[name]["pixels-below-lines"] = HEADING_RULE_PAD + HEADING_RULE_WIDTH
    for name in PYGMENTS_TAG_NAMES:
        props[name] = {"foreground-rgba": _rgba(palette[name])}
    return props


# How each kind of property in the table above is maintained once a tag
# exists. Colors are re-applied when the desktop theme flips
# (update_tag_colors); lengths when the zoom factor or the content column
# moves (apply_metrics). Everything else -- weight, style, wrap-mode, and
# `scale` -- is set once at construction and never touched again. `scale`
# in particular is deliberately not a length: it's a multiplier GTK
# already composes on top of whatever font size zoom.py's CSS sets.
_COLOR_PROPS = frozenset({"foreground-rgba", "background-rgba"})
# Lengths that are pure sizes: zoom is all that moves them.
_LENGTH_PROPS = frozenset({
    "pixels-inside-wrap", "pixels-above-lines", "pixels-below-lines",
    "indent", "rise",
})
# Lengths measured from the edge of the text window rather than from the
# content column. A Gtk.TextTag left/right-margin *replaces* the view's own
# (see CONTENT_MARGIN), so a tag spelling out an indent in one of these has
# to carry the current gutter as well as the zoom -- otherwise centring the
# text column moves prose and leaves every indented block behind.
_EDGE_PROPS = frozenset({"left-margin", "right-margin"})


def _metric(prop, value, scale, gutter):
    return round(value * scale) + (gutter if prop in _EDGE_PROPS else 0)


def _apply_dynamic_metrics(tag, data):
    """Rescale one of the renderer's lazily-created tags, recognised by name.

    They aren't in tag_style_props: they're minted mid-render, one per
    distinct gap or nesting level a document uses. The name carries the
    base value, so it doubles as the record of what to rescale to.
    """
    scale, gutter = data
    kind, _, suffix = (tag.get_property("name") or "").rpartition("-")
    if not suffix.isdigit():
        return
    n = int(suffix)  # a gap in base pixels, or a nesting level
    if kind == "block-gap":
        tag.set_property("pixels-above-lines", round(n * scale))
    elif kind in ("list-indent", "list-body"):
        column = (list_marker_column if kind == "list-indent" else list_text_column)(n)
        tag.set_property("left-margin", round(column * scale) + gutter)


def apply_metrics(tag_table, scale=1.0, gutter=0, leading=0):
    """Re-scale every length in `tag_table` to zoom factor `scale`, push the
    margin-valued ones out by `gutter` pixels, and give every line
    `leading` pixels of line-height padding.

    This is what makes zooming proportional: zoom.py only changes the CSS
    font-size, which the `scale`-valued properties ride for free, but
    every absolute length here is written against BASE_FONT_PX and has to
    be scaled by hand. `leading` is the exception that arrives already in
    device pixels -- it is measured from live font metrics (line_leading),
    not derived from a base value.

    Only lengths are touched, so this and update_tag_colors compose in
    either order. Which properties are lengths doesn't depend on the
    palette, so the light table is read purely for its shape.
    """
    for name, props in tag_style_props(dark=False).items():
        tag = tag_table.lookup(name)
        if tag is None:
            continue
        for prop, value in props.items():
            if prop in _LENGTH_PROPS or prop in _EDGE_PROPS:
                tag.set_property(prop, _metric(prop, value, scale, gutter))
    prose = tag_table.lookup("prose")
    if prose is not None:
        prose.set_property("pixels-below-lines", leading)
        prose.set_property("pixels-inside-wrap", leading)
    tag_table.foreach(_apply_dynamic_metrics, (scale, gutter))


def _make_tag(name, props):
    tag = Gtk.TextTag(name=name)
    for prop, value in props.items():
        tag.set_property(prop, value)
    return tag


def create_tag_table(dark=False):
    """Build a fresh Gtk.TextTagTable with every static tag installed."""
    table = Gtk.TextTagTable()
    for name, props in tag_style_props(dark).items():
        table.add(_make_tag(name, props))
    return table


def update_tag_colors(tag_table, dark):
    """Re-apply theme-dependent colors in place (called on dark-mode change).

    Colors only: the lengths in the same table belong to apply_metrics, and
    re-applying those from the base table here would quietly reset a zoomed,
    centred document to 100% spacing at the window edge the moment the
    desktop flipped light/dark.
    """
    for name, props in tag_style_props(dark).items():
        tag = tag_table.lookup(name)
        if tag is None:
            continue
        for prop, value in props.items():
            if prop in _COLOR_PROPS:
                tag.set_property(prop, value)


def get_or_create_tag(tag_table, name, props=None):
    """Look up `name` in `tag_table`, creating it (with `props` applied, if
    given) if it doesn't exist yet. The shared lookup-or-create primitive
    behind ensure_list_indent_tag/ensure_instance_tag below, and reused
    as-is by findbar.py for its match-highlight tags -- one "create this
    tag if missing" idiom, not three."""
    tag = tag_table.lookup(name)
    if tag is None:
        tag = Gtk.TextTag(name=name)
        for prop, value in (props or {}).items():
            tag.set_property(prop, value)
        tag_table.add(tag)
    return tag


def ensure_block_gap_tag(tag_table, pixels):
    """Lazily create and cache a block-gap tag with `pixels-above-lines`.

    `pixels` is a base, 100%-zoom value, as everything the renderer works
    in is: a tag minted mid-render carries it as written, and apply_metrics
    scales it to the live zoom afterwards.
    """
    pixels = max(0, int(pixels))
    return get_or_create_tag(
        tag_table,
        f"block-gap-{pixels}",
        {"pixels-above-lines": pixels},
    )


def list_indent_tag_name(level):
    return f"list-indent-{level}"


def list_body_tag_name(level):
    return f"list-body-{level}"


def ensure_list_indent_tag(tag_table, level):
    """Lazily create (and cache in the table) a list level's two margins.

    `list-indent-N` is the *marker* column, which the item's opening line
    starts at; `list-body-N` is the *text* column one hanging indent
    further in, where the item's wrapped lines land and where its
    continuation blocks belong.

    Both margins are absolute rather than per-level increments, because
    `left-margin` doesn't accumulate: a nested item carries every
    ancestor's tag too, and GTK takes the value from the
    highest-priority tag that sets it. Priority is insertion order, so
    the pair is created together, body after indent -- which makes body
    beat its own level's marker column, and any deeper level (reachable
    only through this one) beat both.
    """
    name = list_indent_tag_name(level)
    if tag_table.lookup(name) is None:
        get_or_create_tag(tag_table, name, {"left-margin": list_marker_column(level)})
        get_or_create_tag(tag_table, list_body_tag_name(level),
                          {"left-margin": list_text_column(level)})
    return tag_table.lookup(name)


def ensure_list_body_tag(tag_table, level):
    """The text-column tag for `level` (see ensure_list_indent_tag)."""
    ensure_list_indent_tag(tag_table, level)
    return tag_table.lookup(list_body_tag_name(level))


def ensure_instance_tag(tag_table, name):
    """Create a bare, style-less tag used only as a per-instance dispatch
    key (links, footnote refs) if it doesn't already exist."""
    return get_or_create_tag(tag_table, name)

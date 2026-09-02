"""Print pipeline, driven entirely off MarkdownRenderer.print_model -- never
by re-walking the live TextBuffer. That's deliberate: Gtk.TextChildAnchor
-embedded tables leave a single object-replacement character in the
buffer's text stream, so reconstructing formatted text from the buffer
after the fact would silently lose every table cell.

Pango.AttrList offsets are UTF-8 *byte* offsets, not character offsets --
this bit us once already while building this file, so run-boundary byte
lengths are computed explicitly throughout rather than assumed to match
`len(text)`.
"""
import math
from pathlib import PurePath

import cairo
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GLib, Gtk, Gdk, Pango, PangoCairo

from . import tags as tagdefs
from . import tables as tabledefs
from . import zoom as zoomdefs
from .i18n import _

# Screen pixels are nominally 96dpi; print units are 72dpi points. Every
# length tags.py and renderer.py express is in those pixels, so it has to
# come through here to land on paper at the proportions it has on screen
# -- a "16" spent directly as points is a third too big.
PX_TO_PT = 72.0 / 96.0


def pt(px):
    return px * PX_TO_PT


HR_HEIGHT_PT = 1.0
HR_BLOCK_HEIGHT_PT = 16.0
TABLE_CELL_PAD_PT = 6.0
TABLE_ROW_GAP_PT = 6.0
TABLE_RULE_RGB = (0.7, 0.7, 0.7)
CODE_BLOCK_PAD_PT = pt(tagdefs.CODE_BLOCK_PADDING)
CODE_BLOCK_RADIUS_PT = pt(tagdefs.CODE_BLOCK_RADIUS)
HEADING_RULE_WIDTH_PT = pt(tagdefs.HEADING_RULE_WIDTH)
HEADING_RULE_PAD_PT = pt(tagdefs.HEADING_RULE_PAD)
# Cap on how much of one page a single image may claim, so a very tall
# image leaves room for something else rather than owning a page outright.
IMAGE_MAX_PAGE_FRACTION = 0.9

# GTK's own default page setup already gives the bottom a generous ~40pt
# (0.56in) -- fine as is -- but only ~18pt (0.25in) on the other three
# sides, which reads as "printed to the edge of the sheet". Bring those
# three up to a plainer 28pt (~10mm); bottom is deliberately left
# untouched. Everything here is in points, like the rest of this module
# (see PX_TO_PT above) -- Gtk.PageSetup takes points just as natively as
# mm or inches, so there's no reason for page-setup margins to be the one
# thing in this file measured in a different unit.
PAGE_MARGIN_PT = 28.0
# A further 28pt (~10mm) on top of that, left only -- right gets its own
# smaller, 14pt (~5mm) bump instead. Kept as separate constants rather
# than folded into PAGE_MARGIN_PT so each side's extra stays visible at
# its own call site.
LEFT_EXTRA_MARGIN_PT = 28.0
RIGHT_EXTRA_MARGIN_PT = 14.0
# With the footer on, its line otherwise sits flush against the last line
# of body content -- there's nowhere else to put its clearance, since
# FOOTER_TEXT_TOP_PT is the entire gap between them regardless of how big
# FOOTER_BAND_PT is. Taken out of the bottom margin instead of added on
# top, so the footer doesn't just end up with more empty paper below it
# too. Only applies when there's a footer to make room for.
FOOTER_MARGIN_SHIFT_PT = 14.0

# One notch down the screen zoom ladder (zoom.py's STEPS has 0.9 right
# below 1.0): 12pt reads comfortably on a backlit screen but heavy on
# paper. Print gets its own constant rather than reusing zoomdefs.BASE_PT
# so a screen-zoom change can never silently resize the printout.
PRINT_BASE_PT = zoomdefs.BASE_PT * 0.9


def _print_page_setup(header_footer=False):
    """A Gtk.PageSetup with 56pt left, 42pt right, 28pt top, and GTK's own
    default bottom margin -- minus FOOTER_MARGIN_SHIFT_PT when there's a
    footer to make room for, untouched otherwise. Used as the operation's
    default so it still shows up, editable, in the print dialog's "Page
    Setup" tab -- and so `Gtk.PrintContext.get_width/height` already
    return the reduced text column by the time `_on_begin_print` sees
    them."""
    setup = Gtk.PageSetup()
    setup.set_top_margin(PAGE_MARGIN_PT, Gtk.Unit.POINTS)
    setup.set_left_margin(PAGE_MARGIN_PT + LEFT_EXTRA_MARGIN_PT, Gtk.Unit.POINTS)
    setup.set_right_margin(PAGE_MARGIN_PT + RIGHT_EXTRA_MARGIN_PT, Gtk.Unit.POINTS)
    if header_footer:
        default_bottom = Gtk.PageSetup().get_bottom_margin(Gtk.Unit.POINTS)
        setup.set_bottom_margin(default_bottom - FOOTER_MARGIN_SHIFT_PT, Gtk.Unit.POINTS)
    return setup


# Only run-level (character-range) properties translate to Pango
# attributes; margins/backgrounds/pixel-spacing are block-level concerns
# handled by this module's own layout math instead.
_PROP_TO_ATTR_CTOR = {
    "weight": Pango.attr_weight_new,
    "style": Pango.attr_style_new,
    "strikethrough": Pango.attr_strikethrough_new,
    "family": Pango.attr_family_new,
    "underline": Pango.attr_underline_new,
    "rise": Pango.attr_rise_new,
}


# Print-only substitutes for a UI font that can't carry its own weights
# onto paper (see _base_font), nearest-first by resemblance to the GNOME
# UI font. Hand-picked because each ships its weights as *separate face
# files*, which is the property that matters and the one thing Pango
# cannot report: Pango.FontFamily.is_variable() is true for Cantarell too
# (it ships a variable face alongside its static ones) even though it
# prints all weights correctly, so the list carries this knowledge rather
# than a predicate.
_STATIC_FONT_FALLBACKS = ("Cantarell", "Liberation Sans", "DejaVu Sans")


def _base_font():
    """The font to print in: GTK's UI font family, at zoom.py's 100% size.

    A Gtk.PrintContext layout otherwise inherits whatever the print
    system picked -- typically a serif, at its own size -- so a document
    came out in a different typeface from the one it was read in, and
    every length scaled from a screen pixel (indents, the hanging indent,
    line spacing) was measured against the wrong body size. Print is
    deliberately pinned to 100%: the reader's zoom level is a reading
    aid, not a property of the document.

    The exception is a *variable* UI font, which the current GNOME
    default (Adwaita Sans) is: one file, with every weight as a named
    instance. Cairo's PDF font subsetting keeps only one weight pairing
    per face, so mixing regular and bold silently prints every heading
    above the first size in regular -- reproducible in a dozen lines of
    plain Pango/cairo, with no fix available from this side (font
    variations, a "... Bold" family name and Weight.HEAVY were all
    tried). Such a font is swapped for a static-faced one on paper only:
    a near-miss in letterforms costs less than losing the heading
    hierarchy, and sizes/metrics still come from the screen either way.
    """
    settings = Gtk.Settings.get_default()
    name = settings.get_property("gtk-font-name") if settings is not None else None
    desc = Pango.FontDescription.new() if not name else Pango.FontDescription.from_string(name)
    families = {f.get_name(): f for f in PangoCairo.FontMap.get_default().list_families()}
    ui_family = families.get(desc.get_family() or "")
    if ui_family is None or ui_family.is_variable():
        substitute = next((f for f in _STATIC_FONT_FALLBACKS if f in families), None)
        if substitute is not None:
            desc.set_family(substitute)
    desc.set_size(int(PRINT_BASE_PT * Pango.SCALE))
    return desc


# Tag properties that describe a *font* rather than decorate a run. They
# go into one complete Pango.FontDescription per run instead of separate
# attributes: layered on a print-context layout as deltas, a bold run at
# any size other than the base one resolved back to the regular face,
# which silently un-bolded every heading above h4.
_DESC_PROPS = frozenset({"weight", "style", "family", "scale"})


def _run_font(base, run_props):
    """One font description for a run, from the tags applied to it."""
    desc = base.copy()
    scale = 1.0
    for props in run_props:
        for prop, value in props.items():
            if prop == "scale":
                # Gtk.TextTag scales *compose* where several tags apply
                # (inline code inside a heading takes both), so they
                # multiply out into the single size below.
                scale *= value
            elif prop == "weight":
                desc.set_weight(value)
            elif prop == "style":
                desc.set_style(value)
            elif prop == "family":
                desc.set_family(value)
    desc.set_size(int(round(PRINT_BASE_PT * scale * Pango.SCALE)))
    return desc


def _foreground_attr(rgba):
    return Pango.attr_foreground_new(
        int(rgba.red * 65535), int(rgba.green * 65535), int(rgba.blue * 65535)
    )


def _rgba_rgb(rgba):
    return rgba.red, rgba.green, rgba.blue


def _rounded_rect(cr, x, y, width, height, radius):
    """Cairo path for the code panel. Deliberately a second, tiny copy of
    decorated_textview.py's: importing that module would pull Adw and the
    whole on-screen view into the print path for eight lines of geometry."""
    radius = max(0.0, min(radius, width / 2.0, height / 2.0))
    x2, y2 = x + width, y + height
    cr.new_sub_path()
    cr.arc(x2 - radius, y + radius, radius, -math.pi / 2, 0.0)
    cr.arc(x2 - radius, y2 - radius, radius, 0.0, math.pi / 2)
    cr.arc(x + radius, y2 - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _left_margin_pt(block_tags):
    """How far in from the text column these block tags put a block on
    screen, so print puts it in the same place.

    Two things this is not. It is not a *sum*: `left-margin` is a plain
    Gtk.TextTag property, so where several applied tags set it GTK takes
    the highest-priority one rather than adding them up -- and the
    list-indent tags are created innermost-last, which makes the innermost
    one win. Their margins are absolute already, so that one value is the
    whole indent, and a blockquote nested in a list adds nothing on top.

    And it is not the tag's raw value: a Gtk.TextTag left-margin
    *replaces* the view's own rather than adding to it, so on screen an
    indented block clears unindented prose by the difference between the
    two. Print's page margin is the equivalent of the view's.
    """
    columns = [
        # A "list-body-" tag is the same level's text column, one hanging
        # indent further in than its marker column.
        (tagdefs.list_text_column if name.startswith("list-body-")
         else tagdefs.list_marker_column)(int(name.rsplit("-", 1)[1]))
        for name in block_tags
        if name.startswith("list-indent-") or name.startswith("list-body-")
    ]
    if columns:
        margin = max(columns)
    elif "blockquote" in block_tags:
        margin = tagdefs.BLOCKQUOTE_INDENT
    else:
        return 0.0
    return pt(max(0, margin - tagdefs.CONTENT_MARGIN))


def _inline_code_spans(item):
    """UTF-8 byte ranges of the item's inline-code runs, merged where they
    touch, so print can paint the same padded rounded chip behind them
    that decorated_textview.py paints on screen. Pango can only give a run
    a flat, tight background, which is the very thing Gtk.TextTag could
    already do and the chip exists to improve on."""
    spans, offset = [], 0
    for text, tag_names in item.runs:
        nbytes = len(text.encode("utf-8"))
        if "code-inline" in tag_names:
            if spans and spans[-1][1] == offset:
                spans[-1] = (spans[-1][0], offset + nbytes)
            else:
                spans.append((offset, offset + nbytes))
        offset += nbytes
    return spans


def _link_spans(item, href_by_tag):
    """UTF-8 byte ranges of the item's hyperlink runs paired with their
    target URL, merged where one link's own runs touch (its text is often
    several runs -- a bold word inside link text is its own run). Pango has
    no notion of a hyperlink, so on paper a link is a surface-level PDF
    annotation placed by geometry over the blue-and-underlined text, in the
    same way _inline_code_spans drives the inline-code chip."""
    spans, offset = [], 0
    for text, tag_names in item.runs:
        nbytes = len(text.encode("utf-8"))
        href = next((href_by_tag[n] for n in tag_names if n in href_by_tag), None)
        if href is not None:
            if spans and spans[-1][1] == offset and spans[-1][2] == href:
                spans[-1] = (spans[-1][0], offset + nbytes, href)
            else:
                spans.append((offset, offset + nbytes, href))
        offset += nbytes
    return spans


def _link_uri_attr(href):
    """cairo's tag-attribute parser has no escape syntax, so a URI holding
    the single quote we wrap it in would truncate the attribute. Percent-
    encoding is URLs' own answer for that character."""
    return href.replace("'", "%27")


_MARKER_TAGS = frozenset({"list-marker", "task-checked-glyph", "task-unchecked-glyph"})


def _marker_hang_pt(item):
    """The hanging indent for a list item's opening line: it starts at the
    marker column, and every line after it at the text column one
    LIST_HANGING_INDENT further in. Zero for anything that isn't a
    marker line -- including the item's own continuation blocks, which
    are already placed on the text column by _left_margin_pt.

    Pango.Layout's indent means the same thing as Gtk.TextTag's (negative
    indents the lines *after* the first, which is how both spell "hang"),
    so screen and paper stay in step through one shared constant.
    """
    if not any(name.startswith("list-indent-") for name in item.block_tags):
        return 0.0
    if not item.runs or not _MARKER_TAGS.intersection(item.runs[0][1]):
        return 0.0
    return pt(-tagdefs.LIST_HANGING_INDENT)


def _line_geometry(layout):
    """[(Pango.LayoutLine, y_top_pt, height_pt, baseline_pt, x_off_pt), ...].

    x_off is the line's own horizontal offset inside the layout, which is
    where a hanging indent lives. Pages are drawn a line at a time
    (show_layout_line, so a block can be split across a page break), and
    that draws wherever the caller puts the pen -- every offset Pango
    computed for the line is lost unless it travels with it.
    """
    result = []
    it = layout.get_iter()
    while True:
        line = it.get_line_readonly()
        _ink, logical = it.get_line_extents()
        y_top = Pango.units_to_double(logical.y)
        height = Pango.units_to_double(logical.height)
        baseline = Pango.units_to_double(it.get_baseline())
        x_off = Pango.units_to_double(logical.x)
        result.append((line, y_top, height, baseline, x_off))
        if not it.next_line():
            break
    return result


def _leading_pt(context, font):
    """Line-height padding in points, from the *print* font's own metrics.

    Not the screen's: a variable UI font gets substituted on paper (see
    _base_font), so the two fonts differ and so does their leading."""
    metrics = context.create_pango_layout().get_context().get_metrics(font, None)
    natural = (metrics.get_ascent() + metrics.get_descent()) / Pango.SCALE
    return pt(tagdefs.line_leading(PRINT_BASE_PT * 96 / 72, natural / PX_TO_PT))


def _build_text_layout(context, style_table, item, width_pt, hanging_pt=0.0,
                       font=None, leading_pt=0.0):
    combined = "".join(text for text, _tags in item.runs) or " "
    layout = context.create_pango_layout()
    layout.set_font_description(font or _base_font())
    layout.set_width(Pango.units_from_double(max(width_pt, 1.0)))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    if hanging_pt:
        layout.set_indent(Pango.units_from_double(-hanging_pt))
    # Matches the "prose" tag's line-height leading on screen (tags.py) --
    # Pango.Layout has no per-run equivalent, so it's set once here at the
    # whole-layout level. Pango puts it *between* lines and not after the
    # last, which is why _build_blocks adds one more to each block's gap.
    layout.set_spacing(Pango.units_from_double(leading_pt))
    layout.set_text(combined, -1)
    attr_list = Pango.AttrList()
    byte_offset = 0
    base_font = font or _base_font()
    for text, tag_names in item.runs:
        nbytes = len(text.encode("utf-8"))
        run_props = [style_table[n] for n in tag_names if style_table.get(n)]
        for props in run_props:
            for prop, value in props.items():
                if prop in _DESC_PROPS:
                    continue  # folded into the run's font description below
                if prop == "foreground-rgba":
                    attr = _foreground_attr(value)
                else:
                    ctor = _PROP_TO_ATTR_CTOR.get(prop)
                    if ctor is None:
                        continue
                    attr = ctor(value)
                attr.start_index = byte_offset
                attr.end_index = byte_offset + nbytes
                attr_list.insert(attr)
        attr = Pango.attr_font_desc_new(_run_font(base_font, run_props))
        attr.start_index = byte_offset
        attr.end_index = byte_offset + nbytes
        attr_list.insert(attr)
        byte_offset += nbytes
    layout.set_attributes(attr_list)
    return layout


def _build_table_rows(context, style_table, rows, width_pt, font=None):
    if not rows:
        return [], [], []
    # Shared with tables.py's on-screen <b>...</b> header markup via
    # tags.py's "table-header" entry, so "headers are bold" is one fact.
    header_weight = style_table.get("table-header", {}).get("weight", Pango.Weight.BOLD)
    ncols = max(len(r) for r in rows)
    # Same median-character-count weighting tables.py uses for the
    # on-screen Gtk.Grid (via max-width-chars), so a column doesn't come
    # out "wide" on screen and "narrow" on paper -- one shared notion of
    # column proportions, applied via the two renderers' own units.
    weights = tabledefs.column_char_weights(rows)
    total_weight = sum(weights)
    col_widths = [width_pt * w / total_weight for w in weights]
    row_layouts, row_heights = [], []
    for row_index, row in enumerate(rows):
        cells, max_h = [], 0.0
        for col_index in range(ncols):
            text = row[col_index] if col_index < len(row) else ""
            col_width = col_widths[col_index]
            layout = context.create_pango_layout()
            layout.set_font_description(font or _base_font())
            layout.set_width(Pango.units_from_double(max(col_width - 2 * TABLE_CELL_PAD_PT, 1.0)))
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
            layout.set_text(text, -1)
            if row_index == 0:
                al = Pango.AttrList()
                attr = Pango.attr_weight_new(header_weight)
                attr.start_index = 0
                attr.end_index = len(text.encode("utf-8"))
                al.insert(attr)
                layout.set_attributes(al)
            _w, h = layout.get_pixel_size()
            max_h = max(max_h, h + 2 * TABLE_CELL_PAD_PT)
            cells.append(layout)
        row_layouts.append(cells)
        row_heights.append(max_h)
    return row_layouts, row_heights, col_widths


class _Block:
    """One drawable unit produced from a PrintItem, laid out against a
    specific content width and ready to be sliced across pages."""

    def __init__(self, kind, x=0.0):
        self.kind = kind
        self.x = x
        self.layout = None
        self.lines = None
        self.rows = None
        self.col_widths = None
        self.pixbuf = None
        self.scene = None
        self.scale = 1.0
        self.palette = None
        self.font = None
        self.draw_size = None
        self.height = 0.0
        # Space above this block, carried over from the renderer's own
        # collapsed-margin model (PrintItem.gap) so paper spaces blocks
        # exactly as the screen does.
        self.gap = 0.0
        # Chrome drawn outside the text box, mirroring what
        # decorated_textview.py paints on screen: `panel` is the
        # fenced-code background (a dict of geometry, or None), `rule` the
        # RGB of an h1/h2 bottom rule (or None).
        self.panel = None
        self.rule = None
        self.chips = ()
        self.chip_rgb = None
        # [(start_byte, end_byte, href), ...] over the block's own layout,
        # one per hyperlink run -- drawn as clickable PDF Link annotations
        # (see _draw_link_annotations), nothing on a raster printer.
        self.links = ()
        # 1-6 for a heading paragraph, None otherwise -- distinct from
        # `rule`, which is only set for h1/h2 (where the bottom rule is
        # drawn). _paginate needs every level, to keep a heading with the
        # section it introduces rather than stranding it at a page foot.
        self.heading_level = None

    @classmethod
    def text(cls, x, layout, *, panel=None, rule=None, chips=(), chip_rgb=None,
             heading_level=None, links=()):
        block = cls("layout", x)
        block.layout = layout
        block.lines = _line_geometry(layout)
        block.height = (block.lines[-1][1] + block.lines[-1][2]) if block.lines else 0.0
        block.panel = panel
        block.heading_level = heading_level
        block.rule = rule
        block.chips = chips
        block.chip_rgb = chip_rgb
        block.links = links
        return block

    @classmethod
    def hr(cls):
        block = cls("hr", 0.0)
        block.height = HR_BLOCK_HEIGHT_PT
        return block

    @classmethod
    def image(cls, x, pixbuf, width, height):
        block = cls("image", x)
        block.pixbuf = pixbuf
        block.draw_size = (width, height)
        block.height = height
        return block

    @classmethod
    def diagram(cls, x, scene, scale, palette, font):
        block = cls("diagram", x)
        block.scene = scene
        block.scale = scale
        block.palette = palette
        # The scene's text is re-laid-out at draw time, so the font it was
        # measured with has to survive until then -- a different one would
        # put every string somewhere other than the box built for it.
        block.font = font
        block.draw_size = (pt(scene.width) * scale, pt(scene.height) * scale)
        block.height = block.draw_size[1]
        return block

    @classmethod
    def table(cls, x, row_layouts, row_heights, col_widths):
        block = cls("table", x)
        block.rows = (row_layouts, row_heights)
        block.col_widths = col_widths
        block.height = sum(row_heights) + TABLE_ROW_GAP_PT * max(len(row_heights) - 1, 0)
        return block


class _AltTextItem:
    """Stands in for an image that has no pixels to print -- one that
    failed, or a remote one the reader never chose to load. Printing the
    alt text keeps the page honest about what was meant to be there
    instead of leaving an unexplained gap."""
    __slots__ = ("runs", "block_tags", "language", "kind")

    def __init__(self, alt, block_tags):
        self.kind = "paragraph"
        # Italic rather than a dim colour: there's no "dim" entry in
        # tag_style_props, and italic alt text is the conventional way to
        # show it anyway.
        self.runs = [(f"[{alt}]", ["em"])]
        self.block_tags = block_tags
        self.language = None


def _image_draw_size(pixbuf, max_width_pt, max_height_pt):
    """Point size to draw at: the image's own 96dpi size, shrunk to fit
    the column, then shrunk again if it's still too tall for a page.
    Never enlarged -- a 32px icon blown up to column width looks broken."""
    natural_w = pt(pixbuf.get_width())
    natural_h = pt(pixbuf.get_height())
    if natural_w <= 0 or natural_h <= 0:
        return None
    scale = min(1.0, max_width_pt / natural_w, max_height_pt / natural_h)
    return natural_w * scale, natural_h * scale


def _diagram_block(item, x, page_width, page_height, font, palette):
    """One mermaid diagram, laid out for paper. None if it can't be.

    The scene is built here rather than reused from the screen because a
    scene is only valid for the font it was measured against, and paper
    doesn't always print in the screen's font (see _base_font). Laying it
    out again from the parsed model costs a few milliseconds per diagram
    and keeps every box the right size around its text.
    """
    from . import mermaid
    try:
        scene = mermaid.build_scene(item.diagram, font)
    except Exception:
        # Same reasoning as renderer._emit_diagram: a diagram that trips
        # over a layout bug must not take the print job down with it.
        return None
    natural_w, natural_h = pt(scene.width), pt(scene.height)
    if natural_w <= 0 or natural_h <= 0:
        return None
    scale = min(1.0,
                (page_width - x) / natural_w,
                page_height * IMAGE_MAX_PAGE_FRACTION / natural_h)
    return _Block.diagram(x, scene, scale, palette, font)


def _draw_diagram_scene(cr, scene, palette, font):
    from . import mermaid
    mermaid.draw.draw_scene(cr, scene, palette, font)


def _build_blocks(context, style_table, print_model, page_width, page_height, dark,
                  href_by_tag=None):
    href_by_tag = href_by_tag or {}
    blocks = []
    # One description for the whole job rather than a Gtk.Settings lookup
    # per layout.
    font = _base_font()
    leading = _leading_pt(context, font)
    code_bg = style_table.get("code-inline", {}).get("background-rgba")
    code_bg_rgb = _rgba_rgb(code_bg) if code_bg is not None else None
    heading_rule = tagdefs.heading_rule_rgba(dark)
    heading_rule_rgb = _rgba_rgb(heading_rule)
    for item in print_model:
        before = len(blocks)
        if item.kind == "image":
            x = _left_margin_pt(item.block_tags)
            # Read the texture now, at print time, rather than at render
            # time -- a remote image the reader loaded after opening the
            # document has pixels by now and should print.
            texture = item.image.texture if item.image is not None else None
            pixbuf = Gdk.pixbuf_get_from_texture(texture) if texture is not None else None
            size = _image_draw_size(
                pixbuf, page_width - x, page_height * IMAGE_MAX_PAGE_FRACTION
            ) if pixbuf is not None else None
            if size is not None:
                blocks.append(_Block.image(x, pixbuf, size[0], size[1]))
            else:
                alt = item.image.alt if item.image is not None else "image"
                layout = _build_text_layout(
                    context, style_table, _AltTextItem(alt, item.block_tags),
                    page_width - x, font=font, leading_pt=leading,
                )
                blocks.append(_Block.text(x, layout))
        elif item.kind == "diagram":
            x = _left_margin_pt(item.block_tags)
            block = _diagram_block(
                item, x, page_width, page_height, font, tagdefs.diagram_palette(dark))
            if block is not None:
                blocks.append(block)
            else:
                layout = _build_text_layout(
                    context, style_table, _AltTextItem("diagram", item.block_tags),
                    page_width - x, font=font,
                )
                blocks.append(_Block.text(x, layout))
        elif item.kind == "paragraph":
            x = _left_margin_pt(item.block_tags)
            layout = _build_text_layout(
                context, style_table, item, page_width - x, _marker_hang_pt(item),
                font, leading,
            )
            run_tags = {tag for _text, tags in item.runs for tag in tags}
            heading_level = next((n for n in range(1, 7) if f"heading{n}" in run_tags), None)
            rule = heading_rule_rgb if heading_level in (1, 2) else None
            blocks.append(_Block.text(
                x, layout, rule=rule, heading_level=heading_level,
                chips=_inline_code_spans(item), chip_rgb=code_bg_rgb,
                links=_link_spans(item, href_by_tag),
            ))
        elif item.kind == "code-block":
            # Same shape as the on-screen panel: it spans the content
            # column edge to edge, with the code text inset by the pad on
            # every side.
            x = _left_margin_pt(item.block_tags)
            layout = _build_text_layout(
                context, style_table, item, page_width - x - 2 * CODE_BLOCK_PAD_PT,
                font=font, leading_pt=leading,
            )
            panel = {
                "x": x,
                "width": page_width - x,
                "pad": CODE_BLOCK_PAD_PT,
                "radius": CODE_BLOCK_RADIUS_PT,
                "fill_rgb": code_bg_rgb or (0.96, 0.96, 0.96),
            }
            blocks.append(_Block.text(x + CODE_BLOCK_PAD_PT, layout, panel=panel))
        elif item.kind == "hr":
            blocks.append(_Block.hr())
        elif item.kind == "table":
            x = _left_margin_pt(item.block_tags)
            row_layouts, row_heights, col_widths = _build_table_rows(
                context, style_table, item.rows, page_width - x, font
            )
            if row_layouts:
                blocks.append(_Block.table(x, row_layouts, row_heights, col_widths))
        if len(blocks) > before:
            # One leading on top of the collapsed margin -- on screen
            # every line carries it, including a block's last.
            blocks[before].gap = pt(item.gap) + leading
    return blocks


def _out_of_room(y, gap, page_height):
    """True if even the inter-block gap wouldn't fit before this block
    starts. Used by table/layout blocks, which paginate their own content
    chunk-by-chunk once started -- this is only the pre-check for whether
    there's room to *begin*. hr is atomic (never chunked) so it checks its
    own full height too, inline, rather than through this helper."""
    return y + gap >= page_height


# A lead-in paragraph short enough to count as "introducing" whatever
# follows it (#22), rather than an ordinary paragraph that just happens
# to precede one.
LEAD_IN_MAX_LINES = 3
# "The first couple of lines" of the block that follows a heading (#23).
HEADING_LOOKAHEAD_LINES = 2
# Never place, nor strand, fewer than this many consecutive lines of a
# wrapped paragraph at either side of a page break (#22 part 2).
MIN_LINES_AT_BREAK = 2


def _heading_group_height(blocks, i):
    """Vertical room needed to keep block `i` (a heading) together with:
    any headings immediately chained after it -- so `## A` followed by
    `### B` moves as one unit rather than each being judged only against
    the next block -- plus a look-ahead into whatever follows that
    chain: a full atomic/table block (never partly shown), or the first
    couple of lines of a paragraph/code block.
    """
    total = 0.0
    j = i
    while j < len(blocks) and blocks[j].heading_level is not None:
        b = blocks[j]
        total += (b.gap if j > i else 0.0) + b.height
        if b.rule:
            total += HEADING_RULE_PAD_PT + HEADING_RULE_WIDTH_PT
        j += 1
    if j < len(blocks):
        b = blocks[j]
        gap = b.gap if j > i else 0.0
        if b.kind == "layout":
            lookahead = b.lines[:HEADING_LOOKAHEAD_LINES]
            height = (lookahead[-1][1] + lookahead[-1][2]) - lookahead[0][1] if lookahead else 0.0
        else:
            height = b.height
        total += gap + height
    return total


def _paginate(blocks, page_height):
    pages, current, y = [], [], 0.0

    def new_page():
        nonlocal current, y
        pages.append(current)
        current, y = [], 0.0

    def break_for(block, gap):
        """Start a fresh page for an atomic/table `block` that doesn't
        fit here. First tries to keep a short lead-in paragraph with it
        (#22): if the page's last entry is a short paragraph fragment,
        and it plus `block` would fit a *fresh* page together, retract
        that fragment and re-place it at the top of the new page ahead
        of `block`, instead of stranding it alone at this page's foot.
        Returns the gap to use before `block` wherever it lands."""
        nonlocal current, y
        entry = current[-1] if current else None
        if (entry is not None and entry["type"] == "layout"
                and len(entry["lines"]) <= LEAD_IN_MAX_LINES):
            pad = entry["panel"]["pad"] if entry["panel"] else 0.0
            entry_height = entry["height"] + 2 * pad
            if entry_height + gap + block.height <= page_height:
                current.pop()
                new_page()
                delta = pad - entry["top"]
                entry["top"] += delta
                entry["y"] += delta
                current.append(entry)
                y = entry_height
                return gap
        new_page()
        return 0.0

    for i, block in enumerate(blocks):
        # A page break already separates a block from what came before it,
        # so the gap it was given only applies mid-page.
        gap = block.gap if current else 0.0
        if block.kind == "hr":
            if y + gap + block.height > page_height and current:
                gap = break_for(block, gap)
            current.append({"type": "hr", "y": y + gap + block.height / 2})
            y += gap + block.height
        elif block.kind == "image":
            # Atomic like hr -- an image is never sliced across a page
            # break; _image_draw_size already guaranteed it fits on one.
            if y + gap + block.height > page_height and current:
                gap = break_for(block, gap)
            current.append({
                "type": "image", "x": block.x, "y": y + gap,
                "pixbuf": block.pixbuf, "size": block.draw_size,
            })
            y += gap + block.height
        elif block.kind == "diagram":
            # Atomic like an image: _diagram_block already scaled it to
            # fit one page, and a diagram sliced in half is unreadable in
            # a way a paragraph is not.
            if y + gap + block.height > page_height and current:
                gap = break_for(block, gap)
            current.append({
                "type": "diagram", "x": block.x, "y": y + gap,
                "scene": block.scene, "scale": block.scale,
                "palette": block.palette, "font": block.font,
            })
            y += gap + block.height
        elif block.kind == "table":
            if current and _out_of_room(y, gap, page_height):
                gap = break_for(block, gap)
            y += gap
            row_layouts, row_heights = block.rows
            total_rows = len(row_layouts)
            i2 = 0
            while i2 < len(row_layouts):
                rows_here, row_y = [], y
                while i2 < len(row_layouts):
                    if row_y + row_heights[i2] > page_height and rows_here:
                        break
                    # Global row index travels with each row (not reset
                    # per page) so _draw_entry can tell whether a given
                    # row is the table's true last row -- it draws a rule
                    # after every row except that one, to match the
                    # on-screen rules-between-rows style exactly.
                    rows_here.append((row_layouts[i2], row_y, row_heights[i2], i2))
                    row_y += row_heights[i2] + TABLE_ROW_GAP_PT
                    i2 += 1
                current.append({
                    "type": "table", "x": block.x, "col_widths": block.col_widths,
                    "rows": rows_here, "total_rows": total_rows,
                })
                y = row_y
                if i2 < len(row_layouts):
                    new_page()
        else:  # "layout": paragraph or code-block
            if block.heading_level is not None and current:
                # #23: look ahead through any chained headings to
                # whatever they introduce, and break *before* the
                # heading rather than after it if the group doesn't fit
                # here but would fit a fresh page.
                lookahead = _heading_group_height(blocks, i)
                if lookahead <= page_height and y + gap + lookahead > page_height:
                    new_page()
                    gap = 0.0
            if current and _out_of_room(y, gap, page_height):
                new_page()
                gap = 0.0
            y += gap
            lines = block.lines
            # Padding for the code panel, and room for a heading's rule.
            # Both are per *page fragment*: a fence split across a page
            # break gets a self-contained panel on each side of it.
            pad = block.panel["pad"] if block.panel else 0.0
            rule_space = (HEADING_RULE_PAD_PT + HEADING_RULE_WIDTH_PT) if block.rule else 0.0
            li = 0
            while li < len(lines):
                y += pad
                remaining = page_height - y - pad - rule_space
                chunk_top = lines[li][1]
                li_start = li
                chunk = []
                while li < len(lines):
                    _line, y_top, height, _baseline, _x_off = lines[li]
                    if (y_top - chunk_top) + height > remaining and chunk:
                        break
                    chunk.append(lines[li])
                    li += 1
                if not chunk:
                    # A single line taller than a whole page: draw it anyway
                    # (it will clip) rather than loop forever.
                    chunk = [lines[li]]
                    li += 1
                elif len(chunk) < MIN_LINES_AT_BREAK and li < len(lines) and current:
                    # Orphan (#22 part 2): a single line alone at this
                    # page's foot, with more of the block still to come.
                    # Defer the whole thing to a fresh page instead --
                    # guaranteed to fit at least this line, usually
                    # several more. The `current` guard is what stops
                    # this from bouncing forever when it's already the
                    # top of an empty page.
                    li = li_start
                    new_page()
                    continue
                elif len(lines) - li == 1 and len(chunk) > MIN_LINES_AT_BREAK:
                    # Widow: only one line would be left to start the
                    # next page alone. Hold one more line back so it
                    # gets two instead.
                    chunk.pop()
                    li -= 1
                chunk_height = (chunk[-1][1] + chunk[-1][2]) - chunk[0][1]
                current.append({
                    "type": "layout", "x": block.x,
                    # `y` is where the chunk's *top* goes, but
                    # show_layout_line draws from the baseline, so the
                    # entry is anchored on the first line's baseline --
                    # one ascent further down. Chrome (panel, rule) needs
                    # the top and height instead, so both travel along.
                    "y": y + (chunk[0][3] - chunk[0][1]),
                    # get_baseline() is absolute (layout-origin-relative),
                    # same coordinate space as y_top -- draw position must
                    # track the *baseline* delta directly, not y_top plus
                    # baseline (that double-counts each line's ascent).
                    "lines": chunk, "origin_baseline": chunk[0][3],
                    "top": y, "height": chunk_height,
                    "panel": block.panel,
                    # Whole-layout byte ranges; each Pango line knows its
                    # own, so they need no per-chunk slicing.
                    "chips": block.chips, "chip_rgb": block.chip_rgb,
                    "links": block.links,
                    # Only the fragment that ends the heading carries its
                    # rule; a heading that wrapped across a page break
                    # would otherwise get one on every page.
                    "rule": block.rule if li >= len(lines) else None,
                })
                y += chunk_height + pad
                if li < len(lines):
                    new_page()
            y += rule_space
    if current or not pages:
        pages.append(current)
    return pages


def _draw_inline_code_chips(cr, entry):
    """Paint the padded rounded chip behind each inline-code span on this
    page fragment, matching decorated_textview.py on screen. Positions
    come from the Pango line rather than the layout, so a chip that wraps
    gets one chip per line, and each line's own extents (relative to its
    baseline, and excluding the inter-line spacing) size it."""
    spans, rgb = entry["chips"], entry["chip_rgb"]
    if not spans or rgb is None:
        return
    pad_x, pad_y = pt(tagdefs.INLINE_CODE_PAD_X), pt(tagdefs.INLINE_CODE_PAD_Y)
    cr.save()
    cr.set_source_rgb(*rgb)
    for line, _y_top, _height, baseline, x_off in entry["lines"]:
        line_start, line_end = line.start_index, line.start_index + line.length
        _ink, logical = line.get_extents()
        top = (entry["y"] + (baseline - entry["origin_baseline"])
               + Pango.units_to_double(logical.y))
        height = Pango.units_to_double(logical.height)
        for span_start, span_end in spans:
            start, end = max(span_start, line_start), min(span_end, line_end)
            if start >= end:
                continue
            x0 = Pango.units_to_double(line.index_to_x(start, False))
            x1 = Pango.units_to_double(line.index_to_x(end - 1, True))
            if x1 < x0:
                x0, x1 = x1, x0
            _rounded_rect(
                cr, entry["x"] + x_off + x0 - pad_x, top - pad_y,
                (x1 - x0) + pad_x * 2, height + pad_y * 2,
                pt(tagdefs.INLINE_CODE_RADIUS),
            )
            cr.fill()
    cr.restore()


def _draw_link_annotations(cr, entry):
    """Lay a clickable PDF 'Link' annotation over every hyperlink run on
    this page fragment -- the piece that makes a printed link actually
    followable rather than just blue underlined text.

    Geometry is the per-line index_to_x walk the inline-code chips already
    use. The rectangle is handed to cairo via cr.user_to_device: a Link
    tag's explicit `rect` is read in the surface's initial device space
    and ignores the current transform, so the page translate _on_draw_page
    applies has to be baked in here rather than left to cairo. On a
    non-PDF surface (a raster printer) tag_begin/tag_end for TAG_LINK are
    silently ignored, so this is a no-op there.
    """
    spans = entry["links"]
    if not spans:
        return
    for line, _y_top, _height, baseline, x_off in entry["lines"]:
        line_start, line_end = line.start_index, line.start_index + line.length
        _ink, logical = line.get_extents()
        top = (entry["y"] + (baseline - entry["origin_baseline"])
               + Pango.units_to_double(logical.y))
        height = Pango.units_to_double(logical.height)
        for span_start, span_end, href in spans:
            start, end = max(span_start, line_start), min(span_end, line_end)
            if start >= end:
                continue
            x0 = Pango.units_to_double(line.index_to_x(start, False))
            x1 = Pango.units_to_double(line.index_to_x(end - 1, True))
            if x1 < x0:
                x0, x1 = x1, x0
            dx0, dy0 = cr.user_to_device(entry["x"] + x_off + x0, top)
            dx1, dy1 = cr.user_to_device(entry["x"] + x_off + x1, top + height)
            rx0, rx1 = sorted((dx0, dx1))
            ry0, ry1 = sorted((dy0, dy1))
            cr.tag_begin(cairo.TAG_LINK, "uri='%s' rect=[%g %g %g %g]" % (
                _link_uri_attr(href), rx0, ry0, rx1 - rx0, ry1 - ry0))
            cr.tag_end(cairo.TAG_LINK)


def _draw_entry(cr, entry, page_width):
    if entry["type"] == "hr":
        cr.save()
        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.set_line_width(HR_HEIGHT_PT)
        cr.move_to(0, entry["y"])
        cr.line_to(page_width, entry["y"])
        cr.stroke()
        cr.restore()
    elif entry["type"] == "table":
        # Thin rule under each row except the table's true last one --
        # matches tables.py's on-screen Gtk.Separator-between-rows style
        # (no per-cell boxes: tried a full grid, rejected as too heavy/
        # inconsistent with a plain-rules look).
        x0, col_widths = entry["x"], entry["col_widths"]
        for cell_layouts, row_y, row_h, row_index in entry["rows"]:
            cx = x0
            for col_index, layout in enumerate(cell_layouts):
                cr.move_to(cx + TABLE_CELL_PAD_PT, row_y + TABLE_CELL_PAD_PT)
                PangoCairo.show_layout(cr, layout)
                cx += col_widths[col_index]
            if row_index < entry["total_rows"] - 1:
                rule_y = row_y + row_h + TABLE_ROW_GAP_PT / 2
                cr.save()
                cr.set_source_rgb(*TABLE_RULE_RGB)
                cr.set_line_width(0.75)
                cr.move_to(x0, rule_y)
                cr.line_to(x0 + sum(col_widths[:len(cell_layouts)]), rule_y)
                cr.stroke()
                cr.restore()
    elif entry["type"] == "image":
        pixbuf = entry["pixbuf"]
        width, height = entry["size"]
        cr.save()
        cr.translate(entry["x"], entry["y"])
        # Gdk.cairo_set_source_pixbuf places the image at its pixel size,
        # so the scale to point-size has to be on the matrix, not on the
        # source.
        cr.scale(width / pixbuf.get_width(), height / pixbuf.get_height())
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
        cr.paint()
        cr.restore()
    elif entry["type"] == "diagram":
        cr.save()
        cr.translate(entry["x"], entry["y"])
        # The scene is in nominal 96dpi pixels and the printer's context
        # is in points, so one scale here puts every length in the scene
        # -- geometry, stroke widths and the absolute font sizes its text
        # is laid out at -- on paper at the proportions it has on screen.
        cr.scale(PX_TO_PT * entry["scale"], PX_TO_PT * entry["scale"])
        _draw_diagram_scene(cr, entry["scene"], entry["palette"], entry["font"])
        cr.restore()
    elif entry["type"] == "layout":
        panel = entry["panel"]
        if panel is not None:
            pad = panel["pad"]
            cr.save()
            _rounded_rect(
                cr, panel["x"], entry["top"] - pad,
                panel["width"], entry["height"] + pad * 2, panel["radius"],
            )
            cr.set_source_rgb(*panel["fill_rgb"])
            cr.fill()
            cr.restore()
        _draw_inline_code_chips(cr, entry)
        for line, _y_top, _height, baseline, x_off in entry["lines"]:
            cr.move_to(entry["x"] + x_off, entry["y"] + (baseline - entry["origin_baseline"]))
            PangoCairo.show_layout_line(cr, line)
        _draw_link_annotations(cr, entry)
        if entry["rule"] is not None:
            rule_y = entry["top"] + entry["height"] + HEADING_RULE_PAD_PT
            cr.save()
            cr.set_source_rgb(*entry["rule"])
            cr.set_line_width(HEADING_RULE_WIDTH_PT)
            cr.move_to(entry["x"], rule_y)
            cr.line_to(page_width, rule_y)
            cr.stroke()
            cr.restore()


# Running head/foot, opt-in (see print_document's header_footer arg): a
# small, muted line above and below the body -- distinct from document
# text so it reads as page chrome, not content. Sized in points like
# everything else here, not off PRINT_BASE_PT, since it should stay put
# regardless of what body size print settles on.
HEADER_FOOTER_FONT_PT = 9.0
HEADER_FOOTER_RGB = (0.45, 0.45, 0.45)
HEADER_BAND_PT = 24.0
HEADER_TEXT_TOP_PT = 4.0
# Body content otherwise starts right at the top page margin (no header)
# or right after the header band (with one) -- both read as a bit tight.
# Unlike FOOTER_MARGIN_SHIFT_PT below, this isn't funded by trimming the
# top page margin: it's a plain addition, present either way.
BODY_TOP_MARGIN_PT = 14.0
# The gap between the last line of body content and the footer text is
# exactly FOOTER_TEXT_TOP_PT, whatever FOOTER_BAND_PT is -- body content
# always fills right up to the top of the footer band. So both need to
# grow by the same FOOTER_MARGIN_SHIFT_PT (funded by _print_page_setup's
# matching cut to the bottom page margin) for that shift to land as
# clearance above the footer rather than just moving the whole band, and
# the footer with it, further from the page's true bottom edge.
FOOTER_BAND_PT = 20.0 + FOOTER_MARGIN_SHIFT_PT
FOOTER_TEXT_TOP_PT = 4.0 + FOOTER_MARGIN_SHIFT_PT


def _header_footer_font():
    desc = _base_font()
    desc.set_size(int(HEADER_FOOTER_FONT_PT * Pango.SCALE))
    return desc


def _header_left_text(doc_title, file_name):
    """The document's own title (its first h1) alongside the file name,
    so a stack of printouts can still be traced back to its file -- just
    the file name on its own if the document has no title, so the two
    never show up as a spurious "file.md -- file.md"."""
    if doc_title and doc_title != file_name:
        return f"{doc_title} – {file_name}"
    return file_name


def _draw_header_footer_line(cr, context, font, left, right, y, width):
    """One line of small print, `left` flush to the text column's left
    edge and `right` flush to its right -- the same simple two-up layout
    for both the header and the footer."""
    cr.save()
    cr.set_source_rgb(*HEADER_FOOTER_RGB)
    if left:
        layout = context.create_pango_layout()
        layout.set_font_description(font)
        layout.set_text(left, -1)
        cr.move_to(0, y)
        PangoCairo.show_layout(cr, layout)
    if right:
        layout = context.create_pango_layout()
        layout.set_font_description(font)
        layout.set_text(right, -1)
        _ink, logical = layout.get_extents()
        cr.move_to(width - Pango.units_to_double(logical.width), y)
        PangoCairo.show_layout(cr, layout)
    cr.restore()


class PrintCoordinator:
    """One instance per print action; holds no state between calls."""

    def print_document(self, parent_window, print_model, dark, doc_title, file_name,
                        action=Gtk.PrintOperationAction.PRINT_DIALOG, export_path=None,
                        header_footer=False, link_targets=None):
        # header_footer is a plain argument, decided by the caller before
        # the dialog even opens -- not read out of the dialog itself.
        # GtkPrintOperation's create-custom-widget/custom-widget-apply
        # would be the natural fit, but GTK's print dialog is routed
        # through the xdg-desktop-portal on this (and most modern
        # Wayland) desktops, and that path flatly refuses to host an
        # app-supplied widget ("create-custom-widget not supported with
        # portal", confirmed live against this GTK). See window.py's
        # "print-header-footer" menu action for where the choice is made
        # instead.
        op = Gtk.PrintOperation()
        op.set_job_name(file_name)
        op.set_default_page_setup(_print_page_setup(header_footer))
        # "Print to File" otherwise suggests a bare "output.pdf"
        # (gtkprintbackendfile.c falls back to that literal string when
        # this key is unset) -- job_name above has no bearing on it, that
        # only names the job, not the file. Stem of the source file's own
        # name, not doc_title: a document can have several h1-derived
        # titles across edits, but only one file name.
        settings = Gtk.PrintSettings()
        settings.set(Gtk.PRINT_SETTINGS_OUTPUT_BASENAME, PurePath(file_name).stem)
        op.set_print_settings(settings)
        if export_path:
            op.set_export_filename(export_path)
        state = {
            "header_footer": header_footer,
            "header_left": _header_left_text(doc_title, file_name),
            # tag name -> dispatch target, straight off the renderer. Only
            # the {"type": "url"} entries matter here; footnote jumps and
            # the like stay screen-only.
            "link_targets": link_targets or {},
        }
        op.connect("begin-print", self._on_begin_print, print_model, dark, state)
        op.connect("draw-page", self._on_draw_page, state)
        return op.run(action, parent_window)

    def _on_begin_print(self, op, context, print_model, dark, state):
        style_table = tagdefs.tag_style_props(dark)
        # Only links that already carry a URI scheme (http/https/mailto/...)
        # become PDF annotations. A bare relative href resolves against the
        # author's own directory on screen (window._open_href); baking that
        # local path -- or a dead in-page "#anchor" -- into a shared PDF is
        # worse than leaving it as plain blue text.
        href_by_tag = {
            name: target["href"]
            for name, target in (state.get("link_targets") or {}).items()
            if target.get("type") == "url" and target.get("href")
            and GLib.uri_parse_scheme(target["href"]) is not None
        }
        width, height = context.get_width(), context.get_height()
        header_band = HEADER_BAND_PT if state["header_footer"] else 0.0
        footer_band = FOOTER_BAND_PT if state["header_footer"] else 0.0
        body_top = header_band + BODY_TOP_MARGIN_PT
        body_height = height - body_top - footer_band
        state["width"] = width
        state["height"] = height
        state["header_band"] = header_band
        state["body_top"] = body_top
        blocks = _build_blocks(
            context, style_table, print_model, width, body_height, dark, href_by_tag)
        # Pango.LayoutLine keeps only a *weak* back-reference to its parent
        # Pango.Layout -- without this, `blocks` (and therefore every
        # Layout) would be garbage collected the moment this method
        # returns, and draw-page would run against dangling lines.
        state["blocks"] = blocks
        pages = _paginate(blocks, body_height)
        state["pages"] = pages
        op.set_n_pages(len(pages))

    def _on_draw_page(self, op, context, page_nr, state):
        cr = context.get_cairo_context()
        cr.save()
        cr.translate(0, state["body_top"])
        for entry in state["pages"][page_nr]:
            _draw_entry(cr, entry, state["width"])
        cr.restore()
        if state["header_footer"]:
            font = _header_footer_font()
            print_time = GLib.DateTime.new_now_local().format("%x %X")
            _draw_header_footer_line(
                cr, context, font, state["header_left"], print_time,
                HEADER_TEXT_TOP_PT, state["width"],
            )
            footer_y = state["height"] - FOOTER_BAND_PT + FOOTER_TEXT_TOP_PT
            page_label = _("Page {page} of {total}").format(
                page=page_nr + 1, total=len(state["pages"]),
            )
            _draw_header_footer_line(
                cr, context, font, None, page_label, footer_y, state["width"],
            )

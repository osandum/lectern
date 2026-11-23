"""A Gtk.TextView that paints the block chrome Gtk.TextTag can't express.

`Gtk.TextTag` has no border property, and its two background properties
are both wrong for what GitHub-style Markdown needs: `background-rgba`
paints a tight rectangle hugging the glyph runs (no padding, no corner
radius), and `paragraph-background-rgba` paints the full line width with
no vertical padding and no radius either. So the three treatments that
need a *box* -- the rule under h1/h2, the inline-code chip, and the
fenced-code panel -- are drawn here instead, underneath the text, by
overriding snapshot() and locating the tagged ranges with
get_iter_location().

Drawing a background larger than the text does not by itself create the
space it occupies, so the padding constants below are paired with layout
that reserves room for them: `heading{1,2}`'s pixels-below-lines in
tags.py for the rule, and renderer.py's fence block margins (which add
CODE_BLOCK_PADDING on top of the normal inter-block gap) for the code
panel. Changing one without the other makes the chrome overlap its
neighbours.

This view also owns the *content column*: where the text starts, how wide
it is allowed to get, and the scale everything is laid out at. Both live
here rather than in window.py because both are answers to "how wide am I",
which only the widget being allocated knows -- see _sync_metrics.
"""
import math

import gi
gi.require_foreign("cairo")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Adw, Graphene, Pango

from . import tags as tagdefs
from . import clipboard

# Slack (px) added around the visible rectangle when deciding which
# tagged ranges to draw, so a chip or panel whose text sits just off
# screen still has its padding painted at the viewport edge.
_VISIBLE_SLACK = 64


def _rounded_rect(cr, x, y, width, height, radius):
    radius = max(0.0, min(radius, width / 2.0, height / 2.0))
    if radius == 0:
        cr.rectangle(x, y, width, height)
        return
    x2, y2 = x + width, y + height
    cr.new_sub_path()
    cr.arc(x2 - radius, y + radius, radius, -math.pi / 2, 0.0)
    cr.arc(x2 - radius, y2 - radius, radius, 0.0, math.pi / 2)
    cr.arc(x + radius, y2 - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()


class DecoratedTextView(Gtk.TextView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # The zoom factor to lay out at, pushed in by zoom.py: tags.py's
        # lengths are all written against a 100% base font.
        self._layout_scale = 1.0
        # Set by window.py after each render -- clipboard.py needs both
        # to turn a selection's tags back into a link href or an
        # anchor's original hr/image/table/diagram. Plain dicts rather
        # than a MarkdownRenderer reference, so an unrendered/empty-state
        # view (no renderer yet) just sees "nothing resolves", not an
        # AttributeError.
        self.dispatch_targets = {}
        self.anchor_descriptors = {}
        # A freshly rendered buffer arrives with its tags at base values,
        # whatever the current zoom.
        self.connect("notify::buffer", lambda *_a: self._sync_metrics())
        self._sync_metrics()

    # -- the content column ------------------------------------------------

    def set_layout_scale(self, scale):
        """Lay out at `scale` (1.0 == 100%). zoom.py owns this value."""
        if scale == self._layout_scale:
            return
        self._layout_scale = scale
        self._sync_metrics()

    def _scaled(self, length):
        """One of tags.py's base-100% lengths, in the pixels this view is
        currently drawing at."""
        return length * self._layout_scale

    def _gutter(self, width):
        """Extra inset per side, beyond the base content margin, keeping
        the body text no wider than MAX_MEASURE_EM. Zero until the window
        exceeds that measure; in em, so it tracks the zoom rather than
        capping at a fixed pixel count."""
        target = tagdefs.MAX_MEASURE_EM * self._scaled(tagdefs.BASE_FONT_PX)
        return max(0, math.ceil((width - 2 * self._base_margin() - target) / 2))

    def _base_margin(self):
        return round(self._scaled(tagdefs.CONTENT_MARGIN))

    def _leading(self):
        """Line-height padding for the font this view actually lays out
        with. The Pango context reports the CSS font size as soon as
        zoom.py's provider loads, so this tracks zoom on its own."""
        context = self.get_pango_context()
        desc = context.get_font_description()
        metrics = context.get_metrics(desc, None)
        natural = (metrics.get_ascent() + metrics.get_descent()) / Pango.SCALE
        font_px = desc.get_size() / Pango.SCALE
        if font_px <= 0 or natural <= 0:
            return 0  # not yet styled; the next sync gets it
        return tagdefs.line_leading(font_px, natural)

    def _marker_width(self, shape):
        """How wide a list marker (tags.list_hang_shape) is in the font the
        view lays out with: to where the text after it starts, so its
        trailing space counts. Set in tabular numerals, as the
        list-marker tag sets it."""
        layout = Pango.Layout.new(self.get_pango_context())
        layout.set_text(shape, -1)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_font_features_new("tnum 1"))
        layout.set_attributes(attrs)
        end = len(shape.encode("utf-8"))
        return layout.index_to_pos(end).x / Pango.SCALE

    def _sync_metrics(self, width=None):
        """Put the content column where the current zoom and window width
        want it.

        The tag half is easy to miss: a Gtk.TextTag left-margin *replaces*
        the view's rather than adding to it (confirmed empirically), so
        every tag spelling out an indent -- lists, blockquotes, fenced code
        -- is positioned from the window edge. Growing the view's own
        margins alone centres the prose and leaves those at the left edge.
        """
        base = self._base_margin()
        gutter = self._gutter(self.get_width() if width is None else width)
        self.set_left_margin(base + gutter)
        self.set_right_margin(base + gutter)
        self.set_top_margin(base)
        self.set_bottom_margin(base)
        buffer = self.get_buffer()
        if buffer is not None:
            tagdefs.apply_metrics(buffer.get_tag_table(), self._layout_scale,
                                  gutter, self._leading(), self._marker_width)
        self.queue_draw()

    def do_size_allocate(self, width, height, baseline):
        # Before chaining up, so this allocation already lays out at the
        # margins its own width implies. Setting a margin queues a resize,
        # but only when the value changed, and the computation depends on
        # nothing the margins affect -- so a resize settles in one extra
        # pass rather than oscillating.
        self._sync_metrics(width)
        Gtk.TextView.do_size_allocate(self, width, height, baseline)

    def do_copy_clipboard(self):
        """Put text/html and text/markdown on the clipboard alongside
        plain text, reconstructed from the selection's own Gtk.TextTags
        -- the default handler this overrides only ever puts plain
        text there. Fires for both Ctrl+C and the context menu's Copy,
        since GTK's own menu is wired to this same virtual method."""
        buffer = self.get_buffer()
        bounds = buffer.get_selection_bounds() if buffer is not None else None
        if not bounds:
            Gtk.TextView.do_copy_clipboard(self)
            return
        start, end = bounds
        plain = buffer.get_text(start, end, True)
        html, markdown = clipboard.selection_to_html_and_markdown(
            buffer, self.dispatch_targets, self.anchor_descriptors, start, end,
        )
        texture = clipboard.selection_image_texture(buffer, self.anchor_descriptors, start, end)
        provider = clipboard.make_content_provider(plain, html, markdown, texture)
        self.get_clipboard().set_content(provider)

    # -- buffer/geometry helpers -------------------------------------------

    def _visible_bounds(self):
        """The buffer range worth drawing chrome for: everything on screen,
        plus a little slack. Without this, every snapshot would walk (and
        force layout validation of) the whole document."""
        buffer = self.get_buffer()
        rect = self.get_visible_rect()
        if rect.height <= 0:
            return buffer.get_bounds()
        lo, _ = self.get_line_at_y(rect.y - _VISIBLE_SLACK)
        hi, _ = self.get_line_at_y(rect.y + rect.height + _VISIBLE_SLACK)
        if not hi.ends_line():
            hi.forward_to_line_end()
        return lo, hi

    def _tagged_ranges(self, tag, lo, hi):
        """(start, end) iter pairs where `tag` is applied, restricted to
        [lo, hi] but extended outwards to whole ranges at both edges.

        Every iter here is a fresh copy: Gtk.TextIter is mutable and
        forward_to_tag_toggle() edits it in place, so handing the loop
        variable itself to the caller would let a later iteration silently
        drag an already-returned range's end forward -- which is exactly
        how every chip and panel once grew to swallow the rest of the
        document.
        """
        buffer = self.get_buffer()
        ranges = []
        it = lo.copy()
        if it.has_tag(tag) and not it.starts_tag(tag):
            it.backward_to_tag_toggle(tag)
        while it.compare(hi) < 0:
            if not it.starts_tag(tag):
                if not it.forward_to_tag_toggle(tag):
                    break
                continue
            start = it.copy()
            end = it.copy()
            if not end.forward_to_tag_toggle(tag):
                end = buffer.get_end_iter()
            ranges.append((start, end))
            it = end.copy()
        return ranges

    def _segment_rect(self, start, end):
        """Widget-coordinate rectangle covering one *display* line's worth
        of a tagged range. Only the two endpoints are measured -- within a
        single display line x grows monotonically, so the interior
        characters can't extend the box."""
        first = self.get_iter_location(start)
        last_iter = end.copy()
        if last_iter.compare(start) > 0:
            last_iter.backward_char()
        last = self.get_iter_location(last_iter)
        x0 = min(first.x, last.x)
        y0 = min(first.y, last.y)
        x1 = max(first.x + first.width, last.x + last.width)
        y1 = max(first.y + first.height, last.y + last.height)
        wx, wy = self.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, x0, y0)
        return (wx, wy, x1 - x0, y1 - y0)

    def _display_line_rects(self, start, end):
        """Split a tagged range at display-line boundaries (not paragraph
        boundaries) so a chip that wraps mid-phrase draws as one box per
        visual line rather than one box spanning both."""
        rects = []
        seg_start = start.copy()
        while seg_start.compare(end) < 0:
            seg_end = seg_start.copy()
            # No-op when seg_start already sits at a display line's end
            # (an empty line inside a fenced block, most commonly), which
            # is why advancing below goes by display *line* rather than by
            # character -- a blank line would otherwise never move on.
            self.forward_display_line_end(seg_end)
            if seg_end.compare(seg_start) < 0:
                seg_end.assign(seg_start)
            if seg_end.compare(end) > 0:
                seg_end.assign(end)
            rects.append(self._segment_rect(seg_start, seg_end))
            if not self.forward_display_line(seg_start):
                break
        return rects

    @staticmethod
    def _union_rects(rects):
        if not rects:
            return None
        x0 = min(r[0] for r in rects)
        y0 = min(r[1] for r in rects)
        x1 = max(r[0] + r[2] for r in rects)
        y1 = max(r[1] + r[3] for r in rects)
        return (x0, y0, x1 - x0, y1 - y0)

    def _content_right(self):
        return self.get_width() - self.get_right_margin()

    @staticmethod
    def _set_source_rgba(cr, rgba):
        cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, rgba.alpha)

    # -- the three treatments ----------------------------------------------

    def _draw_code_panels(self, cr, ranges, color):
        """Full-content-width panel behind a fenced block, matching
        GitHub's padding on all four sides. The panel's left edge is the
        code text's own left margin minus that padding, which the
        code-block tag sets so the edge lands exactly where surrounding
        prose starts."""
        pad = self._scaled(tagdefs.CODE_BLOCK_PADDING)
        for start, end in ranges:
            rect = self._union_rects(self._display_line_rects(start, end))
            if rect is None:
                continue
            x, y, width, height = rect
            # A block with lines wider than the window keeps its panel
            # under them rather than cutting it off at the viewport edge.
            right = max(self._content_right(), x + width + pad)
            _rounded_rect(
                cr, x - pad, y - pad, right - (x - pad), height + pad * 2,
                self._scaled(tagdefs.CODE_BLOCK_RADIUS),
            )
            self._set_source_rgba(cr, color)
            cr.fill()

    def _draw_code_chips(self, cr, ranges, color):
        pad_x = self._scaled(tagdefs.INLINE_CODE_PAD_X)
        pad_y = self._scaled(tagdefs.INLINE_CODE_PAD_Y)
        for start, end in ranges:
            # Per display line, not per range: a chip that wraps gets one
            # box on each line rather than a single box spanning both.
            for x, y, width, height in self._display_line_rects(start, end):
                _rounded_rect(
                    cr, x - pad_x, y - pad_y,
                    width + pad_x * 2, height + pad_y * 2,
                    self._scaled(tagdefs.INLINE_CODE_RADIUS),
                )
                self._set_source_rgba(cr, color)
                cr.fill()

    def _draw_heading_rules(self, cr, ranges):
        color = tagdefs.heading_rule_rgba(Adw.StyleManager.get_default().get_dark())
        right = self._content_right()
        self._set_source_rgba(cr, color)
        cr.set_line_width(self._scaled(tagdefs.HEADING_RULE_WIDTH))
        for start, end in ranges:
            rect = self._union_rects(self._display_line_rects(start, end))
            if rect is None:
                continue
            x, y, width, height = rect
            # Half-pixel offset so a 1px stroke lands on one device pixel
            # instead of straddling two and rendering as a grey smear.
            rule_y = round(y + height + self._scaled(tagdefs.HEADING_RULE_PAD)) + 0.5
            cr.move_to(x, rule_y)
            cr.line_to(max(right, x + width), rule_y)
            cr.stroke()

    # -- snapshot ----------------------------------------------------------

    def do_snapshot(self, snapshot):
        buffer = self.get_buffer()
        found = self._decorated_ranges(buffer) if buffer is not None else None
        if found:
            bounds = Graphene.Rect()
            bounds.init(0, 0, self.get_width(), self.get_height())
            cr = snapshot.append_cairo(bounds)
            # One gray for both code treatments, read off the live
            # code-inline tag so a light/dark flip repaints both without
            # this module knowing anything about the palette.
            code_tag = buffer.get_tag_table().lookup("code-inline")
            color = code_tag.get_property("background-rgba") if code_tag else None
            if color is not None:
                self._draw_code_panels(cr, found.get("code-block", ()), color)
                self._draw_code_chips(cr, found.get("code-inline", ()), color)
            self._draw_heading_rules(
                cr, found.get("heading1", []) + found.get("heading2", [])
            )
        Gtk.TextView.do_snapshot(self, snapshot)

    def _decorated_ranges(self, buffer):
        """{tag name: [(start, end), ...]} for every decorated tag with
        something in view, omitting the ones with nothing. Collected up
        front so a viewport with no chrome in it skips the full-size Cairo
        node entirely -- snapshot() runs on every scroll step."""
        lo, hi = self._visible_bounds()
        table = buffer.get_tag_table()
        found = {}
        for name in ("code-block", "code-inline", "heading1", "heading2"):
            tag = table.lookup(name)
            ranges = self._tagged_ranges(tag, lo, hi) if tag is not None else []
            if ranges:
                found[name] = ranges
        return found

"""Text measurement for diagram layout, and the matching layout builder
drawing uses.

Both sides go through `make_layout` on purpose. Layout measures strings
against a scratch context to decide how big a node is; drawing re-lays the
same string out against a cairo context that may be a window or a printer.
Those two land in the same place only if every knob (font, absolute size,
wrap width, alignment) is set identically, so there is one function that
sets them and no second copy to drift.

Sizes here are *absolute* -- `set_absolute_size`, not `set_size`. A point
size would be multiplied by the context's resolution, which is 96dpi for a
window and 72dpi for a Gtk.PrintContext, so the same node would come out a
third smaller on paper than the box laid out for it. Absolute sizes are in
the target's own device units, and printing.py hands the printer a context
already scaled so that one device unit is one nominal 96dpi pixel.
"""
import gi
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GLib, Pango, PangoCairo

import cairo
import html
import re

# Body text in a diagram, in nominal 96dpi pixels. Slightly under the
# 16px body size (zoom.py's 12pt), because a diagram carries many short
# strings at once and reads better a little tighter than prose.
FONT_PX = 14.0

# Longest a single label line gets before it wraps, in characters. Mermaid
# wraps node text too; without a cap one long label stretches its rank
# across the page and squashes everything else.
WRAP_CHARS = 28

# The inline HTML a label may carry that is drawn as formatting rather
# than shown: mermaid labels are HTML, and these four are what
# hand-written diagrams actually use. Any other tag stays visible text.
_STYLE_TAG_RE = re.compile(r"<(/?)(b|strong|i|em)>", re.I)
_STYLE_TAGS = {"b": "b", "strong": "b", "i": "i", "em": "i"}

# Mermaid's own spelling of an entity, for characters its grammar would
# otherwise take as syntax: `#quot;`, `#35;`.
_MERMAID_ENTITY_RE = re.compile(r"(?<!&)#(\w+);")


def _text(text):
    """Markup for plain label text. Labels being HTML, character
    references in them (`&lt;`, and mermaid's `#lt;`) stand for the
    character. They're decoded here, between the tags, never before
    finding them: `&lt;b&gt;` is a way of *showing* `<b>`."""
    text = _MERMAID_ENTITY_RE.sub(
        lambda m: f"&#{m.group(1)};" if m.group(1).isdigit() else f"&{m.group(1)};", text)
    return GLib.markup_escape_text(html.unescape(text), -1)


def label_markup(text):
    """Pango markup for a label: `<b>`/`<strong>` bold, `<i>`/`<em>`
    italic, character references decoded, everything else escaped. Tags that don't nest properly are
    left as visible text rather than guessed at, for the same reason
    parse_label leaves unknown tags alone."""
    out, stack, pos = [], [], 0
    for match in _STYLE_TAG_RE.finditer(text):
        tag = _STYLE_TAGS[match.group(2).lower()]
        if match.group(1):
            if not stack or stack.pop() != tag:
                return _text(text)
        else:
            stack.append(tag)
        out.append(_text(text[pos:match.start()]))
        out.append(f"<{match.group(1)}{tag}>")
        pos = match.end()
    if stack:
        return _text(text)
    out.append(_text(text[pos:]))
    return "".join(out)


_scratch_context = None


def scratch_context():
    """A Pango context to measure against, with no window or printer
    behind it. One per process: building it costs an image surface, and
    nothing about it varies per diagram.

    Unhinted from the start, not first by make_layout: Measurer reads the
    font's line height off this context before laying anything out, and
    reading it hinted made the first diagram in a process measure every
    line taller than all the diagrams after it."""
    global _scratch_context
    if _scratch_context is None:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        _scratch_context = _unhint_metrics(PangoCairo.create_context(cairo.Context(surface)))
    return _scratch_context


def font_for(base, font_px, *, bold=False, italic=False, mono=False):
    desc = base.copy() if base is not None else Pango.FontDescription.new()
    if mono:
        desc.set_family("monospace")
    desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
    desc.set_style(Pango.Style.ITALIC if italic else Pango.Style.NORMAL)
    desc.set_absolute_size(font_px * Pango.SCALE)
    return desc


def _unhint_metrics(context):
    """Turn off metric hinting, on both the measuring and the drawing side.

    Hinted glyph advances are rounded to whole *device* pixels, so they
    stop being proportional to the font size the moment a context is
    scaled -- and view.py scales its context to fit a diagram into the
    window. A label measured at 1.0 then re-wraps when drawn at 0.82, which
    put a third line inside a box laid out for two. Unhinted advances scale
    linearly, so measurement and drawing agree at every zoom level.
    """
    options = cairo.FontOptions()
    options.set_hint_metrics(cairo.HINT_METRICS_OFF)
    PangoCairo.context_set_font_options(context, options)
    return context


def make_layout(context, base_font, font_px, text, *, bold=False, italic=False,
                mono=False, max_px=None, width=None, align="center"):
    layout = Pango.Layout(_unhint_metrics(context))
    layout.set_font_description(font_for(base_font, font_px, bold=bold, italic=italic, mono=mono))
    layout.set_markup(label_markup(text), -1)
    if width is not None:
        layout.set_width(int(width * Pango.SCALE))
        layout.set_alignment({
            "center": Pango.Alignment.CENTER,
            "right": Pango.Alignment.RIGHT,
        }.get(align, Pango.Alignment.LEFT))
    elif max_px is not None:
        layout.set_width(int(max_px * Pango.SCALE))
    if width is not None or max_px is not None:
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    return layout


class Measurer:
    """Measures label text for one diagram, at one font."""

    def __init__(self, base_font, font_px=FONT_PX):
        self.base_font = base_font
        self.font_px = font_px
        self._context = scratch_context()
        self._cache = {}
        # Width of WRAP_CHARS average characters, from the font's own
        # metrics rather than a guess -- "28 characters" is meaningless in
        # pixels until the font says how wide its characters are, and that
        # differs by a third between the narrow GNOME UI font and DejaVu.
        metrics = self._context.get_metrics(font_for(base_font, font_px), None)
        self.char_px = metrics.get_approximate_char_width() / Pango.SCALE
        self.wrap_px = self.char_px * WRAP_CHARS
        self.line_px = (metrics.get_ascent() + metrics.get_descent()) / Pango.SCALE

    def size(self, text, *, bold=False, italic=False, mono=False, wrap=True, scale=1.0):
        """(width, height) of `text` as it will be drawn. Empty text still
        gets one line's height, so an unlabelled node isn't a sliver.

        `scale` must match the scene.Text's own -- it is the size the
        string is measured *and* drawn at, not a post-hoc adjustment.
        """
        key = (text, bold, italic, mono, wrap, scale)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        layout = make_layout(
            self._context, self.base_font, self.font_px * scale, text,
            bold=bold, italic=italic, mono=mono,
            max_px=self.wrap_px if wrap else None,
        )
        width, height = layout.get_pixel_size()
        size = (float(width), float(max(height, self.line_px * scale)))
        self._cache[key] = size
        return size

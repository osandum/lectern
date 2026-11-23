"""Layout measurement harness: assert the *rendered geometry*, not the tag
properties that were asked for.

Everything else in this suite checks what went into the buffer. These tests
check what GTK did with it, via `Gtk.TextView.get_iter_location()` and
`get_line_yrange()`, so "the spacing regressed" can be a failing test rather
than a feeling. Numbers are in em -- multiples of the base font size -- never
pixels, because the font differs between machines (Adwaita Sans on a GNOME
desktop, DejaVu Sans in CI's container) and only the ratios are comparable.

Four things about measuring a Gtk.TextView were established by experiment and
are easy to get wrong (each one silently produces plausible-looking numbers
rather than an error):

1. **An unallocated view reports zero for everything vertical.** `x` and the
   per-line text height come out correct, `y` and `get_line_yrange()` are all
   0, so every computed gap is 0 and the spacing looks perfect. Realizing is
   not what fixes this -- allocating is. `_measure` therefore always calls
   `Gtk.Widget.allocate()` and `assert_valid_geometry` guards it.

2. **CSS font-size only reaches the layout once the window is presented.**
   The widget's Pango context reports the new size as soon as the provider
   loads, even unparented, but the TextView's own layout keeps the old
   metrics until it is presented -- confirmed divergent between a desktop
   (realize() was enough) and CI's Xvfb (it was not). Since zoom.py pins the
   base font via CSS, and that pinning is what makes numbers comparable
   across machines at all, every measurement here goes through present().

3. **With no window manager the window ignores its requested size.** Under
   Xvfb a `default_width=700` window is allocated 640, so the width is forced
   with an explicit `allocate()` *after* present() -- otherwise the wrap
   points, and anything derived from them, are whatever Xvfb's screen size
   happens to make them.

4. **A live zoom change needs a real main loop iteration to take effect.**
   Draining already-pending events is not enough; the relayout arrives on a
   frame-clock tick. `_settle` runs an actual GLib.MainLoop until the font
   size it is waiting for shows up, which doubles as the guard that the CSS
   landed at all.

See also tests/conftest.py, which pins GSK_RENDERER -- presenting a window
under GTK's default renderer aborts outright where there is no GL.
"""
import re

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Pango
import pytest

from markdown_it.tree import SyntaxTreeNode

from lectern.document import make_parser
from lectern.tags import create_tag_table
from lectern.renderer import MarkdownRenderer
from lectern.decorated_textview import DecoratedTextView
from lectern.zoom import ZoomController
from lectern import tags as tagdefs
from lectern import zoom as zoomdefs

_PARSER = make_parser()

# The base font zoom.py pins via CSS, in nominal 96dpi pixels -- the same
# units every constant in tags.py/renderer.py is written in. One em.
BASE_PX = zoomdefs.BASE_PT * 96 / 72

# Wide enough that ordinary prose does not wrap unless a test wants it to,
# narrow enough to stay inside Xvfb's default screen.
WIDTH = 700
HEIGHT = 900

# The measure GitHub caps its rendered body at: 1012px of content against a
# 16px base font. Lectern converges on GitHub's typography elsewhere, so the
# ceiling is borrowed rather than invented.
REFERENCE_MEASURE_EM = 1012 / 16

# A tolerance in em, not a percentage: rounding to whole device pixels can
# move any single measurement by up to a pixel either way, and a pixel is
# 1/16 em. Anything looser would swallow the constants being asserted.
TOL = 1.5 / BASE_PX


class Line:
    """Measured geometry of one logical buffer line, in pixels.

    `space_above`/`space_below` are the difference between the line's full
    box (`get_line_yrange`, which includes pixels-above/below-lines) and the
    text rectangle inside it (`get_iter_location`) -- i.e. they measure the
    block spacing directly, per line, rather than by subtracting neighbours.
    Inter-block gaps live *inside* the following block's first line box in
    GTK's model, so consecutive line boxes are exactly contiguous and
    differencing them would only ever yield 0.
    """

    def __init__(self, index, text, x, space_above, text_height, space_below,
                 line_height, display_lines, first_line_chars, display_ys,
                 text_x=None, wrap_x=None, char_xs=()):
        self.index = index
        self.text = text
        self.x = x
        # Where the line's text starts past a list marker and the spaces
        # after it (x itself, if it has none), and where its second display
        # line starts (None if it doesn't wrap).
        self.text_x = x if text_x is None else text_x
        self.wrap_x = wrap_x
        # The x of each character of the line's first display line.
        self.char_xs = list(char_xs)
        self.space_above = space_above
        self.text_height = text_height
        self.space_below = space_below
        self.line_height = line_height
        self.display_lines = display_lines
        self.first_line_chars = first_line_chars
        # Top of each display line's text rectangle, so intra-paragraph
        # spacing can be read off consecutive rows directly instead of
        # being backed out of the line box (whose text height already
        # carries part of the spacing).
        self.display_ys = display_ys

    @property
    def wrapped_line_spacing(self):
        """Space between consecutive wrapped display lines of this one
        logical line. Raises if it never wrapped."""
        assert len(self.display_ys) > 1, "line did not wrap"
        deltas = [b - a for a, b in zip(self.display_ys, self.display_ys[1:])]
        assert max(deltas) - min(deltas) <= 1, \
            f"uneven wrapped-line pitch {deltas}"
        return sum(deltas) / len(deltas) - self.text_height

    def __repr__(self):
        return (f"Line({self.index}, {self.text[:24]!r}, x={self.x}, "
                f"above={self.space_above}, text_h={self.text_height}, "
                f"below={self.space_below}, disp={self.display_lines})")


class Layout:
    """The measured lines of one rendered document, plus the em unit they
    should be divided by."""

    def __init__(self, lines, font_px, width, left_margin, right_margin):
        self.lines = lines
        self.font_px = font_px
        self.width = width
        self.left_margin = left_margin
        self.right_margin = right_margin

    @property
    def text_column_px(self):
        """Width actually available to a line of prose. Read from the view's
        own margins rather than assumed, so capping the measure by growing
        those margins (what issue #5 proposes) is visible here."""
        return self.width - self.left_margin - self.right_margin

    def em(self, pixels):
        return pixels / self.font_px

    def pitch_em(self, substring):
        """Distance from the last rendered line before `substring` to its
        first, in em -- top of text to top of text.

        The browser-comparable measurement, and the only one that is: a
        browser has no notion of GTK's line boxes, gap tags or collapsed
        margins, only where the text lands.
        """
        previous = None
        for line in self.lines:
            if substring in line.text:
                assert previous is not None, f"{substring!r} is the first line"
                return self.em(line.display_ys[0] - previous.display_ys[-1])
            if line.text.strip():
                previous = line
        raise AssertionError(f"no line containing {substring!r}")

    def find(self, substring):
        """The first line whose text contains `substring`. Tests address
        lines by content, so inserting a block into the probe document
        does not renumber every assertion."""
        for line in self.lines:
            if substring in line.text:
                return line
        raise AssertionError(f"no line containing {substring!r} in "
                             f"{[l.text[:24] for l in self.lines]}")


def _font_px(view):
    return round(view.get_pango_context().get_font_description().get_size()
                 / Pango.SCALE, 1)


def _drain():
    ctx = GLib.MainContext.default()
    for _ in range(500):
        if not ctx.pending():
            break
        ctx.iteration(False)


def _settle(view, expected_px, timeout_ms=5000):
    """Run a real main loop until the CSS font size lands, and return whether
    it did. Blocking iteration, not a drain: the relayout comes on a
    frame-clock tick that has not been queued yet."""
    timed_out = []
    GLib.timeout_add(timeout_ms, lambda: (timed_out.append(True), False)[1])
    ctx = GLib.MainContext.default()
    while not timed_out:
        if _font_px(view) == expected_px:
            return True
        ctx.iteration(True)
    return False


def _build_view():
    """A view configured exactly as window.py configures the real one -- no
    margins passed, because the view sets its own from its allocated width,
    which is precisely what the measure test below is checking."""
    view = DecoratedTextView(
        editable=False, cursor_visible=False,
        wrap_mode=Gtk.WrapMode.WORD_CHAR,
    )
    view.add_css_class("lectern-content")
    return view


def _line_geometry(view, buffer, index):
    it = buffer.get_iter_at_line(index)[1]
    text_rect = view.get_iter_location(it)
    box = view.get_line_yrange(it)

    end = it.copy()
    if not end.ends_line():
        end.forward_to_line_end()
    text = buffer.get_text(it, end, True)

    # Walk display lines to count wraps and to find where the first one
    # broke -- forward_display_line mutates the iter it is given, hence the
    # copy (see decorated_textview.py's docstring on the same trap).
    probe = it.copy()
    display_lines = 1
    first_break = None
    wrap_x = None
    display_ys = [text_rect.y]
    while view.forward_display_line(probe) and probe.get_line() == index:
        if first_break is None:
            first_break = probe.get_line_offset()
            wrap_x = view.get_iter_location(probe).x
        display_lines += 1
        display_ys.append(view.get_iter_location(probe).y)

    marker = re.match(r"(?:•|☐|☑|\d+\.)\s*", text)
    text_at = it.copy()
    text_at.forward_chars(marker.end() if marker else 0)

    return Line(
        index=index,
        text=text,
        x=text_rect.x,
        space_above=text_rect.y - box.y,
        text_height=text_rect.height,
        space_below=(box.y + box.height) - (text_rect.y + text_rect.height),
        line_height=box.height,
        display_lines=display_lines,
        first_line_chars=first_break if first_break is not None else len(text),
        display_ys=display_ys,
        text_x=view.get_iter_location(text_at).x,
        wrap_x=wrap_x,
        char_xs=_char_xs(view, it, first_break if first_break is not None else len(text)),
    )


def _char_xs(view, it, count):
    xs, probe = [], it.copy()
    for _ in range(count + 1):
        xs.append(view.get_iter_location(probe).x)
        if not probe.forward_char():
            break
    return xs


_cache = {}


def measure(markdown_text, width=WIDTH, zoom_factor=1.0):
    """Render `markdown_text` and measure it at `width` and `zoom_factor`.

    Presents a real window (see the module docstring for why nothing less
    will do), so this is the slow part of the suite -- results are cached
    per (document, width, zoom) since several tests read the same one.
    """
    key = (markdown_text, width, zoom_factor)
    if key in _cache:
        return _cache[key]

    tree = SyntaxTreeNode(_PARSER.parse(markdown_text))
    buffer = Gtk.TextBuffer(tag_table=create_tag_table(dark=False))
    MarkdownRenderer().render(tree, buffer)

    view = _build_view()
    view.set_buffer(buffer)
    window = Gtk.Window(default_width=width, default_height=HEIGHT)
    window.set_child(view)

    zoom = ZoomController(view)
    while zoom.factor < zoom_factor:
        zoom.zoom_in()
    while zoom.factor > zoom_factor:
        zoom.zoom_out()
    assert zoom.factor == pytest.approx(zoom_factor)

    window.present()
    expected_px = round(BASE_PX * zoom_factor, 1)
    assert _settle(view, expected_px), (
        f"CSS font-size never reached the layout: wanted {expected_px}px, "
        f"got {_font_px(view)}px. Every measurement below would be silently "
        f"wrong -- see this module's docstring, point 2.")

    # Force the width: with no window manager the presented window is not
    # the size it asked for (docstring, point 3). Twice, either side of the
    # drain: the view sets its own margins from the width it is allocated,
    # which queues a resize, and servicing that queue is what lets the
    # wrongly-sized toplevel allocate the view back down to its own idea of
    # the width. The second call has the last word, and needs no drain after
    # it -- every measurement below validates the layout on demand.
    view.allocate(width, HEIGHT, -1, None)
    _drain()
    view.allocate(width, HEIGHT, -1, None)

    layout = Layout(
        lines=[_line_geometry(view, buffer, n)
               for n in range(buffer.get_line_count())],
        font_px=_font_px(view),
        width=width,
        left_margin=view.get_left_margin(),
        right_margin=view.get_right_margin(),
    )
    assert_valid_geometry(layout)

    zoom.close()
    window.destroy()
    _cache[key] = layout
    return layout


def assert_valid_geometry(layout):
    """Fail loudly on the unallocated-view failure mode (docstring, point 1)
    instead of quietly reporting a perfectly-spaced document of zero-height
    lines."""
    assert layout.font_px > 0
    assert all(line.line_height > 0 for line in layout.lines), (
        "every line box measured zero height -- the view was never allocated, "
        f"so no vertical measurement in {layout.lines} means anything")


# One of every block construct, in the order a gap-model test wants them.
# Tables and hr are deliberately absent: they are Gtk.TextChildAnchor
# widgets, whose geometry is the widget's allocation rather than a line box,
# and need their own measurement path.
PROBE = """# Heading one

First paragraph of prose, written long enough that it certainly has to wrap \
at seven hundred pixels, which is what makes it usable for measuring the \
spacing between wrapped lines within a single block.

Second paragraph, following the first.

## Heading two

- level one item
- second level one item
  - level two item
    - level three item
  - another level two item

> a blockquote line

```python
fenced = "code"
```

Closing paragraph.
"""


def test_the_harness_reports_geometry_at_all():
    """The guard test: if this fails, every other assertion here is
    meaningless rather than wrong."""
    layout = measure(PROBE)
    assert layout.font_px == pytest.approx(BASE_PX)
    assert_valid_geometry(layout)
    assert layout.find("First paragraph").display_lines > 1, \
        "the probe paragraph must wrap for the wrapped-line tests to mean anything"


def test_base_font_is_pinned_to_the_same_em_on_every_machine():
    """Why em works: the CSS base is a fixed pixel size everywhere, even
    though the font *family* (and so every line height) is not."""
    layout = measure(PROBE)
    assert layout.font_px == pytest.approx(BASE_PX)


# --- the reference table --------------------------------------------------
#
# Expected values are derived from the constants rather than pasted as
# measured numbers, so a deliberate change to a constant updates the
# expectation and an accidental change to *layout* still fails.

def test_paragraph_after_paragraph_gap_matches_the_block_model():
    layout = measure(PROBE)
    expected = MarkdownRenderer._BLOCK_BOTTOM_MARGIN["paragraph"]
    line = layout.find("Second paragraph")
    assert layout.em(line.space_above) == pytest.approx(
        layout.em(expected), abs=TOL)


def test_heading_gets_the_larger_of_its_top_and_the_previous_bottom():
    layout = measure(PROBE)
    expected = max(MarkdownRenderer._BLOCK_TOP_MARGIN["heading"],
                   MarkdownRenderer._BLOCK_BOTTOM_MARGIN["paragraph"])
    line = layout.find("Heading two")
    assert layout.em(line.space_above) == pytest.approx(
        layout.em(expected), abs=TOL)


def test_h1_and_h2_reserve_room_below_for_their_rule():
    """decorated_textview.py paints the rule outside the text box; the space
    it needs has to be reserved by layout or the next block sits on it."""
    layout = measure(PROBE)
    expected = tagdefs.HEADING_RULE_PAD + tagdefs.HEADING_RULE_WIDTH
    for heading in ("Heading one", "Heading two"):
        line = layout.find(heading)
        assert layout.em(line.space_below) == pytest.approx(
            layout.em(expected), abs=TOL), heading


def test_fence_gap_includes_the_padding_its_panel_needs():
    """A fence's gap is the collapsed margin *plus* CODE_BLOCK_PADDING,
    which does not collapse -- the panel is drawn in that space."""
    layout = measure(PROBE)
    expected = (MarkdownRenderer._BLOCK_BOTTOM_MARGIN["blockquote"]
                + tagdefs.CODE_BLOCK_PADDING)
    line = layout.find('fenced = "code"')
    assert layout.em(line.space_above) == pytest.approx(
        layout.em(expected), abs=TOL)


def test_list_items_after_the_first_get_the_item_gap():
    layout = measure(PROBE)
    line = layout.find("second level one item")
    assert layout.em(line.space_above) == pytest.approx(
        layout.em(tagdefs.LIST_ITEM_GAP), abs=TOL)


# What a browser actually does with PROBE, rather than what its stylesheet
# says: Chromium + github-markdown-css v5 at a 16px base, measured with a
# Range over each text node (one client rect per rendered line) and divided
# by the font size. Regenerate by rendering PROBE through markdown-it into
# a .markdown-body div with that stylesheet and reading the rects back.
#
# Only the boundaries Lectern is expected to match are listed. The three
# left out are known divergences with causes of their own, each a separate
# piece of work: headings (GitHub gives h1-h6 line-height 1.25, Lectern
# gives them the font's own) and fences (font-size 85%, line-height 1.45).
GITHUB_PITCH_EM = {
    "Second paragraph": 2.5,        # paragraph to paragraph
    "second level one item": 1.75,  # item to sibling item
    "level two item": 1.5,          # item to its nested list's first item
    "level three item": 1.5,        # ... and one level deeper
    "another level two item": 1.75, # back out of a nested list
    "a blockquote line": 2.5,       # list to blockquote
}


def test_block_pitch_matches_the_browser():
    """The convergence test: everything else here asserts Lectern's own
    model, this asserts the model lands where GitHub lands."""
    layout = measure(PROBE)
    for probe, expected in GITHUB_PITCH_EM.items():
        assert layout.pitch_em(probe) == pytest.approx(expected, abs=TOL), probe


def test_a_nested_list_is_not_pushed_away_from_its_parent_item():
    """PROBE's lists are tight, so their items are not paragraphs and have
    no paragraph margin to collapse against: a nested list follows its
    parent closely, and never further off than one of its own siblings."""
    layout = measure(PROBE)
    opening = layout.find("level two item")
    sibling = layout.find("another level two item")
    assert layout.em(sibling.space_above) == pytest.approx(
        layout.em(tagdefs.LIST_ITEM_GAP), abs=TOL)
    assert layout.em(opening.space_above) <= layout.em(sibling.space_above) + TOL


# A blank line between the items is the whole difference from PROBE's list:
# it makes the list loose, so markdown-it wraps each item's content in a
# paragraph and the spacing should follow. Every boundary a loose list has
# is here -- into a nested list, back out of one, and plain item to item.
LOOSE_PROBE = """- loose item one

  - tight child of a loose item

- loose item after a nested list

- last loose item
"""


def test_a_loose_lists_pitch_matches_the_browser():
    """Written loose, the same list spaces out again -- uniformly, and
    including coming back *out* of a nested list. That one is the trap:
    the nested list owes nothing below itself, so the gap has to come from
    the following item's own top margin (GitHub's `li > p`)."""
    layout = measure(LOOSE_PROBE)
    for probe in ("tight child", "loose item after a nested list", "last loose item"):
        assert layout.pitch_em(probe) == pytest.approx(2.5, abs=TOL), probe


def test_list_indent_is_one_step_per_nesting_level():
    layout = measure(PROBE)
    xs = [layout.find(t).x for t in
          ("level one item", "level two item", "level three item")]
    steps = [b - a for a, b in zip(xs, xs[1:])]
    for step in steps:
        assert layout.em(step) == pytest.approx(
            layout.em(tagdefs.LIST_INDENT_STEP), abs=TOL)


def test_an_inline_code_chip_leaves_the_space_beside_it_showing():
    """The chip is painted INLINE_CODE_PAD_X out from its code, and with
    no room set aside for that it covered the spaces either side and met
    the letters beyond them. The room is made in the text now, so the
    visible gap each side of the chip is a whole space, as on GitHub."""
    layout = measure("Its `10.` should stand out.\n")
    line = layout.find("Its 10. should")
    xs = line.char_xs
    pad = tagdefs.INLINE_CODE_PAD_X
    code_start, code_end = line.text.index("10."), line.text.index("10.") + 3
    # The chip spans the code and `pad` either side of it.
    left_gap = (xs[code_start] - pad) - xs[code_start - 1]     # after "Its"
    right_gap = xs[code_end + 1] - (xs[code_end] + pad)        # before "should"
    plain = line.text.index(" stand")
    space = xs[plain + 1] - xs[plain]                          # an ordinary space
    assert layout.em(left_gap) == pytest.approx(layout.em(space), abs=TOL)
    assert layout.em(right_gap) == pytest.approx(layout.em(space), abs=TOL)
    assert layout.em(space) > 0.15


LIST_PROBE = """\
- A bullet item written long enough that it has to wrap at seven hundred \
pixels, so its wrapped lines can be compared with its first.

9. A ninth item written long enough that it has to wrap at seven hundred \
pixels, so its wrapped lines can be compared with its first.
10. A tenth item written long enough that it has to wrap at seven hundred \
pixels, so its wrapped lines can be compared with its first.

- [ ] A task item written long enough that it has to wrap at seven hundred \
pixels, so its wrapped lines can be compared with its first.
"""


@pytest.mark.parametrize("probe", ["A bullet item", "A ninth item", "A tenth item",
                                   "A task item"])
def test_list_item_text_starts_on_its_text_column(probe):
    """The marker hangs left of the item's text, right-aligned against it,
    so the text starts on the column its wrapped lines and continuation
    blocks use -- for "10." too, which used to push its text right (#43).
    Which way round the hang goes matters: a negative Pango indent leaves
    the *first* line alone and indents the ones after it."""
    layout = measure(LIST_PROBE)
    line = layout.find(probe)
    assert line.wrap_x is not None, "the probe item must wrap"
    assert layout.em(line.text_x) == pytest.approx(
        layout.em(tagdefs.list_text_column(0)), abs=TOL)
    assert layout.em(line.wrap_x) == pytest.approx(
        layout.em(tagdefs.list_text_column(0)), abs=TOL)


def test_a_two_digit_marker_reaches_left_of_the_marker_column():
    layout = measure(LIST_PROBE)
    assert layout.find("A tenth item").x < layout.find("A ninth item").x
    assert layout.find("A tenth item").x < tagdefs.list_marker_column(0)


def test_blockquote_is_indented_from_the_content_column():
    layout = measure(PROBE)
    line = layout.find("a blockquote line")
    assert layout.em(line.x) == pytest.approx(
        layout.em(tagdefs.BLOCKQUOTE_INDENT), abs=TOL)


def test_a_line_of_prose_is_one_line_height_tall():
    """A Gtk.TextView gives a line whatever height the font asks for, where
    CSS pads every line box out to line-height. tags.line_leading supplies
    the difference, so consecutive lines sit exactly LINE_HEIGHT apart --
    on any font, which is what makes this portable to CI."""
    layout = measure(PROBE)
    line = layout.find("First paragraph")
    assert line.display_lines > 1
    pitch = line.wrapped_line_spacing + line.text_height
    assert layout.em(pitch) == pytest.approx(tagdefs.LINE_HEIGHT, abs=TOL)


# --- zoom, and the capped measure -----------------------------------------
#
# These two started as strict-xfails describing issues #5 and #6. They are
# the acceptance criteria for both fixes, which is why they assert the
# *ratios* rather than any particular constant.

def test_zoom_preserves_every_em_ratio():
    at_100 = measure(PROBE, zoom_factor=1.0)
    at_200 = measure(PROBE, zoom_factor=2.0)
    assert at_200.font_px == pytest.approx(2 * at_100.font_px), \
        "the font itself must scale, or this tests nothing"

    for probe in ("Second paragraph", "Heading two", "second level one item",
                  "a blockquote line"):
        a, b = at_100.find(probe), at_200.find(probe)
        assert at_200.em(b.space_above) == pytest.approx(
            at_100.em(a.space_above), abs=TOL), f"{probe}: space above"

    a, b = at_100.find("level two item"), at_200.find("level two item")
    assert at_200.em(b.x) == pytest.approx(at_100.em(a.x), abs=TOL), \
        "list indent"


def test_text_column_is_capped_at_a_comfortable_measure():
    """Asserted in em, not characters. Characters-per-line is the meaningful
    complaint (45-90 is the usual comfortable range) but it is not portable:
    the same 1600px window fits 90 characters of DejaVu Sans and well over
    100 of the narrower Adwaita Sans, so a character threshold passes in CI
    and fails on a desktop. The em ceiling is pure geometry."""
    layout = measure(PROBE, width=1600)
    measured = layout.em(layout.text_column_px)
    chars = layout.find("First paragraph").first_line_chars
    assert measured <= REFERENCE_MEASURE_EM, (
        f"text column is {measured:.0f} em wide at {layout.width}px "
        f"({chars} characters of body text per line); comfortable is "
        f"<= {REFERENCE_MEASURE_EM} em")


def test_a_narrow_window_is_not_capped_at_all():
    """The cap only ever takes width away from a window that has more than
    the measure needs -- below that it must be inert, not a minimum."""
    layout = measure(PROBE, width=WIDTH)
    assert layout.left_margin == tagdefs.CONTENT_MARGIN
    assert layout.right_margin == tagdefs.CONTENT_MARGIN


def test_capping_the_measure_moves_the_whole_column_not_just_prose():
    """The trap in centring a Gtk.TextView's text: a Gtk.TextTag left-margin
    replaces the view's own rather than adding to it, so every indented
    block is positioned from the window edge and has to be moved along by
    the same amount."""
    narrow = measure(PROBE, width=WIDTH)
    wide = measure(PROBE, width=1600)
    shift = wide.left_margin - narrow.left_margin
    assert shift > 0, "the wide window must actually be capped for this to test anything"
    for probe in ("level one item", "level two item", "a blockquote line",
                  'fenced = "code"'):
        assert wide.find(probe).x - narrow.find(probe).x == pytest.approx(
            shift, abs=1), probe

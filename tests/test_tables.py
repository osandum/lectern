"""Table column widths: allocate_columns, and the screen and paper tables
that both lay out by it."""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk
import pytest

from markdown_it.tree import SyntaxTreeNode

from lectern.document import make_parser
from lectern import tables
from lectern import printing
from lectern.tags import tag_style_props

# The shape that showed the median weighting up: two columns of similar
# typical length, one with a few long cells. Split evenly, the long cells
# wrapped beside a column of timestamps with room to spare. Addresses are
# from the documentation ranges (RFC 5737).
LOGINS = """\
| Time | Address |
|---|---|
| 03.04 21:17:45 | 192.0.2.17 (first sign-in, right after the account was created) |
| 05.04 06:12:08 | 198.51.100.42 |
| 06.04 13:48:51 | 198.51.100.42 |
| 08.04 02:31:19 | 198.51.100.42 (signed in to the **second** account too, see below) |
| 08.04 15:05:33 | 203.0.113.9 (mobile, Android) |
| 11.04 10:44:27 | 192.0.2.18 |
"""

def test_everything_fits_so_every_column_gets_its_widest():
    widths = tables.allocate_columns([50, 100], [100, 400], 600)
    assert widths == pytest.approx([120, 480])  # 100 spare, shared 1:4


def test_a_column_at_its_widest_takes_none_of_the_room_left_over():
    widths = tables.allocate_columns([100, 100], [100, 500], 300)
    assert widths == pytest.approx([100, 200])


def test_room_above_the_narrowest_is_shared_evenly_among_those_still_short():
    widths = tables.allocate_columns([50, 50], [150, 450], 300)
    # 200 spare: 100 each, which the first needs exactly
    assert widths == pytest.approx([150, 150])
    widths = tables.allocate_columns([50, 50, 50], [80, 450, 650], 450)
    # 300 spare: the first takes the 30 it needs, the others 135 each
    assert widths == pytest.approx([80, 185, 185])


def test_too_narrow_for_even_the_longest_words_shrinks_in_proportion():
    assert tables.allocate_columns([100, 300], [200, 600], 200) == pytest.approx([50, 150])


def test_rounded_widths_add_up_to_the_rounded_total():
    widths = tables.round_widths([100.4, 100.4, 100.4])
    assert sum(widths) == 301
    assert all(isinstance(w, int) for w in widths)


def _table_node(markdown_text):
    tree = SyntaxTreeNode(make_parser().parse(markdown_text))
    return next(n for n in tree.walk() if n.type == "table")


def _laid_out_screen_table(markdown_text, width):
    frame, _rows, _links, cells = tables.build_table_widget(
        _table_node(markdown_text), "#1c71d8")
    grid = frame.get_child()
    frame.set_child(None)
    window = Gtk.Window(child=grid)
    window.present()
    _lo, _nat, _b1, _b2 = grid.measure(Gtk.Orientation.HORIZONTAL, -1)
    height = grid.measure(Gtk.Orientation.VERTICAL, width)[0]
    rect = Gdk.Rectangle()
    rect.width, rect.height = width, height
    grid.size_allocate(rect, -1)
    return cells


def _outer_width(label):
    return label.get_width() + 2 * tables.CELL_PAD_X


def _on_one_line(label):
    """Whether the label's text fits its allocated width unwrapped. Not
    get_height(): that is the row's height, two lines for a timestamp
    sitting beside a wrapped cell."""
    at_width = label.measure(Gtk.Orientation.VERTICAL, _outer_width(label))[0]
    unwrapped = label.measure(Gtk.Orientation.VERTICAL, -1)[0]
    return at_width == unwrapped


def test_screen_table_keeps_short_cells_on_one_line_and_gives_long_ones_the_room():
    cells = _laid_out_screen_table(LOGINS, 900)
    timestamps = [row[0].label for row in cells]
    ips = [row[1].label for row in cells]
    assert all(_on_one_line(label) for label in timestamps + ips)
    assert _outer_width(ips[0]) > 2 * _outer_width(timestamps[0])


def test_screen_table_columns_line_up_and_fill_the_width():
    cells = _laid_out_screen_table(LOGINS, 900)
    for col in (0, 1):
        assert len({_outer_width(row[col].label) for row in cells}) == 1
    first = cells[0]
    used = sum(_outer_width(c.label) for c in first) + tables.COLUMN_SPACING
    assert used == 900


def test_screen_table_wraps_the_long_column_first_when_squeezed():
    cells = _laid_out_screen_table(LOGINS, 420)
    assert all(_on_one_line(row[0].label) for row in cells)
    assert not _on_one_line(cells[1][1].label)


class _PdfContext:
    def __init__(self):
        import cairo
        self._cr = cairo.Context(cairo.PDFSurface(None, 600, 800))

    def create_pango_layout(self):
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import PangoCairo
        layout = PangoCairo.create_layout(self._cr)
        PangoCairo.context_set_resolution(layout.get_context(), 72)
        return layout


def test_printed_table_shares_the_width_the_same_way():
    rows = tables.extract_rows(_table_node(LOGINS))
    layouts, _heights, widths = printing._build_table_rows(
        _PdfContext(), tag_style_props(False), rows, 480)
    assert sum(widths) == pytest.approx(480)
    assert widths[1] > 2 * widths[0]
    assert all(row[0].get_line_count() == 1 for row in layouts)
    assert all(row[1].get_line_count() == 1 for row in layouts)

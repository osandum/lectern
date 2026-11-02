"""Markdown `table` subtree -> on-screen TableGrid, plus the plain row-text
extraction shared with the print model.
"""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk, GLib

# Each cell's padding, as label margins, and the rule between rows, a
# Gtk.Separator one pixel tall. Shared with printing.py, which draws the
# same rows on paper and used to pad them by its own, twice-as-big numbers.
CELL_PAD_X = 8
CELL_PAD_Y = 6
ROW_RULE_PX = 1


COLUMN_SPACING = 16


def allocate_columns(mins, maxes, available):
    """Column widths for a table `available` wide, from each column's
    narrowest possible width (its longest word) and its widest useful one
    (its longest cell, unwrapped) -- the auto table layout browsers use,
    so a column of short timestamps keeps them on one line and a column
    with one long note in it gets the room that note needs.

    Everything fits: each column gets its widest, and what's left over is
    shared in proportion to it. Not everything fits: each gets its
    narrowest, and the room above that is shared out evenly, except that
    a column needing less than its share to reach its widest gets just
    that, and the rest is shared again among the others -- GTK's own
    gtk_distribute_natural_allocation. Browsers share it in proportion to
    how much each column would still use instead, which wraps a column of
    timestamps for want of a few pixels beside a column of sentences that
    wraps anyway. Not even the narrowest fits: they shrink in proportion, which
    the screen never asks for (a table's minimum width is the sum of its
    columns', so it widens the view instead) but paper, which can't
    scroll, does.

    This replaced a weighting by each column's median length in
    characters, which shared the width out the same way whether or not a
    column needed it: two columns of similar typical length got half each,
    so the one with a few long cells wrapped them beside a column of
    timestamps with room to spare.
    """
    maxes = [max(lo, hi) for lo, hi in zip(mins, maxes)]
    total_min, total_max = sum(mins), sum(maxes)
    if not mins:
        return []
    if available >= total_max:
        if total_max <= 0:
            return [available / len(mins)] * len(mins)
        return [hi + (available - total_max) * hi / total_max for hi in maxes]
    if available <= total_min:
        if total_min <= 0:
            return [0.0] * len(mins)
        return [lo * available / total_min for lo in mins]
    widths = list(mins)
    room = available - total_min
    hungry = sorted(range(len(mins)), key=lambda i: maxes[i] - mins[i])
    for count, index in zip(range(len(hungry), 0, -1), hungry):
        share = min(maxes[index] - mins[index], room / count)
        widths[index] += share
        room -= share
    return widths


def round_widths(widths):
    """Whole pixels summing to the rounded total, so the last column doesn't
    end a pixel short of the frame or past it."""
    result, carried, placed = [], 0.0, 0
    for width in widths:
        carried += width
        result.append(round(carried) - placed)
        placed = round(carried)
    return result


def inline_plain_text(node):
    """Flatten an inline subtree to plain text. Written for table cells,
    but the same job comes up for an image's alt text (renderer.py) --
    markdown-it leaves an image node's `content` as the *raw* alt source,
    so `![a *b*](x)` would otherwise show its asterisks.
    """
    parts = []

    def walk(node):
        # "text" and "code_inline" are both leaf nodes whose content lives
        # in .content, not in .children -- code_inline has zero children,
        # so recursing into it (the fallback branch below) silently
        # produces nothing. This is exactly what made a cell like
        # `[`../app/foo/`](../app/foo/)` (a link whose display text is
        # itself inline code) render as a blank cell: the "link" node
        # correctly recursed into its "code_inline" child, but nothing
        # captured that child's own .content.
        if node.type in ("text", "code_inline"):
            parts.append(node.content)
        elif node.type == "softbreak":
            parts.append(" ")
        else:
            for child in node.children:
                walk(child)

    walk(node)
    return "".join(parts).strip()


def _iter_rows(table_node):
    """Yield list[cell_node] for every row (header row(s) first, GFM
    tables always have exactly one) -- the shared structural walk behind
    extract_rows and build_table_widget, so they can't disagree about
    what a "row" is."""
    for section in table_node.children:  # thead, tbody
        for tr in section.children:
            yield list(tr.children)


def extract_rows(table_node):
    """Return list[list[str]] of cell text, header row(s) included first."""
    return [[inline_plain_text(cell) for cell in cells] for cells in _iter_rows(table_node)]


# A cell's inline styles, by the tag name the body text uses for each --
# so print can style cell runs from tags.tag_style_props like a paragraph's
# -- and the Pango markup element the screen's labels spell it with.
_CELL_STYLES = {"strong": "strong", "em": "em", "s": "strike"}
_CELL_MARKUP = {"strong": "b", "em": "i", "strike": "s", "code-inline": "tt"}


def cell_runs(cell_node):
    """A cell's inline content as [(text, tag_names, href)] runs, href None
    outside a link. The one walk behind both the screen's label markup and
    the printed cell, so the two support the same styles: the handful
    renderer.py supports for body text that plausibly turn up in a table
    cell, not the full inline grammar.

    `code_inline` is a leaf with its text in .content and no children;
    recursing into it instead once made a cell like
    `[`../app/foo/`](../app/foo/)` render blank.
    """
    runs = []

    def walk(node, tags, href):
        t = node.type
        if t == "text":
            runs.append((node.content, tags, href))
        elif t == "code_inline":
            runs.append((node.content, tags + ["code-inline"], href))
        elif t == "softbreak":
            runs.append((" ", tags, href))
        elif t == "link":
            # Nested links aren't valid Markdown, so this can't nest --
            # simply adopt the innermost href, matching CommonMark's own
            # "link text can't itself contain a link" rule.
            for child in node.children:
                walk(child, tags + ["link"], node.attrs.get("href", ""))
        else:
            style = _CELL_STYLES.get(t)
            for child in node.children:
                walk(child, tags + [style] if style else tags, href)

    walk(cell_node, [], None)
    return runs


def _cell_markup(cell_node, link_color, highlights=()):
    """A cell's runs as Pango markup for an on-screen Gtk.Label -- links
    as clickable <a href> spans, so a cell like `[`../app/foo/`](../app/foo/)`
    is navigable, not just readable.

    `highlights` is find's `[(start, end, color)]` over the cell's plain
    text, baked straight into the markup -- see TableCell.highlight for
    why they can't just be Pango attributes layered on afterward. Each run
    opens and closes its own elements, so a match straddling a style
    boundary just yields one span each side of it instead of a span
    crossing a tag.
    """
    parts = []
    emitted = 0  # plain-text characters emitted so far, to index highlights by

    def emit(text):
        nonlocal emitted
        start, end = emitted, emitted + len(text)
        emitted = end
        cursor = start
        for hl_start, hl_end, color in highlights:
            lo, hi = max(hl_start, cursor), min(hl_end, end)
            if lo >= hi:
                continue
            parts.append(GLib.markup_escape_text(text[cursor - start:lo - start]))
            parts.append(f'<span background="{color}">')
            parts.append(GLib.markup_escape_text(text[lo - start:hi - start]))
            parts.append("</span>")
            cursor = hi
        parts.append(GLib.markup_escape_text(text[cursor - start:]))

    for text, tags, href in cell_runs(cell_node):
        elements = [_CELL_MARKUP[tag] for tag in tags if tag in _CELL_MARKUP]
        if href:
            # Color applied here, not via the shared "link" TextTag --
            # table cells are separate Gtk.Label widgets outside that
            # tag table (see tags.link_color_hex's docstring).
            escaped = GLib.markup_escape_text(href)
            parts.append(f'<a href="{escaped}"><span foreground="{link_color}">')
        parts.extend(f"<{e}>" for e in elements)
        emit(text)
        parts.extend(f"</{e}>" for e in reversed(elements))
        if href:
            parts.append("</span></a>")
    return "".join(parts)


def extract_cell_runs(table_node):
    """cell_runs for every cell, [row][col], header row first -- what print
    lays a table's cells out from."""
    return [[cell_runs(cell) for cell in cells] for cells in _iter_rows(table_node)]


class TableCell:
    """One rendered cell, kept re-renderable so find can highlight it.

    Highlights are re-rendered into the markup rather than layered on as
    Pango attributes, because Gtk.Label.set_attributes *merges* into the
    label's cached PangoLayout instead of replacing its attribute list:
    shrinking the highlight set (backspacing in the find bar, or clearing
    it) leaves the dropped ranges painted, and set_attributes(None)
    doesn't remove them either. Re-setting the markup makes GTK rebuild
    the layout outright, which is the only reliable way back to a clean
    label.
    """
    __slots__ = ("label", "_node", "_link_color", "_heading")

    def __init__(self, label, node, link_color, heading):
        self.label = label
        self._node = node
        self._link_color = link_color
        self._heading = heading

    @property
    def text(self):
        """The cell's plain text -- what find searches, and what the
        offsets passed to highlight() index into."""
        return self.label.get_text()

    def highlight(self, ranges):
        """`ranges` is [(start, end, color)] over `text`, in order and
        non-overlapping. Pass an empty list to go back to no highlight."""
        markup = _cell_markup(self._node, self._link_color, ranges)
        self.label.set_markup(f"<b>{markup}</b>" if self._heading else markup)


def build_table_widget(table_node, link_color):
    """Return (widget_to_embed, rows, link_labels, cells) -- rows is
    reused verbatim for print; link_labels are the Gtk.Labels whose markup
    contains a clickable link, for the caller to connect "activate-link"
    on (see window.py) -- table cell clicks don't route through the
    buffer's tag-based dispatch_targets mechanism at all, since this
    content lives in embedded widgets, not the TextBuffer. cells is a
    TableCell per cell, in the same [row][col] shape as rows, so
    findbar.py can search and highlight this widget-based text too.
    """
    cell_rows = list(_iter_rows(table_node))
    rows = [[inline_plain_text(cell) for cell in cells] for cells in cell_rows]
    # Thin horizontal rules between rows (header included) rather than a
    # full per-cell grid -- a per-cell Gtk.Frame was tried and rejected:
    # Libadwaita renders each Frame as its own rounded card, which reads
    # as a row of disconnected pills rather than a table. Rules-only
    # matches how GitHub/pandoc render GFM tables in practice.
    link_labels = []
    cell_grid = []
    rules = []
    for row_index, cells in enumerate(cell_rows):
        is_head = row_index == 0
        cell_row = []
        for col_index, cell_node in enumerate(cells):
            label = Gtk.Label(
                xalign=0.0, wrap=True, selectable=True,
                margin_top=CELL_PAD_Y, margin_bottom=CELL_PAD_Y,
                margin_start=CELL_PAD_X, margin_end=CELL_PAD_X,
            )
            # Table text lives in these labels, not in the TextBuffer, so
            # the TextView's own click-drag selection can't reach it --
            # without `selectable` a drag inside a cell selects nothing at
            # all. Selection is per-label (GTK has no way to carry one
            # selection across sibling widgets), so a drag covers a single
            # cell, and Ctrl+C copies it because GtkLabel binds that itself.
            #
            # That binding is why the focusability `selectable` turns on is
            # left alone, even though it puts every cell in the Tab chain:
            # a label with can-focus or focusable cleared refuses
            # grab_focus() outright (measured, GTK 4.18), so the click that
            # starts a selection can't move the keyboard focus onto it and
            # Ctrl+C keeps going to whatever still holds focus.
            markup = _cell_markup(cell_node, link_color)
            label.set_markup(f"<b>{markup}</b>" if is_head else markup)
            if "<a href" in markup:
                link_labels.append(label)
            cell_row.append(TableCell(label, cell_node, link_color, is_head))
        cell_grid.append(cell_row)
        if row_index < len(cell_rows) - 1:
            rules.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

    frame = Gtk.Frame()
    frame.set_child(TableGrid([[c.label for c in row] for row in cell_grid], rules))
    frame.set_margin_top(6)
    frame.set_margin_bottom(6)
    return frame, rows, link_labels, cell_grid


class _TableLayout(Gtk.LayoutManager):
    """Lays the cells out with allocate_columns, at allocation time.

    Gtk.Grid can't be told how to share width between columns: it hands
    each its widest cell's minimum, then splits what's left evenly among
    the expanding ones, which is the even split allocate_columns exists to
    avoid. Doing it here rather than by pushing size requests onto the
    labels means every measurement is taken with the font the cells are
    drawn in -- a zoom step restyles them a frame after the width sync
    that would otherwise have read the old one.
    """

    def __init__(self, cells, rules):
        super().__init__()
        self._cells = cells
        self._rules = rules
        self._ncols = max((len(row) for row in cells), default=0)

    def do_get_request_mode(self, _widget):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def _extents(self):
        mins, maxes = [0] * self._ncols, [0] * self._ncols
        for row in self._cells:
            for col, label in enumerate(row):
                lo, hi, _b1, _b2 = label.measure(Gtk.Orientation.HORIZONTAL, -1)
                mins[col], maxes[col] = max(mins[col], lo), max(maxes[col], hi)
        return mins, maxes

    def _spacing(self):
        return COLUMN_SPACING * max(self._ncols - 1, 0)

    def column_widths(self, width):
        mins, maxes = self._extents()
        available = max(width - self._spacing(), sum(mins))
        return round_widths(allocate_columns(mins, maxes, available))

    def _row_heights(self, widths):
        return [
            max((label.measure(Gtk.Orientation.VERTICAL, widths[col])[0]
                 for col, label in enumerate(row)), default=0)
            for row in self._cells
        ]

    def _rule_height(self, rule, width):
        return rule.measure(Gtk.Orientation.VERTICAL, width)[0]

    def do_measure(self, _widget, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            mins, maxes = self._extents()
            return sum(mins) + self._spacing(), sum(maxes) + self._spacing(), -1, -1
        if for_size < 0:
            for_size = sum(self._extents()[1]) + self._spacing()
        height = sum(self._row_heights(self.column_widths(for_size)))
        height += sum(self._rule_height(rule, for_size) for rule in self._rules)
        return height, height, -1, -1

    def do_allocate(self, _widget, width, _height, _baseline):
        widths = self.column_widths(width)
        y = 0
        for index, (row, row_height) in enumerate(zip(self._cells, self._row_heights(widths))):
            x = 0
            for col, label in enumerate(row):
                label.size_allocate(_rect(x, y, widths[col], row_height), -1)
                x += widths[col] + COLUMN_SPACING
            y += row_height
            if index < len(self._rules):
                rule = self._rules[index]
                rule_height = self._rule_height(rule, width)
                rule.size_allocate(_rect(0, y, width, rule_height), -1)
                y += rule_height


def _rect(x, y, width, height):
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = x, y, width, height
    return rect


class TableGrid(Gtk.Widget):
    """The table's cells and the rules between its rows, laid out by
    _TableLayout. `cells` is [row][col] of labels."""

    def __init__(self, cells, rules):
        super().__init__()
        for index, row in enumerate(cells):
            for label in row:
                label.set_parent(self)
            if index < len(rules):
                rules[index].set_parent(self)
        self.set_layout_manager(_TableLayout(cells, rules))

    def do_dispose(self):
        child = self.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            child.unparent()
            child = following
        Gtk.Widget.do_dispose(self)

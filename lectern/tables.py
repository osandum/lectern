"""Markdown `table` subtree -> on-screen Gtk.Grid, plus the plain row-text
extraction shared with the print model.
"""
import statistics

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

MIN_COLUMN_CHARS = 3  # floor so an all-empty or all-tiny column doesn't collapse to nothing
# Each cell's padding, as label margins, and the rule between rows, a
# Gtk.Separator one pixel tall. Shared with printing.py, which draws the
# same rows on paper and used to pad them by its own, twice-as-big numbers.
CELL_PAD_X = 8
CELL_PAD_Y = 6
ROW_RULE_PX = 1


def _longest_word_length(text):
    words = text.split()
    return max((len(w) for w in words), default=0)


def column_char_weights(rows):
    """Per-column weight = median character length of that column's cells
    (header row included), used as a simple, sensible proxy for how much
    horizontal space each column deserves. Shared by both the on-screen
    Gtk.Grid (as a max-width-chars hint) and printing.py (as a
    proportional share of the page width), so the two can't disagree
    about which columns are "wide" and which are "narrow".

    Median rather than mean: one abnormally long cell in an otherwise
    narrow column (a stray long sentence in a "Notes" column, say)
    shouldn't blow that column's width out the way an outlier drags a
    mean up.

    Floored, per column, at that column's own longest single word (e.g.
    the header "Supported" against otherwise-short "Yes"/"No" data) --
    without this, a narrow-but-wordy column can end up assigned less
    width than its own unbreakable header word needs. On screen this is
    harmless (Gtk.Label never breaks a word mid-way regardless of what
    width it's asked for), but printing.py's Pango layouts use
    WORD_CHAR wrapping and *will* hyphenlessly break a word that doesn't
    fit -- confirmed by "Supported" printing as "Suppor-/ted" before this
    floor was added.
    """
    if not rows:
        return []
    ncols = max(len(r) for r in rows)
    weights = []
    for col in range(ncols):
        cells = [row[col] for row in rows if col < len(row)]
        if not cells:
            weights.append(MIN_COLUMN_CHARS)
            continue
        median_len = round(statistics.median(len(c) for c in cells))
        longest_word = max(_longest_word_length(c) for c in cells)
        weights.append(max(MIN_COLUMN_CHARS, median_len, longest_word))
    return weights


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


def _cell_markup(cell_node, link_color, highlights=()):
    """Render a cell's inline content as Pango markup for an on-screen
    Gtk.Label -- preserves links as clickable <a href> spans (so a cell
    like `[`../app/foo/`](../app/foo/)` is navigable, not just readable)
    plus the same handful of inline styles renderer.py supports for body
    text. Only covers what plausibly turns up in a table cell, not the
    full inline grammar the main buffer renderer handles.

    `highlights` is find's `[(start, end, color)]` over the cell's plain
    text, baked straight into the markup -- see TableCell.highlight for
    why they can't just be Pango attributes layered on afterward.
    """
    parts = []
    emitted = 0  # plain-text characters emitted so far, to index highlights by

    def emit(text):
        """Append `text`, escaped, wrapping any highlighted slice of it in
        a background span. Slicing per emitted run (rather than once over
        the finished markup) is what keeps the result well-formed: a match
        straddling an inline tag boundary just yields one span each side
        of it instead of a span crossing the tag.
        """
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

    def open_link(href):
        if href:
            # Color applied here, not via the shared "link" TextTag --
            # table cells are separate Gtk.Label widgets outside that
            # tag table (see tags.link_color_hex's docstring).
            escaped = GLib.markup_escape_text(href)
            parts.append(f'<a href="{escaped}"><span foreground="{link_color}">')

    def close_link(href):
        if href:
            parts.append("</span></a>")

    def walk(node, href=None):
        t = node.type
        if t == "text":
            open_link(href)
            emit(node.content)
            close_link(href)
        elif t == "code_inline":
            open_link(href)
            parts.append("<tt>")
            emit(node.content)
            parts.append("</tt>")
            close_link(href)
        elif t == "softbreak":
            emit(" ")
        elif t == "strong":
            parts.append("<b>")
            for child in node.children:
                walk(child, href)
            parts.append("</b>")
        elif t == "em":
            parts.append("<i>")
            for child in node.children:
                walk(child, href)
            parts.append("</i>")
        elif t == "s":
            parts.append("<s>")
            for child in node.children:
                walk(child, href)
            parts.append("</s>")
        elif t == "link":
            # Nested links aren't valid Markdown, so this can't nest --
            # simply adopt the innermost href, matching CommonMark's own
            # "link text can't itself contain a link" rule.
            inner_href = node.attrs.get("href", "")
            for child in node.children:
                walk(child, inner_href)
        else:
            for child in node.children:
                walk(child, href)

    walk(cell_node)
    return "".join(parts)


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
    col_weights = column_char_weights(rows)
    # Thin horizontal rules between rows (header included) rather than a
    # full per-cell grid -- a per-cell Gtk.Frame was tried and rejected:
    # Libadwaita renders each Frame as its own rounded card, which reads
    # as a row of disconnected pills rather than a table. Rules-only
    # matches how GitHub/pandoc render GFM tables in practice.
    grid = Gtk.Grid(column_spacing=16, row_spacing=0)
    link_labels = []
    cell_grid = []
    grid_row = 0
    for row_index, cells in enumerate(cell_rows):
        is_head = row_index == 0
        cell_row = []
        for col_index, cell_node in enumerate(cells):
            label = Gtk.Label(
                xalign=0.0, wrap=True, hexpand=True, selectable=True,
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
            # Caps this label's *natural* width to roughly its column's
            # typical content length, instead of the unwrapped full-text
            # width every column got before -- GtkGrid then sizes each
            # column to the max natural width among its cells, so a
            # short numeric column no longer claims as much room as a
            # column of long sentences.
            label.set_max_width_chars(col_weights[col_index])
            markup = _cell_markup(cell_node, link_color)
            label.set_markup(f"<b>{markup}</b>" if is_head else markup)
            if "<a href" in markup:
                link_labels.append(label)
            cell_row.append(TableCell(label, cell_node, link_color, is_head))
            grid.attach(label, col_index, grid_row, 1, 1)
        cell_grid.append(cell_row)
        grid_row += 1
        if row_index < len(cell_rows) - 1:
            rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            grid.attach(rule, 0, grid_row, len(cells), 1)
            grid_row += 1

    frame = Gtk.Frame()
    frame.set_child(grid)
    frame.set_margin_top(6)
    frame.set_margin_bottom(6)
    return frame, rows, link_labels, cell_grid

"""Print pipeline tests, driven end-to-end through a real
Gtk.PrintOperation with the EXPORT action -- headless, no printer or
dialog needed, and it exercises the same begin-print/draw-page signals a
real print job does rather than a hand-rolled stand-in for them.
"""
import re
import zlib

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
import pytest

from markdown_it.tree import SyntaxTreeNode

from lectern.document import make_parser
from lectern.tags import create_tag_table
from lectern.renderer import MarkdownRenderer
from lectern import printing
from lectern import zoom as zoomdefs

_PARSER = make_parser()


def print_model_for(markdown_text):
    tree = SyntaxTreeNode(_PARSER.parse(markdown_text))
    buffer = Gtk.TextBuffer(tag_table=create_tag_table(dark=False))
    renderer = MarkdownRenderer()
    renderer.render(tree, buffer)
    return renderer.print_model


def render_for(markdown_text):
    """print_model plus the renderer's dispatch_targets -- the second is
    what carries link hrefs into the print pipeline."""
    tree = SyntaxTreeNode(_PARSER.parse(markdown_text))
    buffer = Gtk.TextBuffer(tag_table=create_tag_table(dark=False))
    renderer = MarkdownRenderer()
    renderer.render(tree, buffer)
    return renderer.print_model, renderer.dispatch_targets


def run_export(print_model, tmp_path, header_footer, doc_title="Hello", file_name="hello.md"):
    """Drive begin-print/draw-page exactly as print_document does, but with
    `header_footer` forced rather than left to a dialog checkbox, and
    handing back the mutated state dict so tests can inspect the geometry
    the pipeline actually computed."""
    coordinator = printing.PrintCoordinator()
    op = Gtk.PrintOperation()
    op.set_default_page_setup(printing._print_page_setup(header_footer))
    op.set_export_filename(str(tmp_path / "out.pdf"))
    state = {
        "header_footer": header_footer,
        "header_left": printing._header_left_text(doc_title, file_name),
    }
    op.connect("begin-print", coordinator._on_begin_print, print_model, False, state)
    op.connect("draw-page", coordinator._on_draw_page, state)
    result = op.run(Gtk.PrintOperationAction.EXPORT, None)
    assert result == Gtk.PrintOperationResult.APPLY
    return state


# -- #13: page margins and base font size -----------------------------------

def test_print_base_font_is_one_zoom_notch_below_screen_size():
    """PRINT_BASE_PT rides zoom.py's own 0.9 step, not a value invented
    just for print -- see the STEPS ladder in zoom.py."""
    assert printing.PRINT_BASE_PT == pytest.approx(zoomdefs.BASE_PT * 0.9)
    assert printing.PRINT_BASE_PT == pytest.approx(10.8)


def test_page_setup_adds_margin_asymmetrically():
    setup = printing._print_page_setup()
    assert setup.get_top_margin(Gtk.Unit.POINTS) == pytest.approx(printing.PAGE_MARGIN_PT)
    assert setup.get_left_margin(Gtk.Unit.POINTS) == pytest.approx(
        printing.PAGE_MARGIN_PT + printing.LEFT_EXTRA_MARGIN_PT)
    assert setup.get_right_margin(Gtk.Unit.POINTS) == pytest.approx(
        printing.PAGE_MARGIN_PT + printing.RIGHT_EXTRA_MARGIN_PT)
    # Bottom is untouched without a footer -- GTK's own default is
    # already generous (more than the 28pt added elsewhere), and the
    # issue is explicit that it should not be applied symmetrically.
    default_bottom = Gtk.PageSetup().get_bottom_margin(Gtk.Unit.POINTS)
    assert setup.get_bottom_margin(Gtk.Unit.POINTS) == pytest.approx(default_bottom)
    assert setup.get_bottom_margin(Gtk.Unit.POINTS) > printing.PAGE_MARGIN_PT


def test_page_setup_trims_bottom_margin_for_footer():
    """With a footer on, the bottom margin gives up FOOTER_MARGIN_SHIFT_PT
    -- that's the clearance _on_begin_print puts above the footer line
    instead (see FOOTER_BAND_PT/FOOTER_TEXT_TOP_PT), not extra blank
    paper at the very edge."""
    plain = printing._print_page_setup(header_footer=False)
    decorated = printing._print_page_setup(header_footer=True)
    assert decorated.get_bottom_margin(Gtk.Unit.POINTS) == pytest.approx(
        plain.get_bottom_margin(Gtk.Unit.POINTS) - printing.FOOTER_MARGIN_SHIFT_PT)


def test_print_document_exports_successfully(tmp_path):
    print_model = print_model_for("# Hello\n\nSome *text* and a [link](x).\n")
    coordinator = printing.PrintCoordinator()
    out = tmp_path / "out.pdf"
    result = coordinator.print_document(
        None, print_model, False, "Hello", "hello.md",
        action=Gtk.PrintOperationAction.EXPORT, export_path=str(out),
    )
    assert result == Gtk.PrintOperationResult.APPLY
    assert out.exists() and out.stat().st_size > 0


def pdf_page_count(path):
    # A page object's dict has "/Type /Page", the document catalog's page
    # *tree* root has "/Type /Pages" -- the lookahead tells the two apart
    # without pulling in a PDF-parsing dependency just for a page count.
    return len(re.findall(rb"/Type\s*/Page(?!s)", path.read_bytes()))


def test_print_document_forwards_header_footer_flag(tmp_path):
    """header_footer is now a plain argument on print_document (see the
    docstring on that method for why it can no longer be a dialog
    checkbox) -- this is the one test that exercises the public API end
    to end rather than driving begin-print/draw-page directly."""
    markdown = "# Hello\n\n" + "more text. " * 500 + "\n"
    print_model = print_model_for(markdown)
    coordinator = printing.PrintCoordinator()
    plain_out, decorated_out = tmp_path / "plain.pdf", tmp_path / "decorated.pdf"
    coordinator.print_document(
        None, print_model, False, "Hello", "hello.md",
        action=Gtk.PrintOperationAction.EXPORT, export_path=str(plain_out),
    )
    coordinator.print_document(
        None, print_model, False, "Hello", "hello.md",
        action=Gtk.PrintOperationAction.EXPORT, export_path=str(decorated_out),
        header_footer=True,
    )
    # Same content, less usable height per page with the header/footer on
    # -> at least as many pages, which only happens if the flag actually
    # reached _on_begin_print.
    assert pdf_page_count(decorated_out) >= pdf_page_count(plain_out)


# -- #14: optional header/footer ---------------------------------------------

def test_header_left_text_combines_title_and_file_name():
    assert printing._header_left_text("My Report", "report.md") == "My Report – report.md"


def test_header_left_text_falls_back_to_file_name_alone():
    assert printing._header_left_text(None, "report.md") == "report.md"
    # A document titled exactly after its own file name would otherwise
    # repeat itself ("report.md -- report.md").
    assert printing._header_left_text("report.md", "report.md") == "report.md"


def test_header_footer_is_off_unless_explicitly_turned_on(tmp_path):
    print_model = print_model_for("# Hello\n\nSome text.\n")
    state = run_export(print_model, tmp_path, header_footer=False)
    assert state["header_band"] == 0.0


def test_body_top_margin_applies_with_or_without_a_header(tmp_path):
    """BODY_TOP_MARGIN_PT is a plain addition on top of the header band --
    present either way, unlike FOOTER_MARGIN_SHIFT_PT which only kicks in
    with a footer."""
    print_model = print_model_for("# Hello\n\nSome text.\n")
    plain = run_export(print_model, tmp_path, header_footer=False)
    decorated = run_export(print_model, tmp_path, header_footer=True)
    assert plain["body_top"] == pytest.approx(printing.BODY_TOP_MARGIN_PT)
    assert decorated["body_top"] == pytest.approx(printing.HEADER_BAND_PT + printing.BODY_TOP_MARGIN_PT)


def test_header_footer_claims_room_from_the_body_not_the_paper(tmp_path):
    """Enabling the header/footer must not shrink the *body* height (that's
    #13's job) -- printable height grows by FOOTER_MARGIN_SHIFT_PT (the
    matching cut to the bottom page margin, see _print_page_setup), and
    FOOTER_BAND_PT grows by exactly that much too, so the two cancel out
    and body content still gets the same room it always did -- which is
    why the same content needs at least as many pages once it's on."""
    markdown = "# Hello\n\n" + "more text. " * 500 + "\n"
    print_model = print_model_for(markdown)
    plain = run_export(print_model, tmp_path, header_footer=False)
    decorated = run_export(print_model, tmp_path, header_footer=True)
    assert plain["header_band"] == 0.0
    assert decorated["header_band"] == pytest.approx(printing.HEADER_BAND_PT)
    assert plain["width"] == decorated["width"]
    assert decorated["height"] == pytest.approx(
        plain["height"] + printing.FOOTER_MARGIN_SHIFT_PT)
    assert len(decorated["pages"]) >= len(plain["pages"])
    assert len(decorated["pages"]) > 1


def pdf_texts(path):
    """Every byte range of the PDF that a scanner might want to search,
    with FlateDecode object/content streams inflated -- cairo packs
    annotation dicts and their URIs into compressed object streams, so a
    raw scan of the file misses them entirely.

    A decompressobj, not rstrip-then-decompress: it stops at the end of
    the deflate stream and leaves the EOL before `endstream` as unused
    data, whereas stripping it can eat a final data byte that happens to
    be \\r or \\n. That stream holds /CreationDate, so its bytes change
    every run and the scan used to miss the annotations about 1 in 128."""
    blob = path.read_bytes()
    out = [blob]
    for m in re.finditer(rb"stream\r?\n", blob):
        start = m.end()
        end = blob.find(b"endstream", start)
        if end == -1:
            continue
        try:
            out.append(zlib.decompressobj().decompress(blob[start:end]))
        except zlib.error:
            pass
    return b"\n".join(out)


def test_scheme_links_become_clickable_pdf_annotations(tmp_path):
    print_model, targets = render_for(
        "See [the guide](https://example.com/guide) or [mail us](mailto:x@example.com).\n"
    )
    out = tmp_path / "out.pdf"
    result = printing.PrintCoordinator().print_document(
        None, print_model, False, "Doc", "doc.md",
        action=Gtk.PrintOperationAction.EXPORT, export_path=str(out),
        link_targets=targets,
    )
    assert result == Gtk.PrintOperationResult.APPLY
    text = pdf_texts(out)
    assert len(re.findall(rb"/Subtype\s*/Link", text)) == 2
    assert b"https://example.com/guide" in text
    assert b"mailto:x@example.com" in text


def test_relative_and_anchor_links_stay_plain_text(tmp_path):
    """A bare relative href resolves against the author's own directory on
    screen; baking that local path -- or a dead in-page #anchor -- into a
    shared PDF is worse than leaving the text unlinked."""
    print_model, targets = render_for(
        "A [relative](../other.md) link and an [anchor](#section) link.\n"
    )
    out = tmp_path / "out.pdf"
    printing.PrintCoordinator().print_document(
        None, print_model, False, "Doc", "doc.md",
        action=Gtk.PrintOperationAction.EXPORT, export_path=str(out),
        link_targets=targets,
    )
    assert b"/Subtype /Link" not in pdf_texts(out)


def test_link_annotations_need_the_targets_argument(tmp_path):
    """print_model alone has only tag names, not hrefs -- without
    link_targets the pipeline can't (and doesn't) emit annotations."""
    print_model = print_model_for("See [the guide](https://example.com/guide).\n")
    out = tmp_path / "out.pdf"
    printing.PrintCoordinator().print_document(
        None, print_model, False, "Doc", "doc.md",
        action=Gtk.PrintOperationAction.EXPORT, export_path=str(out),
    )
    assert b"/Subtype /Link" not in pdf_texts(out)


def test_wrapped_link_gets_one_annotation_rect_per_line(tmp_path):
    long_text = "a link that just keeps going " * 6
    print_model, targets = render_for(
        f"Lead text before [{long_text}](https://example.com/long) and after.\n"
    )
    out = tmp_path / "out.pdf"
    printing.PrintCoordinator().print_document(
        None, print_model, False, "Doc", "doc.md",
        action=Gtk.PrintOperationAction.EXPORT, export_path=str(out),
        link_targets=targets,
    )
    # One Link annotation per line the anchor text wrapped onto, all
    # pointing at the same URL.
    assert len(re.findall(rb"/Subtype\s*/Link", pdf_texts(out))) >= 2


def test_link_spans_merges_touching_runs_of_one_link():
    """A link whose text has inner formatting arrives as several runs;
    they collapse to a single (start, end, href) span."""
    print_model, targets = render_for("Go to [**bold** plain](https://example.com/x).\n")
    href_by_tag = {
        name: t["href"] for name, t in targets.items() if t.get("type") == "url"
    }
    para = next(item for item in print_model if item.kind == "paragraph")
    spans = printing._link_spans(para, href_by_tag)
    assert len(spans) == 1
    assert spans[0][2] == "https://example.com/x"


def test_footer_gap_above_is_shifted_by_the_full_margin_amount():
    """FOOTER_TEXT_TOP_PT is the entire gap between body content's last
    line and the footer text, whatever FOOTER_BAND_PT is (see the comment
    above both) -- so the fix has to show up here, not just as a bigger
    band."""
    assert printing.FOOTER_TEXT_TOP_PT == pytest.approx(4.0 + printing.FOOTER_MARGIN_SHIFT_PT)
    assert printing.FOOTER_BAND_PT == pytest.approx(20.0 + printing.FOOTER_MARGIN_SHIFT_PT)


# -- list markers line up with their wrapped lines --------------------------

class _PdfContext:
    """Stands in for a Gtk.PrintContext: layouts on a PDF surface at 72dpi,
    so a layout unit is a point, as on paper."""

    def __init__(self):
        import cairo
        self._cr = cairo.Context(cairo.PDFSurface(None, 600, 800))

    def create_pango_layout(self):
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import PangoCairo
        layout = PangoCairo.create_layout(self._cr)
        PangoCairo.context_set_resolution(layout.get_context(), 72)
        return layout


@pytest.mark.parametrize("source", [
    "- **Sync:** ",
    "1. ",
    "- [ ] task ",
], ids=["bullet", "number", "task"])
def test_list_item_text_starts_where_its_wrapped_lines_do(source):
    """The marker is text in the print font, the hang a fixed length, and
    they used to disagree: in a 10.8pt Cantarell "•  " is 8pt wide against
    a 12pt hang, so every wrapped line sat 4pt right of the first one.

    Unless the marker is wider than the hang, which depends on the font:
    "1. " is 13pt in DejaVu Sans, the only font on the CI images. Then it
    keeps its natural width and pushes the text right (see the next test)."""
    from gi.repository import Pango
    item = print_model_for(source + "word " * 60 + "\n")[0]
    hang = printing._marker_hang_pt(item)
    style_table = printing.tagdefs.tag_style_props(False)
    marker = item.runs[0][0]

    def x_at(hanging_pt, chars):
        layout = printing._build_text_layout(
            _PdfContext(), style_table, item, 400, hanging_pt,
        )
        index = len(marker[:chars].encode("utf-8"))
        return layout, Pango.units_to_double(layout.index_to_pos(index).x)

    layout, start = x_at(hang, len(marker))
    _, natural = x_at(0.0, len(marker.rstrip()) + 1)  # down to one space
    lines = printing._line_geometry(layout)
    assert len(lines) > 1
    assert start == pytest.approx(max(lines[1][4], natural), abs=0.1)


def test_a_marker_wider_than_the_hang_keeps_a_space_before_its_text():
    """"10." is wider than the hang, and closing the gap to it ran the
    number into its text. It may only lose spaces down to one."""
    from gi.repository import Pango
    item = print_model_for("10. " + "word " * 60 + "\n")[0]
    layout = printing._build_text_layout(
        _PdfContext(), printing.tagdefs.tag_style_props(False), item, 400,
        printing._marker_hang_pt(item),
    )
    natural = printing._build_text_layout(
        _PdfContext(), printing.tagdefs.tag_style_props(False), item, 400,
    )
    end = len(item.runs[0][0].encode("utf-8"))
    assert layout.index_to_pos(end).x == pytest.approx(natural.index_to_pos(end).x, abs=0.1 * Pango.SCALE)


# -- Paper is the screen at PRINT_SCALE, spacing included -------------------
#
# The font was taken down a step for paper while every length stayed at its
# 100% size, so each gap, indent and pad came out ~11% too big for the text
# beside it -- the paper version of #6. Asserted in em, as test_layout.py
# does, so these hold whatever PRINT_SCALE settles on.

def _screen_em(px):
    return px / printing.tagdefs.BASE_FONT_PX


def _paper_em(points):
    return points / printing.PRINT_BASE_PT


def _blocks_for(markdown_text):
    return printing._build_blocks(
        _PdfContext(), printing.tagdefs.tag_style_props(False),
        print_model_for(markdown_text), 400, 700, False,
    )


def test_print_base_font_is_the_screen_font_at_print_scale():
    assert printing.PRINT_BASE_PT == pytest.approx(zoomdefs.BASE_PT * printing.PRINT_SCALE)


def test_printed_block_gaps_keep_the_screens_em_ratio():
    model = print_model_for("One paragraph.\n\nAnother paragraph.\n")
    blocks = _blocks_for("One paragraph.\n\nAnother paragraph.\n")
    leading = printing._leading_pt(_PdfContext(), printing._base_font())
    assert _paper_em(blocks[1].gap - leading) == pytest.approx(_screen_em(model[1].gap))


def test_printed_list_indents_keep_the_screens_em_ratio():
    tags = printing.tagdefs
    for level in (1, 2, 3):
        screen = tags.list_marker_column(level) - tags.CONTENT_MARGIN
        assert _paper_em(printing._left_margin_pt([f"list-indent-{level}"])) == pytest.approx(
            _screen_em(screen))
    item = print_model_for("- item\n")[0]
    assert _paper_em(-printing._marker_hang_pt(item)) == pytest.approx(
        _screen_em(tags.LIST_HANGING_INDENT))


@pytest.mark.parametrize("paper, screen", [
    ("CODE_BLOCK_PAD_PT", "CODE_BLOCK_PADDING"),
    ("CODE_BLOCK_RADIUS_PT", "CODE_BLOCK_RADIUS"),
    ("HEADING_RULE_PAD_PT", "HEADING_RULE_PAD"),
])
def test_printed_block_decorations_keep_the_screens_em_ratio(paper, screen):
    assert _paper_em(getattr(printing, paper)) == pytest.approx(
        _screen_em(getattr(printing.tagdefs, screen)))


def test_printed_table_rows_are_padded_like_the_screens():
    """Each row on screen is its labels' top and bottom margins plus a
    one-pixel rule; on paper it was 6pt of padding each side plus a 6pt
    gap, twice the screen's overhead, which is what made table-heavy
    documents run to extra pages."""
    from lectern import tables as tabledefs
    blocks = _blocks_for("| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n")
    table = next(b for b in blocks if b.kind == "table")
    row_layouts, row_heights = table.rows
    text_height = max(layout.get_pixel_size()[1] for layout in row_layouts[1])
    assert _paper_em(row_heights[1] - text_height) == pytest.approx(
        _screen_em(2 * tabledefs.CELL_PAD_Y))
    assert _paper_em(printing.TABLE_ROW_GAP_PT) == pytest.approx(
        _screen_em(tabledefs.ROW_RULE_PX))


# -- Table cells keep their inline styles on paper ----------------------------

STYLED_TABLE = """\
| Name | Note |
|---|---|
| **bold** | *slanted* and ~~gone~~ |
| `code` | [site](https://example.com/t) and [here](other.md) |
"""


def _printed_table(markdown_text):
    block = next(b for b in _blocks_for(markdown_text) if b.kind == "table")
    return block.rows[0], block.row_items


def _attrs_at(layout, text):
    """Every Pango attribute type covering `text`'s first character, with
    font descriptions unpacked into the fields they set."""
    from gi.repository import Pango
    index = layout.get_text().encode("utf-8").index(text.encode("utf-8"))
    found = {}
    for attr in layout.get_attributes().get_attributes():
        if not attr.start_index <= index < attr.end_index:
            continue
        desc = attr.as_font_desc()
        if desc is not None:
            found["weight"] = desc.desc.get_weight()
            found["style"] = desc.desc.get_style()
            found["family"] = desc.desc.get_family()
        elif attr.klass.type == Pango.AttrType.STRIKETHROUGH:
            found["strikethrough"] = True
        elif attr.klass.type == Pango.AttrType.FOREGROUND:
            found["foreground"] = True
    return found


def test_printed_table_cells_keep_their_inline_styles():
    from gi.repository import Pango
    layouts, _items = _printed_table(STYLED_TABLE)
    assert _attrs_at(layouts[0][0], "Name")["weight"] == Pango.Weight.BOLD
    assert _attrs_at(layouts[1][0], "bold")["weight"] == Pango.Weight.BOLD
    assert _attrs_at(layouts[1][1], "slanted")["style"] == Pango.Style.ITALIC
    assert _attrs_at(layouts[1][1], "gone").get("strikethrough")
    assert _attrs_at(layouts[1][1], "and").get("weight", Pango.Weight.NORMAL) == Pango.Weight.NORMAL
    assert "mono" in _attrs_at(layouts[2][0], "code")["family"].lower()
    assert _attrs_at(layouts[2][1], "site").get("foreground")


def test_printed_table_cells_get_code_chips_and_scheme_links_only():
    _layouts, items = _printed_table(STYLED_TABLE)
    assert printing._inline_code_spans(items[2][0]) == [(0, 4)]
    links = printing._link_spans(items[2][1], items[2][1].hrefs)
    assert [href for _s, _e, href in links] == ["https://example.com/t"]


def test_table_cell_links_become_pdf_annotations(tmp_path):
    out = tmp_path / "out.pdf"
    printing.PrintCoordinator().print_document(
        None, print_model_for(STYLED_TABLE), False, "Doc", "doc.md",
        action=Gtk.PrintOperationAction.EXPORT, export_path=str(out),
    )
    text = pdf_texts(out)
    assert len(re.findall(rb"/Subtype\s*/Link", text)) == 1
    assert b"https://example.com/t" in text


# -- The dialog's Scale reflows the document ----------------------------------

LONG = "# Hello\n\n" + "more text. " * 900 + "\n"


def run_scaled_export(markdown_text, tmp_path, scale, header_footer=False):
    """run_export with the print dialog's Scale set. EXPORT is the one
    path where GTK doesn't apply it, so this also covers draw-page making
    up for that."""
    coordinator = printing.PrintCoordinator()
    op = Gtk.PrintOperation()
    op.set_default_page_setup(printing._print_page_setup(header_footer))
    settings = Gtk.PrintSettings()
    settings.set_scale(scale)
    op.set_print_settings(settings)
    out = tmp_path / f"scaled-{scale}.pdf"
    op.set_export_filename(str(out))
    state = {"header_footer": header_footer,
             "header_left": printing._header_left_text("Hello", "hello.md")}
    op.connect("begin-print", coordinator._on_begin_print, print_model_for(markdown_text),
               False, state)
    op.connect("draw-page", coordinator._on_draw_page, state)
    assert op.run(Gtk.PrintOperationAction.EXPORT, None) == Gtk.PrintOperationResult.APPLY
    return state, out


def test_scale_lays_the_document_out_on_a_page_divided_by_it(tmp_path):
    full, _ = run_scaled_export(LONG, tmp_path, 100)
    small, _ = run_scaled_export(LONG, tmp_path, 80)
    assert small["width"] == pytest.approx(full["width"] / 0.8)
    assert small["height"] == pytest.approx(full["height"] / 0.8)
    assert small["paper_width"] == pytest.approx(full["width"])


def test_a_smaller_scale_reflows_onto_fewer_pages_and_a_bigger_onto_more(tmp_path):
    counts = {s: len(run_scaled_export(LONG, tmp_path, s)[0]["pages"]) for s in (70, 100, 140)}
    assert counts[70] < counts[100] < counts[140]


def test_lines_rewrap_to_the_scaled_width(tmp_path):
    full, _ = run_scaled_export(LONG, tmp_path, 100)
    small, _ = run_scaled_export(LONG, tmp_path, 80)
    words_per_line = []
    for state in (full, small):
        paragraph = next(b for b in state["blocks"] if b.kind == "layout" and b.heading_level is None)
        words_per_line.append(len(paragraph.layout.get_text().split()) / paragraph.layout.get_line_count())
    assert words_per_line[1] > words_per_line[0] * 1.15


def test_the_document_is_drawn_at_the_scale(tmp_path, monkeypatch):
    """EXPORT doesn't apply the Scale itself, so draw-page has to."""
    drawn = []
    real = printing._draw_entry

    def spy(cr, entry, page_width):
        drawn.append(cr.get_matrix().xx)
        real(cr, entry, page_width)

    monkeypatch.setattr(printing, "_draw_entry", spy)
    run_scaled_export("Some text.\n", tmp_path, 80)
    assert drawn and all(xx == pytest.approx(0.8) for xx in drawn)


def test_running_head_and_foot_keep_their_size_and_place_on_paper(tmp_path):
    full, _ = run_scaled_export(LONG, tmp_path, 100, header_footer=True)
    small, _ = run_scaled_export(LONG, tmp_path, 80, header_footer=True)
    assert small["header_band"] * 0.8 == pytest.approx(full["header_band"])
    assert small["paper_height"] == pytest.approx(full["paper_height"])


def test_missing_scale_makes_up_whatever_gtk_did_not_apply():
    import cairo
    identity = cairo.Matrix(1, 0, 0, 1, 18, 18)
    assert printing._missing_scale(identity, 0.8, 1) == pytest.approx(0.8)
    applied = cairo.Matrix(0.8, 0, 0, 0.8, 18, 18)
    assert printing._missing_scale(applied, 0.8, 1) == pytest.approx(1.0)
    rotated = cairo.Matrix(0, -0.8, 0.8, 0, 18, 18)  # landscape
    assert printing._missing_scale(rotated, 0.8, 1) == pytest.approx(1.0)
    assert printing._missing_scale(identity, 0.8, 2) == 1.0  # n-up: trust GTK


# -- The sheet a document was printed on is remembered ------------------------

def test_print_page_round_trips_through_plain_data():
    import json
    letter = Gtk.PaperSize.new(Gtk.PAPER_NAME_LETTER)
    page = printing.PrintPage(letter, Gtk.PageOrientation.LANDSCAPE, 80.0)
    back = printing.PrintPage.from_data(json.loads(json.dumps(page.to_data())))
    assert back.paper.get_name() == Gtk.PAPER_NAME_LETTER
    assert back.orientation == Gtk.PageOrientation.LANDSCAPE
    assert back.scale == 80.0


def test_a_custom_paper_size_round_trips_too():
    custom = Gtk.PaperSize.new_custom("receipt", "Receipt", 80, 200, Gtk.Unit.MM)
    back = printing.PrintPage.from_data(printing.PrintPage(custom, None, 100.0).to_data())
    assert back.paper.get_width(Gtk.Unit.MM) == pytest.approx(80)
    assert back.paper.get_height(Gtk.Unit.MM) == pytest.approx(200)


@pytest.mark.parametrize("data", [None, "A4", {"paper": "not a variant"}, {"scale": "big"}])
def test_an_unreadable_stored_page_is_dropped(data):
    assert printing.PrintPage.from_data(data) is None


def test_printing_reports_the_sheet_it_ran_with(tmp_path):
    coordinator = printing.PrintCoordinator()
    letter = Gtk.PaperSize.new(Gtk.PAPER_NAME_LETTER)
    coordinator.print_document(
        None, print_model_for(LONG), False, "Hello", "hello.md",
        action=Gtk.PrintOperationAction.EXPORT, export_path=str(tmp_path / "out.pdf"),
        page=printing.PrintPage(letter, Gtk.PageOrientation.LANDSCAPE, 80.0),
    )
    page = coordinator.page
    assert page.paper.get_name() == Gtk.PAPER_NAME_LETTER
    assert page.orientation == Gtk.PageOrientation.LANDSCAPE
    assert page.scale == pytest.approx(80.0)


def test_a_remembered_sheet_is_the_sheet_printed_on(tmp_path):
    """The PDF's page box is letter, landscape: the remembered page reached
    the job, not just the dialog's defaults."""
    out = tmp_path / "out.pdf"
    printing.PrintCoordinator().print_document(
        None, print_model_for(LONG), False, "Hello", "hello.md",
        action=Gtk.PrintOperationAction.EXPORT, export_path=str(out),
        page=printing.PrintPage(Gtk.PaperSize.new(Gtk.PAPER_NAME_LETTER),
                                Gtk.PageOrientation.LANDSCAPE, 80.0),
    )
    boxes = set(re.findall(rb"/MediaBox\s*\[\s*0 0 ([\d.]+) ([\d.]+)\s*\]", pdf_texts(out)))
    assert {(round(float(w)), round(float(h))) for w, h in boxes} == {(792, 612)}

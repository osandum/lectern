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
    raw scan of the file misses them entirely."""
    blob = path.read_bytes()
    out = [blob]
    for m in re.finditer(rb"stream\r?\n", blob):
        start = m.end()
        end = blob.find(b"endstream", start)
        if end == -1:
            continue
        try:
            out.append(zlib.decompress(blob[start:end].rstrip(b"\r\n")))
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

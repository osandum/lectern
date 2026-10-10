"""Headless image tests. Gdk.Texture.new_from_file decodes into memory
without needing a GL context or a display, so local loading is testable
here; the remote path is only checked to the extent that it must *not*
happen on its own.
"""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gio, GdkPixbuf

from markdown_it.tree import SyntaxTreeNode

from lectern.document import make_parser
from lectern.tags import create_tag_table
from lectern.renderer import MarkdownRenderer
from lectern.images import ImageView, is_remote

_PARSER = make_parser()


def write_png(path, width=40, height=20, color=0x3584E4FF):
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, width, height)
    pixbuf.fill(color)
    pixbuf.savev(str(path), "png", [], [])
    return path


def render(markdown_text, base_dir=None):
    tree = SyntaxTreeNode(_PARSER.parse(markdown_text))
    buffer = Gtk.TextBuffer(tag_table=create_tag_table(dark=False))
    renderer = MarkdownRenderer()
    renderer.render(tree, buffer, base_dir=base_dir)
    return renderer, buffer


def base_dir_for(tmp_path):
    return Gio.File.new_for_path(str(tmp_path))


def test_local_image_loads_from_a_path_relative_to_the_document(tmp_path):
    write_png(tmp_path / "pic.png", width=40, height=20)
    renderer, _ = render("![a picture](pic.png)\n", base_dir_for(tmp_path))
    assert len(renderer.images) == 1
    image = renderer.images[0]
    assert image.texture is not None
    assert (image.texture.get_width(), image.texture.get_height()) == (40, 20)


def test_image_in_a_subdirectory_resolves(tmp_path):
    (tmp_path / "pics").mkdir()
    write_png(tmp_path / "pics" / "pic.png")
    renderer, _ = render("![x](pics/pic.png)\n", base_dir_for(tmp_path))
    assert renderer.images[0].texture is not None


def test_missing_local_image_keeps_its_alt_text_and_no_texture(tmp_path):
    renderer, _ = render("![the alt text](nope.png)\n", base_dir_for(tmp_path))
    image = renderer.images[0]
    assert image.texture is None
    assert image.alt == "the alt text"


def test_alt_text_is_flattened_to_plain_text(tmp_path):
    # Two traps here: markdown-it leaves attrs["alt"] empty, and .content
    # holds the *raw* alt source -- using either would put literal
    # asterisks and backticks in the placeholder.
    renderer, _ = render("![alt *with* `code`](nope.png)\n", base_dir_for(tmp_path))
    assert renderer.images[0].alt == "alt with code"


def test_remote_image_is_not_fetched_on_render(tmp_path):
    """The privacy-relevant one: opening a document must not touch the
    network. Anything remote stays textureless until load_remote()."""
    renderer, _ = render("![r](https://example.invalid/x.png)\n", base_dir_for(tmp_path))
    image = renderer.images[0]
    assert image.remote is True
    assert image.texture is None


def test_local_images_are_not_flagged_remote(tmp_path):
    write_png(tmp_path / "pic.png")
    renderer, _ = render("![x](pic.png)\n", base_dir_for(tmp_path))
    assert renderer.images[0].remote is False


def test_is_remote_covers_only_http_schemes():
    assert is_remote("http://example.com/x.png")
    assert is_remote("https://example.com/x.png")
    assert not is_remote("pics/x.png")
    assert not is_remote("/abs/x.png")
    assert not is_remote("file:///abs/x.png")
    assert not is_remote("")


def test_image_appends_an_image_print_item(tmp_path):
    write_png(tmp_path / "pic.png")
    renderer, _ = render("![x](pic.png)\n", base_dir_for(tmp_path))
    kinds = [item.kind for item in renderer.print_model]
    assert "image" in kinds
    item = next(i for i in renderer.print_model if i.kind == "image")
    assert item.image is renderer.images[0]


def test_images_are_excluded_from_fill_width_widgets(tmp_path):
    """Images take the available width as a ceiling to scale down to, so
    they must not land in the list that gets a width forced onto it."""
    write_png(tmp_path / "pic.png")
    renderer, buffer = render("![x](pic.png)\n\n---\n", base_dir_for(tmp_path))
    textview = Gtk.TextView()
    textview.set_buffer(buffer)
    fill = renderer.attach_pending_widgets(textview)
    assert not any(isinstance(w, ImageView) for w in fill)
    assert len(fill) == 1  # just the hr separator


def test_available_width_caps_but_never_upscales(tmp_path):
    write_png(tmp_path / "wide.png", width=800, height=200)
    write_png(tmp_path / "small.png", width=40, height=10)
    renderer, _ = render(
        "![w](wide.png)\n\n![s](small.png)\n", base_dir_for(tmp_path)
    )
    wide, small = renderer.images
    wide.set_available_width(400)
    small.set_available_width(400)
    assert wide._picture.get_size_request() == (400, 100)   # scaled, aspect kept
    assert small._picture.get_size_request() == (40, 10)    # left alone


def test_linked_image_carries_the_links_href(tmp_path):
    # The thumbnail-gallery shape: a small image linking to the full one.
    write_png(tmp_path / "thumb.png")
    renderer, _ = render("[![t](thumb.png)](full.png)\n", base_dir_for(tmp_path))
    assert renderer.images[0].href == "full.png"


def test_unlinked_image_has_no_href(tmp_path):
    write_png(tmp_path / "pic.png")
    renderer, _ = render("![a](pic.png) [link](elsewhere.md)\n", base_dir_for(tmp_path))
    assert renderer.images[0].href is None


def test_file_name_with_space_and_percent_resolves(tmp_path):
    # markdown-it hands out "a%20b%25c.png" for `<a b%c.png>`.
    write_png(tmp_path / "a b%c.png")
    renderer, _ = render("![x](<a b%c.png>)\n", base_dir_for(tmp_path))
    assert renderer.images[0].texture is not None


def test_non_ascii_file_name_resolves(tmp_path):
    write_png(tmp_path / "skønne unger.png")
    renderer, _ = render("![x](<skønne unger.png>)\n", base_dir_for(tmp_path))
    assert renderer.images[0].texture is not None

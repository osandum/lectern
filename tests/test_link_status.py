"""The browser-style hover status: what the bottom-left bubble says for a
link, and the href resolution it shares with clicking.

The window is realised but never presented -- iter geometry isn't needed,
the hover handler is fed a resolved target directly through the same
methods the motion controller calls.
"""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib, Graphene

Adw.init()

from lectern.window import LecternWindow


def make_window(tmp_path, text):
    doc = tmp_path / "doc.md"
    doc.write_text(text)
    return LecternWindow(None, Gio.File.new_for_path(str(doc)))


def test_absolute_url_is_shown_as_written(tmp_path):
    window = make_window(tmp_path, "[x](https://example.com/a?b=1)\n")
    window._show_link_status("https://example.com/a?b=1")
    assert window._link_status_revealer.get_reveal_child()
    assert window._link_status_label.get_text() == "https://example.com/a?b=1"


def test_relative_href_shows_the_file_it_resolves_to(tmp_path):
    window = make_window(tmp_path, "[x](sub/other.md#sec)\n")
    window._show_link_status("sub/other.md#sec")
    assert window._link_status_label.get_text() == f"file://{tmp_path}/sub/other.md"


def test_percent_encoded_relative_href_resolves_to_the_decoded_file_name(tmp_path):
    # markdown-it hands out "my%20file.md" for `[x](<my file.md>)`; the
    # path must be decoded before it is treated as a filesystem path,
    # both for the status text and for what a click opens.
    window = make_window(tmp_path, "[x](<my file.md>)\n")
    assert window._resolve_href("my%20file.md") == f"file://{tmp_path}/my%20file.md"
    window._show_link_status("my%20file.md")
    assert window._link_status_label.get_text() == f"file://{tmp_path}/my file.md"


def test_fragment_is_shown_as_written_and_not_resolved(tmp_path):
    window = make_window(tmp_path, "# Top\n\n[x](#top)\n")
    assert window._resolve_href("#top") is None
    window._show_link_status("#top")
    assert window._link_status_label.get_text() == "#top"


def test_empty_href_hides_the_status(tmp_path):
    window = make_window(tmp_path, "[x](https://example.com)\n")
    window._show_link_status("https://example.com")
    window._show_link_status("")
    assert not window._link_status_revealer.get_reveal_child()


def test_table_cell_links_get_a_hover_controller(tmp_path):
    window = make_window(tmp_path, "| a |\n|---|\n| [c](https://example.com) |\n")
    labels = window._renderer.table_link_labels
    assert len(labels) == 1
    kinds = [type(c) for c in labels[0].observe_controllers()]
    assert Gtk.EventControllerMotion in kinds


def test_textview_motion_over_a_table_link_leaves_the_status_alone(tmp_path):
    # The TextView's motion handler fires for the same event as the
    # table label's (motion bubbles up from the anchored child), and
    # used to hide the bubble the label had just shown.
    window = make_window(tmp_path, "| a |\n|---|\n| [c](https://example.com) |\n")
    window.present()
    while GLib.MainContext.default().pending():
        GLib.MainContext.default().iteration(False)

    label = window._renderer.table_link_labels[0]
    ok, point = label.compute_point(window._textview, Graphene.Point.zero())
    assert ok
    width, height = label.get_width(), label.get_height()
    assert width and height

    window._show_link_status("https://example.com")
    window._on_textview_motion(None, point.x + width / 2, point.y + height / 2)
    assert window._link_status_revealer.get_reveal_child()

"""The recently-opened list (lectern/recent.py). Every test passes its own
Gtk.RecentManager(filename=...) so the real ~/.local/share/recently-used.xbel
is never touched."""
import time

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

import pytest

from lectern import recent


def _pump():
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


@pytest.fixture
def manager(tmp_path):
    return Gtk.RecentManager(filename=str(tmp_path / "recently-used.xbel"))


def _write(tmp_path, name, text="# hi\n"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return GLib.filename_to_uri(str(p), None)


def test_records_and_lists_markdown(tmp_path, manager):
    uri = _write(tmp_path, "notes.md")
    recent.record(uri, manager=manager)
    _pump()
    assert [i.get_uri() for i in recent.markdown_items(manager=manager)] == [uri]


def test_lists_most_recently_visited_first(tmp_path, manager):
    first = _write(tmp_path, "first.md")
    second = _write(tmp_path, "second.md")
    recent.record(first, manager=manager)
    time.sleep(1.05)  # get_visited() resolves to whole seconds
    recent.record(second, manager=manager)
    _pump()
    assert [i.get_uri() for i in recent.markdown_items(manager=manager)] == [second, first]


def test_non_markdown_in_the_shared_store_is_excluded(tmp_path, manager):
    md = _write(tmp_path, "keep.md")
    recent.record(md, manager=manager)
    # The store is shared desktop-wide; another app's non-Markdown entry
    # must not show up in Lectern's list.
    other = _write(tmp_path, "photo.png", text="notreallyapng")
    data = Gtk.RecentData()
    data.app_name = "Other"
    data.app_exec = "other %u"
    data.mime_type = "image/png"
    data.is_private = False
    manager.add_full(other, data)
    _pump()
    assert [i.get_uri() for i in recent.markdown_items(manager=manager)] == [md]


def test_missing_file_is_excluded(tmp_path, manager):
    present = _write(tmp_path, "here.md")
    gone = GLib.filename_to_uri(str(tmp_path / "gone.md"), None)
    recent.record(present, manager=manager)
    recent.record(gone, manager=manager)
    _pump()
    assert [i.get_uri() for i in recent.markdown_items(manager=manager)] == [present]


def test_limit_caps_the_list(tmp_path, manager):
    for n in range(5):
        recent.record(_write(tmp_path, f"doc{n}.md"), manager=manager)
    _pump()
    assert len(recent.markdown_items(limit=3, manager=manager)) == 3


def test_clear_empties_the_list(tmp_path, manager):
    recent.record(_write(tmp_path, "notes.md"), manager=manager)
    _pump()
    recent.clear(manager=manager)
    _pump()
    assert recent.markdown_items(manager=manager) == []

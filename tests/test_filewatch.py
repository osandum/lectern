"""FileWatcher (lectern/filewatch.py). Only the construction-failure path
is exercised here -- the debounced changed/deleted signal plumbing needs a
live filesystem watch and a running main loop to see fire, which is
already outside what this headless suite covers for window.py's other
collaborators.
"""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib

import pytest

from lectern.filewatch import FileWatcher


def test_monitor_failure_leaves_a_watcher_that_can_still_be_closed(tmp_path, monkeypatch):
    """gfile.monitor_file() can raise (an inotify watch limit, a
    filesystem that doesn't support monitoring) -- that shouldn't be
    fatal to opening a document, just to its live-reload support."""
    path = tmp_path / "doc.md"
    path.write_text("hello\n", encoding="utf-8")
    gfile = Gio.File.new_for_path(str(path))

    def raise_error(*_args, **_kwargs):
        raise GLib.Error("simulated monitor failure")

    monkeypatch.setattr(Gio.File, "monitor_file", raise_error)
    watcher = FileWatcher(gfile)
    assert watcher._monitor is None
    watcher.close()  # must not raise on a watcher that never got a monitor


def test_successful_monitor_creation_is_unaffected():
    path_gfile = Gio.File.new_for_path("/tmp")
    watcher = FileWatcher(path_gfile)
    assert watcher._monitor is not None
    watcher.close()

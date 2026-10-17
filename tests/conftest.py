"""Test-wide setup that has to happen before anything imports Gtk.

`GSK_RENDERER=cairo`: test_layout.py presents a real Gtk.Window, and GTK's
default (GL) renderer *aborts the process* -- SIGABRT, core dumped, no
catchable exception -- when it can't open libGLESv2, which is the case in a
bare container with only the packages CI installs. The cairo renderer needs
no GL at all and produces byte-identical geometry (verified against the GL
renderer on a desktop and against mesa-libGLES/mesa-dri-drivers in a Fedora
container). Set here rather than only in CI so a `podman run` or a slimmed
desktop can't core-dump the suite; the workflow sets it too, so the reason
is visible where the failure would otherwise appear.

Nothing else in the suite realizes a window, so this affects test_layout.py
alone -- but the variable is read when a renderer is first created, which
is too late to set from inside a test module that has already imported Gtk.
"""
import os

os.environ.setdefault("GSK_RENDERER", "cairo")


import pytest


@pytest.fixture(autouse=True)
def _private_recent_manager(tmp_path, monkeypatch):
    """Point lectern.recent at a throwaway store for every test.

    Anything that opens a document through LecternWindow records it in the
    recent list, and the default Gtk.RecentManager is the *desktop's* --
    so without this, every pytest tmp file showed up in the real Open
    Recent menu and the file manager's Recent view.
    """
    from gi.repository import Gtk
    from lectern import recent
    manager = Gtk.RecentManager(filename=str(tmp_path / "recently-used.xbel"))
    monkeypatch.setattr(recent, "_default", lambda: manager)

"""CLI entry point. Imports of markdown_it/pygments deliberately stay
inside document.py/highlighting.py, not here -- a bare `lectern` launch or
an argument error shouldn't pay for parsing/highlighting machinery it
never uses.
"""
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio

from . import __version__
from .i18n import _
from .window import LecternWindow

APPLICATION_ID = "io.github.osandum.Lectern"


class LecternApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APPLICATION_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)

        # GTK4 windows already ship a built-in "window.close" action (just
        # needs an accel); "quit" has no stock equivalent, so it needs an
        # actual action wired to Gio.Application.quit().
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda a, p: self.quit())
        self.add_action(quit_action)

    def do_startup(self):
        Adw.Application.do_startup(self)
        # Accels are application-scoped state (win.* actions are resolved
        # against whichever window has focus at trigger time) -- set once
        # per process here, not once per window in LecternWindow.__init__.
        self.set_accels_for_action("win.open", ["<primary>o"])
        self.set_accels_for_action("win.find", ["<primary>f"])
        self.set_accels_for_action("win.zoom-in", ["<primary>plus", "<primary>equal", "<primary>KP_Add"])
        self.set_accels_for_action("win.zoom-out", ["<primary>minus", "<primary>KP_Subtract"])
        self.set_accels_for_action("win.zoom-reset", ["<primary>0"])
        self.set_accels_for_action("win.print-doc", ["<primary>p"])
        # Ctrl+W closes the current document's window (Papers/Evince/GNOME
        # convention); Ctrl+Q quits the whole application, all windows at
        # once -- distinct from Ctrl+W only when more than one is open.
        self.set_accels_for_action("window.close", ["<primary>w"])
        self.set_accels_for_action("app.quit", ["<primary>q"])

    def do_open(self, files, n_files, hint):
        # One window per file, including when this arrives at an
        # already-running instance via GApplication's default D-Bus
        # activation -- no window-reuse logic, by design (Papers/Evince
        # model: a second file always gets a second window).
        for gfile in files:
            win = LecternWindow(application=self, gfile=gfile)
            win.present()

    def do_activate(self):
        win = LecternWindow(application=self)
        win.present()

    def _on_about(self, action, param):
        about = Adw.AboutDialog(
            application_name="Lectern",
            # Matches the icon actually installed at
            # data/icons/hicolor/scalable/apps/APPLICATION_ID.svg (and
            # the .desktop file's own Icon= line) -- resolved through
            # the icon theme by name, same as any other themed icon.
            application_icon=APPLICATION_ID,
            version=__version__,
            developer_name="Lectern contributors",
            license_type=Gtk.License.GPL_2_0,
            comments=_("A read-only, Papers-style Markdown viewer."),
            website="https://github.com/osandum/lectern",
        )
        about.present(self.get_active_window())


def main():
    app = LecternApplication()
    try:
        return app.run(sys.argv)
    except KeyboardInterrupt:
        # PyGObject's own SIGINT-fallback (gi/_ossighelper.py) lets the
        # GLib main loop quit cleanly on Ctrl-C, then re-raises
        # KeyboardInterrupt out of app.run() so callers can decide what to
        # do with it -- a bare CLI traceback isn't the right answer here.
        return 130  # conventional exit code for SIGINT-terminated processes


if __name__ == "__main__":
    sys.exit(main())

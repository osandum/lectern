"""Gio.FileMonitor wrapper with save-via-rename handling and debouncing."""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject

DEBOUNCE_MS = 300

_RELEVANT_EVENTS = (
    Gio.FileMonitorEvent.CHANGED,
    Gio.FileMonitorEvent.CHANGES_DONE_HINT,
    Gio.FileMonitorEvent.CREATED,
    Gio.FileMonitorEvent.RENAMED,
    Gio.FileMonitorEvent.MOVED_IN,
)


class FileWatcher(GObject.Object):
    """Emits `reload-needed` (debounced) or `file-missing` on delete."""

    __gsignals__ = {
        "reload-needed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "file-missing": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, gfile: Gio.File):
        super().__init__()
        self._gfile = gfile
        self._debounce_id = 0
        self._pending_missing = False
        # monitor_file can fail -- an inotify watch limit hit, a
        # filesystem that doesn't support monitoring at all (some FUSE
        # mounts, certain network shares) -- and that shouldn't be fatal
        # to opening a document that otherwise loaded fine. A window with
        # a watcher stuck at None just never emits reload-needed/
        # file-missing: live reload is lost, not the document.
        try:
            self._monitor = gfile.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error:
            self._monitor = None
            return
        self._monitor.connect("changed", self._on_changed)

    def _on_changed(self, monitor, file, other_file, event_type):
        if event_type in _RELEVANT_EVENTS:
            self._pending_missing = False
            self._schedule()
        elif event_type == Gio.FileMonitorEvent.DELETED:
            self._pending_missing = True
            self._schedule()

    def _schedule(self):
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(DEBOUNCE_MS, self._fire)

    def _fire(self):
        self._debounce_id = 0
        if self._pending_missing:
            self.emit("file-missing")
        else:
            self.emit("reload-needed")
        return GLib.SOURCE_REMOVE

    def close(self):
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
            self._debounce_id = 0
        if self._monitor is not None:
            self._monitor.cancel()

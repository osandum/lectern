"""The recently-opened list, backed by Gtk.RecentManager -- the
desktop-wide recent-files store the file manager and Gtk.FileDialog also
use. No GSettings schema, no private file, and it works under the Flatpak
document portal where a path-based store would not (see the
sticky_settings module docstring for why paths are unreliable there).

All Gtk.RecentManager use is funnelled through here so the rest of the app
has a small call surface, and so tests can pass their own manager
(Gtk.RecentManager(filename=...)) rather than touching the real store.
"""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

_MIME_TYPES = ("text/markdown", "text/x-markdown")
_SUFFIXES = (".md", ".markdown")
MAX_ITEMS = 10


def _default():
    return Gtk.RecentManager.get_default()


def record(uri, *, manager=None):
    """Note a document as just opened. Safe to call on every open -- the
    manager just bumps the timestamp on a URI it already holds.

    add_full rather than add_item: add_item derives the registering
    application from g_get_application_name(), which is only set once a
    GApplication is running -- so it silently no-ops (with a warning) when
    called from a plain script or a test. Naming Lectern explicitly also
    puts a usable app_exec in the shared store for other consumers."""
    data = Gtk.RecentData()
    data.app_name = "Lectern"
    data.app_exec = "lectern %u"
    data.mime_type = "text/markdown"
    data.is_private = False
    (manager or _default()).add_full(uri, data)


def markdown_items(limit=MAX_ITEMS, *, manager=None):
    """The still-existing Markdown entries, most-recently-visited first,
    capped at `limit`."""
    infos = [
        info for info in (manager or _default()).get_items()
        if info.exists() and _is_markdown(info)
    ]
    infos.sort(key=_visited_unix, reverse=True)
    return infos[:limit]


def clear(*, manager=None):
    (manager or _default()).purge_items()


def _is_markdown(info):
    if (info.get_mime_type() or "") in _MIME_TYPES:
        return True
    return (info.get_uri() or "").lower().endswith(_SUFFIXES)


def _visited_unix(info):
    visited = info.get_visited()
    # GTK4 returns a GLib.DateTime; older bindings handed back a Unix int.
    return visited.to_unix() if isinstance(visited, GLib.DateTime) else visited

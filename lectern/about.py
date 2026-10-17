"""The About dialog: who made Lectern, what it's built on, what changed in
this release, and -- under Troubleshooting -- exactly which build this is
and what it's running on, as a copyable block for bug reports.

Everything here runs only when the dialog is opened, so none of it (the
git query, the metainfo parse, the dependency version lookups) costs
anything at launch.
"""
import os
import platform
import sys
import xml.etree.ElementTree as ET
from importlib import metadata

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, Adw, GLib

from .buildinfo import PACKAGE_DIR, build_info
from .i18n import _

AUTHOR = "Ole Sandum"
WEBSITE = "https://github.com/osandum/lectern"

# Name, URL and license of what a Flatpak build bundles alongside Lectern
# itself; the GNOME runtime covers GTK and friends.
_BUNDLED = [
    ("markdown-it-py", "https://github.com/executablebooks/markdown-it-py",
     "© ExecutableBookProject", Gtk.License.MIT_X11),
    ("mdit-py-plugins", "https://github.com/executablebooks/mdit-py-plugins",
     "© ExecutableBookProject", Gtk.License.MIT_X11),
    ("linkify-it-py", "https://github.com/tsutsu3/linkify-it-py",
     "© tsutsu3", Gtk.License.MIT_X11),
    ("Pygments", "https://pygments.org",
     "© the Pygments authors", Gtk.License.BSD),
]


def present(parent, application_id):
    info = build_info()
    about = Adw.AboutDialog(
        application_name="Lectern",
        # Matches the icon actually installed at
        # data/icons/hicolor/scalable/apps/APPLICATION_ID.svg (and the
        # .desktop file's own Icon= line) -- resolved through the icon
        # theme by name, same as any other themed icon.
        application_icon=application_id,
        version=info.version_label,
        developer_name=AUTHOR,
        developers=[f"{AUTHOR} https://github.com/osandum"],
        copyright=f"© 2026 {AUTHOR}",
        license_type=Gtk.License.GPL_2_0,
        # No comments=: they only appear behind a "Details" row, which is a
        # click for one sentence. Leaving them out puts Website on the main
        # page instead, and _show_tagline puts the sentence there too.
        website=WEBSITE,
        issue_url=f"{WEBSITE}/issues",
        debug_info=debug_info(info),
        debug_info_filename="lectern-debug-info.txt",
    )
    # The msgid comes back unchanged where nobody has filled it in, and
    # showing the literal "translator-credits" would be worse than nothing.
    credits = _("translator-credits")
    if credits != "translator-credits":
        about.set_translator_credits(credits)

    notes = release_notes(application_id, info.version)
    if notes:
        about.set_release_notes(notes)
        about.set_release_notes_version(info.version)

    about.add_credit_section(_("Built With"), [
        "GTK https://gtk.org",
        "Libadwaita https://gnome.pages.gitlab.gnome.org/libadwaita/",
        "PyGObject https://pygobject.gnome.org",
        *(f"{name} {url}" for name, url, _c, _l in _BUNDLED),
    ])
    for name, _url, copyright, license_type in _BUNDLED:
        about.add_legal_section(name, copyright, license_type, None)

    about.present(parent)
    _show_tagline(about, _("A read-only, Papers-style Markdown viewer."))


def _find(widget, css_class):
    if widget.has_css_class(css_class):
        return widget
    child = widget.get_first_child()
    while child:
        found = _find(child, css_class)
        if found:
            return found
        child = child.get_next_sibling()
    return None


def _show_tagline(about, text):
    """Put `text` under the version pill. Adw.AboutDialog has no public
    place on its main page for a description, so this finds the pill by
    its `app-version` style class -- private structure, which is why
    failing to find it just leaves the tagline out. Only works once the
    dialog is presented: its widgets are built then, not at construction.
    """
    pill = _find(about, "app-version")
    if pill is None or not isinstance(pill.get_parent(), Gtk.Box):
        return
    label = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER)
    label.add_css_class("dimmed")
    pill.get_parent().insert_child_after(label, pill)


def _metainfo_candidates(application_id):
    name = f"{application_id}.metainfo.xml"
    # A checkout first: there the repo's own copy is the current one, and
    # an older installed release may be lying around system-wide.
    yield os.path.join(os.path.dirname(PACKAGE_DIR), "data", name)
    for share in (os.path.join(sys.prefix, "share"), *GLib.get_system_data_dirs()):
        yield os.path.join(share, "metainfo", name)


def release_notes(application_id, version, paths=None):
    """This release's <description> from the metainfo, in the AppStream
    markup Adw.AboutDialog's release notes take (<p>, <ul>, <code>, ...),
    or None.

    Adw.AboutDialog.new_from_appdata() would do this, but wants the
    metainfo in a GResource, which a plain setuptools package doesn't
    have -- so the installed file is read instead (the Flatpak and the RPM
    both install it; a pip install doesn't, and gets no notes).
    """
    for path in paths or _metainfo_candidates(application_id):
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        desc = root.find(f"releases/release[@version='{version}']/description")
        if desc is None:
            return None
        return "".join(ET.tostring(child, encoding="unicode") for child in desc).strip()
    return None


def _dist_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not found"


def debug_info(info):
    """A plain-text report for the Troubleshooting page. Not translated:
    it's for whoever reads the bug report, not for the reader."""
    if info.commit:
        commit = info.commit + (" (with uncommitted changes)" if info.dirty else "")
    else:
        commit = "unknown"
    built = info.built or "not built (running from a source checkout)"
    packaging = "Flatpak" if os.path.exists("/.flatpak-info") else "native"
    display = Gdk.Display.get_default()

    lines = [
        f"Lectern {info.version}",
        f"Commit: {commit}",
        f"Built: {built}",
        "",
        f"OS: {GLib.get_os_info('PRETTY_NAME') or platform.platform()}",
        f"Packaging: {packaging}",
        f"Display: {type(display).__name__ if display else 'none'}",
        f"Python: {platform.python_version()}",
        f"GTK: {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}",
        f"Libadwaita: {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}",
        f"PyGObject: {'.'.join(map(str, gi.version_info))}",
    ]
    for name, *_rest in _BUNDLED:
        lines.append(f"{name}: {_dist_version(name)}")
    return "\n".join(lines) + "\n"

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
from xml.sax.saxutils import escape
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
    if not _add_issue_row_to_troubleshooting(about, f"{WEBSITE}/issues"):
        about.set_issue_url(f"{WEBSITE}/issues")
    _fit_height_to_main_page(about)


def _fit_height_to_main_page(about):
    """Adw.AboutDialog asks for a fixed content height (638px in 1.9)
    whatever is on its main page, so the page scrolls by a few pixels as
    soon as it holds more than Adw's own rows -- the tagline's line or
    two is enough -- and leaves dead space when it holds fewer. Size it
    to the main page instead, measured after everything above has been
    added and at the width the dialog will actually have (a translated
    tagline can wrap to an extra line). Longer subpages still scroll, as
    they always did; a window too short for even the main page still
    clamps the dialog, as Adw does for any size.
    """
    nav = _find_type(about, Adw.NavigationView)
    page = nav.get_visible_page() if nav else None
    if page is None:
        return
    _minimum, natural, _mb, _nb = page.measure(Gtk.Orientation.VERTICAL, about.get_content_width())
    if natural > 0:
        about.set_content_height(natural)


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


def _add_issue_row_to_troubleshooting(about, url):
    """Put "Report an Issue" on the Troubleshooting page, next to the
    debug info a report wants, instead of as its own row on the main
    page. The page is found by its public navigation tag, but the list in
    it is private structure, so this returns False when it can't find it
    and the caller falls back to Adw's own row.

    The title is Adw's own string from its own catalogue, so it reads and
    translates exactly as the row it replaces.
    """
    nav = _find_type(about, Adw.NavigationView)
    page = nav.find_page("troubleshooting") if nav else None
    group = _find_type(page, Adw.PreferencesGroup) if page else None
    if group is None:
        return False
    row = Adw.ActionRow(title=GLib.dgettext("libadwaita", "_Report an Issue"),
                        use_underline=True, activatable=True)
    row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
    row.connect("activated", lambda r: Gtk.UriLauncher(uri=url).launch(r.get_root(), None, None))
    group.add(row)
    return True


def _find_type(widget, cls):
    if isinstance(widget, cls):
        return widget
    child = widget.get_first_child()
    while child:
        found = _find_type(child, cls)
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


def release_notes(application_id, version, paths=None, previous=5):
    """This release's <description> from the metainfo, followed by the
    first paragraph of each of up to `previous` earlier releases, in the
    AppStream markup Adw.AboutDialog's release notes take (<p>, <ul>,
    <em>, <code>, ...). None if the metainfo has no entry for `version`.

    Adw shows notes for one version only, so the earlier releases go into
    the same markup, each under an emphasised version-and-date line -- no
    words in it, so nothing to translate, and the date in the reader's
    own format.

    Adw.AboutDialog.new_from_appdata() would read the metainfo itself, but
    wants it in a GResource, which a plain setuptools package doesn't
    have -- so the installed file is read instead (the Flatpak and the RPM
    both install it; a pip install doesn't, and gets no notes).
    """
    for path in paths or _metainfo_candidates(application_id):
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        releases = root.findall("releases/release")
        current = next((i for i, r in enumerate(releases) if r.get("version") == version), None)
        if current is None or releases[current].find("description") is None:
            return None
        parts = [_markup(releases[current].find("description"))]
        for release in releases[current + 1:current + 1 + previous]:
            desc = release.find("description")
            if desc is None or len(desc) == 0:
                continue
            heading = release.get("version")
            date = _format_date(release.get("date"))
            if date:
                heading += f" · {date}"
            parts.append(f"<p><em>{escape(heading)}</em></p>")
            parts.append(_markup([desc[0]]))
        return "".join(parts)
    return None


def _markup(elements):
    return "".join(ET.tostring(e, encoding="unicode") for e in elements).strip()


def _format_date(iso):
    try:
        y, m, d = (int(n) for n in iso.split("-"))
        return GLib.DateTime.new_local(y, m, d, 0, 0, 0).format("%x")
    except (AttributeError, ValueError, TypeError):
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

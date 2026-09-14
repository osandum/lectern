# Lectern

[![Tests](https://github.com/osandum/lectern/actions/workflows/tests.yml/badge.svg)](https://github.com/osandum/lectern/actions/workflows/tests.yml)
[![License: GPL-2.0-or-later](https://img.shields.io/badge/License-GPL--2.0--or--later-blue.svg)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/osandum/lectern)](https://github.com/osandum/lectern/releases/latest)
[![Copr build status](https://copr.fedorainfracloud.org/coprs/osandum/lectern/package/lectern/status_image/last_build.png)](https://copr.fedorainfracloud.org/coprs/osandum/lectern/package/lectern/)

A read-only, [Papers](https://apps.gnome.org/da/Papers/)-style Markdown
viewer for GNOME: open a `.md` file, read it, search/zoom/print it. No
editing UI.

![Lectern showing a Markdown document: a table of ferry crossings, a task
list, a blockquote and a syntax-highlighted Python
function](data/screenshots/01-reading.png)

- Full CommonMark + GFM: tables, fenced code with syntax highlighting,
  task lists, footnotes, images.
- Mermaid diagrams — `flowchart`/`graph` (including `subgraph` frames),
  `sequenceDiagram`, `classDiagram` and `erDiagram` are drawn (and
  printed) natively, with no browser involved. Any other diagram type, or
  syntax outside the supported subset, stays a highlighted code block.
- Native GTK4/Libadwaita rendering (a single `Gtk.TextView` + tags) —
  no embedded browser engine, with spacing and typography following
  GitHub's Markdown rendering.
- Find bar — whole-word and case-sensitive, matching inside table cells
  as well as body text — zoom, printing, auto-reload when the file
  changes on disk.
- Links resolve relative to the document, and hovering one shows its
  target bottom-left, the way a browser does.
- One window per opened file, no sidebar — same feel as Papers/Evince.

### Remote images

Local images render immediately. Images with `http(s)` URLs are **not**
fetched when you open a document — opening a file shouldn't tell whoever
hosts its images that you opened it, and a per-image URL makes a
serviceable read receipt. A banner offers to load them, per document.

## Installing

**Fedora, via COPR:**

```sh
dnf copr enable osandum/lectern
dnf install lectern
```

No packaged builds yet for other distros — see "Running from source" below,
or "Building the Flatpak" for a local Flatpak build (not yet published to
Flathub).

## Running from source

```sh
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install -e .
lectern path/to/file.md
```

`--system-site-packages` is required so the venv can see the system
PyGObject/GTK4/Libadwaita bindings, which are not installed via pip.

To get a desktop entry with its icon:

```sh
cp data/io.github.osandum.Lectern.desktop ~/.local/share/applications/
cp -r data/icons/hicolor ~/.local/share/icons/
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor
update-desktop-database ~/.local/share/applications
```

The desktop entry's `Exec=lectern %U` resolves via `PATH`, so the venv's
`bin/` has to be on your `PATH` — symlinking `.venv/bin/lectern` into
`~/.local/bin/` is the easy way. Copying the entry without the icons
leaves it showing a blank tile, since `Icon=` names a themed icon rather
than a file path.

Nothing under `data/` is installed by `pip install -e .` — `pyproject.toml`
ships only the Python package. The Flatpak build installs them explicitly;
for a plain `pip` install you copy them yourself, as above.

## Building the Flatpak

```sh
flatpak install flathub org.gnome.Platform//50 org.gnome.Sdk//50
flatpak-builder --user --install --force-clean build \
    build-aux/io.github.osandum.Lectern.yml
flatpak run io.github.osandum.Lectern
```

The manifest is pinned to a released tag and its commit, so this builds
the *release*, not your working tree — it fetches from GitHub rather than
reading the checkout. To build what you have locally, swap the `type: git`
source for the `type: dir` block noted in the manifest just above it.

## Tests

```sh
pytest tests/
```

The tests never show a window, but they do need a display: constructing a
widget with no display at all segfaults. On a desktop they just run;
anywhere else, wrap them — `xvfb-run -a pytest`, which is what CI does.

Beware of concluding otherwise from unsetting `DISPLAY` and
`WAYLAND_DISPLAY`: GDK then falls back to the default `wayland-0` socket
in `XDG_RUNTIME_DIR` and connects anyway.

## Translating

See [`po/README.md`](po/README.md) for how to add or update a
translation.

## Claude Code integration

If you use [Claude Code](https://claude.com/claude-code), `contrib/claude-code/`
has a skill that teaches it to open generated Markdown deliverables — reports,
summaries, plans — in Lectern as it writes them, instead of just naming the
file path. Install it globally:

```sh
cp -r contrib/claude-code/skills/open-with-lectern ~/.claude/skills/
```

## Status

Known scope cuts: table column widths are a simple
median-character-count heuristic rather than true
text-measurement-based sizing, mermaid support is a deliberate subset
(the layout is a cut-down dagre rather than the real one), highlighting
for undeclared/obscure
fenced-code languages is best-effort, images don't scale with zoom, and
an image sitting *inside* a paragraph prints above that paragraph rather
than within it (on screen it's positioned correctly).

## License

GPL-2.0-or-later, see [LICENSE](LICENSE).

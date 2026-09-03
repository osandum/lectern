%global desktop_id io.github.osandum.Lectern

Name:           lectern
Version:        0.4.1
Release:        %autorelease
Summary:        A read-only, Papers-style GTK4/Libadwaita Markdown viewer

License:        GPL-2.0-or-later
URL:            https://github.com/osandum/lectern
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  gettext
BuildRequires:  desktop-file-utils
BuildRequires:  appstream

# Runtime, not just build: GTK4/Libadwaita aren't expressible as PyPI
# dependencies (PyGObject just binds whatever's on the system), so
# they have to be spelled out here explicitly.
Requires:       gtk4
Requires:       libadwaita
Requires:       python3-gobject

%description
Lectern is a read-only, Papers-style Markdown viewer for GNOME: open a
.md file, read it, search/zoom/print it. No editing UI.

Full CommonMark + GFM (tables, fenced code with syntax highlighting,
task lists, footnotes, images), Mermaid diagrams drawn natively (no
browser engine involved), and native GTK4/Libadwaita rendering via a
single Gtk.TextView.

%prep
%autosetup -p1 -n %{name}-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files lectern

install -Dm0644 data/%{desktop_id}.desktop \
    %{buildroot}%{_datadir}/applications/%{desktop_id}.desktop
install -Dm0644 data/%{desktop_id}.metainfo.xml \
    %{buildroot}%{_datadir}/metainfo/%{desktop_id}.metainfo.xml
install -Dm0644 data/icons/hicolor/scalable/apps/%{desktop_id}.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{desktop_id}.svg
install -Dm0644 data/icons/hicolor/symbolic/apps/%{desktop_id}-symbolic.svg \
    %{buildroot}%{_datadir}/icons/hicolor/symbolic/apps/%{desktop_id}-symbolic.svg

# lectern/locale/<lang>/LC_MESSAGES/*.mo ships as package data inside
# site-packages/lectern/ (lectern/i18n.py loads it relative to its own
# module path, not from the standard /usr/share/locale tree), so it's
# already captured by %{pyproject_files} above -- no %find_lang needed.

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{desktop_id}.desktop
appstreamcli validate --no-net \
    %{buildroot}%{_datadir}/metainfo/%{desktop_id}.metainfo.xml

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{_bindir}/lectern
%{_datadir}/applications/%{desktop_id}.desktop
%{_datadir}/metainfo/%{desktop_id}.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/%{desktop_id}.svg
%{_datadir}/icons/hicolor/symbolic/apps/%{desktop_id}-symbolic.svg

%changelog
%autochangelog

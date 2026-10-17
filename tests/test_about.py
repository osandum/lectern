"""Build info and the About dialog's content (lectern/buildinfo.py,
lectern/about.py)."""
import os
import subprocess

from lectern import __version__, about
from lectern.buildinfo import BuildInfo, archival_commit, build_info, git_commit

APP_ID = "io.github.osandum.Lectern"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_version_label_carries_the_short_commit():
    info = BuildInfo("1.2.3", "0123456789abcdef", False, None)
    assert info.version_label == "1.2.3 (0123456)"


def test_version_label_flags_uncommitted_changes():
    info = BuildInfo("1.2.3", "0123456789abcdef", True, None)
    assert info.version_label == "1.2.3 (0123456-dirty)"


def test_version_label_without_a_commit_is_just_the_version():
    assert BuildInfo("1.2.3", None, False, None).version_label == "1.2.3"


def test_unsubstituted_archival_file_yields_no_commit(tmp_path):
    p = tmp_path / "_archival.txt"
    p.write_text("commit: $Format:%H$\n")
    assert archival_commit(str(p)) is None


def test_substituted_archival_file_yields_its_commit(tmp_path):
    p = tmp_path / "_archival.txt"
    p.write_text("commit: 0123456789abcdef\n")
    assert archival_commit(str(p)) == "0123456789abcdef"


def test_a_directory_inside_someone_elses_repo_reports_no_commit(tmp_path):
    # A venv living inside an unrelated checkout must not borrow its commit.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    nested = tmp_path / "venv" / "site-packages"
    nested.mkdir(parents=True)
    assert git_commit(str(nested)) == (None, False)


def test_build_info_reports_this_package_version():
    assert build_info().version == __version__


def test_debug_info_names_version_commit_and_build_time():
    info = BuildInfo("1.2.3", "0123456789abcdef", True, "2026-10-17T12:00:00Z")
    text = about.debug_info(info)
    assert "Lectern 1.2.3" in text
    assert "0123456789abcdef (with uncommitted changes)" in text
    assert "Built: 2026-10-17T12:00:00Z" in text


def test_release_notes_come_from_the_matching_release(tmp_path):
    p = tmp_path / "x.metainfo.xml"
    p.write_text("""<component><releases>
      <release version="2.0"><description><p>New</p></description></release>
      <release version="1.0"><description><p>Old <code>x</code></p></description></release>
    </releases></component>""")
    assert about.release_notes(APP_ID, "1.0", [str(p)]) == "<p>Old <code>x</code></p>"
    assert about.release_notes(APP_ID, "3.0", [str(p)]) is None


def test_the_current_version_has_release_notes():
    # Bumping __version__ without a matching <release> in the metainfo
    # would quietly drop "What's New" from the About dialog.
    path = os.path.join(REPO, "data", f"{APP_ID}.metainfo.xml")
    assert about.release_notes(APP_ID, __version__, [path])

"""Where this copy of Lectern came from: version, commit and build time.

A copy arrives by one of three routes, and each knows its commit a
different way:

- **Built** (`pip install .`, the Flatpak, the RPM): `setup.py`'s
  `build_py` writes `lectern/_build.py` into the built package, so the
  answer travels with the code. The Flatpak builds from a git clone; the
  RPM builds from a GitHub release tarball, which has no `.git` but does
  have `_archival.txt` with its `$Format:...$` placeholders filled in by
  `git archive` (see `.gitattributes`).
- **Editable install** (`pip install -e .`, i.e. development): there is
  no `_build.py` -- one frozen at install time would go stale with the
  next commit -- so the checkout is asked directly, each time.
- Neither: commit unknown, and the About dialog says so.

Deliberately free of `gi` imports: `setup.py` imports this at build time,
where no GTK is needed.
"""
import os
import subprocess
from dataclasses import dataclass

from . import __version__

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVAL_FILE = os.path.join(PACKAGE_DIR, "_archival.txt")


@dataclass(frozen=True)
class BuildInfo:
    version: str
    commit: str | None
    dirty: bool
    # ISO 8601, UTC. None when running straight from a checkout, which
    # was never built as such.
    built: str | None

    @property
    def short_commit(self):
        if not self.commit:
            return None
        return self.commit[:7] + ("-dirty" if self.dirty else "")

    @property
    def version_label(self):
        if not self.commit:
            return self.version
        return f"{self.version} ({self.short_commit})"


def _git(repo, *args):
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True,
                              text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def git_commit(repo):
    """(commit, dirty) for the checkout rooted at `repo`, or (None, False).

    Only if `repo` is itself the top of a work tree: a package installed
    into a venv that happens to live inside some other repository must not
    report *that* repository's commit as its own.
    """
    top = _git(repo, "rev-parse", "--show-toplevel")
    if not top or os.path.realpath(top) != os.path.realpath(repo):
        return None, False
    commit = _git(repo, "rev-parse", "HEAD")
    if not commit:
        return None, False
    dirty = bool(_git(repo, "status", "--porcelain", "--untracked-files=no"))
    return commit, dirty


def archival_commit(path=ARCHIVAL_FILE):
    """The commit `git archive` stamped into `_archival.txt`, or None in a
    checkout, where the placeholder is still literally `$Format:%H$`."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition(":")
                value = value.strip()
                if key.strip() == "commit" and value and not value.startswith("$Format"):
                    return value
    except OSError:
        pass
    return None


def build_info():
    try:
        from . import _build
    except ImportError:
        commit, dirty = git_commit(os.path.dirname(PACKAGE_DIR))
        return BuildInfo(__version__, commit, dirty, None)
    return BuildInfo(__version__, _build.COMMIT, _build.DIRTY, _build.BUILT)

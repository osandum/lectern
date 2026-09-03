"""Document-level tests. Currently the title derivation behind the window
title, which is what the shell's window list shows -- without it, several
open documents all appear as the bare application name.
"""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio

from lectern.document import Document


def load(tmp_path, text, name="doc.md"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    document = Document(Gio.File.new_for_path(str(path)))
    document.load()
    return document


def test_title_is_the_first_level_one_heading(tmp_path):
    doc = load(tmp_path, "# The Title\n\nbody\n")
    assert doc.title == "The Title"


def test_title_flattens_inline_markup(tmp_path):
    doc = load(tmp_path, "# A *fancy* `title`\n\nbody\n")
    assert doc.title == "A fancy title"


def test_only_the_first_h1_wins(tmp_path):
    doc = load(tmp_path, "# First\n\nbody\n\n# Second\n")
    assert doc.title == "First"


def test_h1_after_other_blocks_is_still_found(tmp_path):
    doc = load(tmp_path, "Some preamble.\n\n# The Title\n\nbody\n")
    assert doc.title == "The Title"


def test_a_document_opening_with_h2_has_no_title(tmp_path):
    """Deliberate: promoting a section heading to the document's name
    would be a guess, and the filename is the honest fallback."""
    doc = load(tmp_path, "## Section\n\nbody\n")
    assert doc.title is None


def test_no_headings_means_no_title(tmp_path):
    doc = load(tmp_path, "Just body text.\n")
    assert doc.title is None


def test_empty_h1_does_not_produce_an_empty_title(tmp_path):
    doc = load(tmp_path, "#\n\nbody\n")
    assert doc.title is None


def test_title_updates_when_the_file_is_reloaded(tmp_path):
    doc = load(tmp_path, "# Before\n\nbody\n")
    assert doc.title == "Before"
    (tmp_path / "doc.md").write_text("# After\n\nbody\n", encoding="utf-8")
    doc.reload()
    assert doc.title == "After"


def test_reading_time_rounds_up_a_partial_minute(tmp_path):
    """201 words at ~200 wpm is just over a minute, not exactly one --
    floor division used to report both as 1 min."""
    doc = load(tmp_path, " ".join(["word"] * 201))
    assert doc.reading_time_minutes() == 2


def test_reading_time_is_exact_on_a_multiple_of_the_rate(tmp_path):
    doc = load(tmp_path, " ".join(["word"] * 400))
    assert doc.reading_time_minutes() == 2


def test_reading_time_is_never_zero_for_a_short_document(tmp_path):
    doc = load(tmp_path, "one two three")
    assert doc.reading_time_minutes() == 1

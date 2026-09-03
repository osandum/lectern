"""#30: sticky per-document print settings, keyed on title/file name
rather than path (see sticky_settings.py's own docstring for why)."""
import json
import os

import pytest

from lectern import sticky_settings


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Every test gets its own store file -- never the real
    ~/.config/lectern/sticky-settings.json."""
    monkeypatch.setattr(sticky_settings, "_STORE_PATH", str(tmp_path / "sticky-settings.json"))


def test_key_for_prefers_title_over_basename():
    assert sticky_settings.key_for("My Title", "file.md") == sticky_settings.key_for("My Title", "other.md")


def test_key_for_falls_back_to_basename_without_a_title():
    assert sticky_settings.key_for(None, "file.md") == sticky_settings.key_for("", "file.md")
    assert sticky_settings.key_for(None, "file.md") != sticky_settings.key_for(None, "other.md")


def test_key_for_is_none_without_title_or_basename():
    assert sticky_settings.key_for(None, None) is None
    assert sticky_settings.key_for("", "") is None


def test_get_sticky_returns_default_when_nothing_saved():
    key = sticky_settings.key_for("Untouched", None)
    assert sticky_settings.get_sticky(key, "header_footer", True) is True
    assert sticky_settings.get_sticky(key, "header_footer", False) is False


def test_get_sticky_with_no_key_returns_default_without_touching_disk():
    assert sticky_settings.get_sticky(None, "header_footer", True) is True
    assert not os.path.exists(sticky_settings._STORE_PATH)


def test_set_sticky_with_no_key_is_a_no_op():
    sticky_settings.set_sticky(None, "header_footer", False)
    assert not os.path.exists(sticky_settings._STORE_PATH)


def test_set_then_get_roundtrips():
    key = sticky_settings.key_for("Roundtrip", None)
    sticky_settings.set_sticky(key, "header_footer", False)
    assert sticky_settings.get_sticky(key, "header_footer", True) is False


def test_set_sticky_persists_across_a_fresh_load():
    """Not just an in-memory cache -- a later call re-reads the file."""
    key = sticky_settings.key_for("Persisted", None)
    sticky_settings.set_sticky(key, "header_footer", False)
    on_disk = json.loads(open(sticky_settings._STORE_PATH, encoding="utf-8").read())
    assert on_disk[key]["header_footer"] is False


def test_set_sticky_leaves_other_settings_under_the_same_key_alone():
    key = sticky_settings.key_for("Multi-setting", None)
    sticky_settings.set_sticky(key, "header_footer", False)
    sticky_settings.set_sticky(key, "some_other_setting", "value")
    assert sticky_settings.get_sticky(key, "header_footer", True) is False
    assert sticky_settings.get_sticky(key, "some_other_setting", None) == "value"


def test_corrupt_store_file_is_treated_as_empty(tmp_path):
    with open(sticky_settings._STORE_PATH, "w", encoding="utf-8") as f:
        f.write("not valid json{{{")
    key = sticky_settings.key_for("Anything", None)
    assert sticky_settings.get_sticky(key, "header_footer", True) is True
    # And a subsequent write still succeeds, overwriting the corrupt file.
    sticky_settings.set_sticky(key, "header_footer", False)
    assert sticky_settings.get_sticky(key, "header_footer", True) is False


@pytest.mark.parametrize("contents", ["[]", '"hello"', "123", "null"])
def test_valid_json_of_the_wrong_shape_is_treated_as_empty(contents):
    """Valid JSON, just not the {key: {setting: value}} object this store
    needs -- json.load happily returns a list/string/number/None, and
    without a shape check that reaches get_sticky's/set_sticky's dict
    methods and blows up with an AttributeError."""
    with open(sticky_settings._STORE_PATH, "w", encoding="utf-8") as f:
        f.write(contents)
    key = sticky_settings.key_for("Anything", None)
    assert sticky_settings.get_sticky(key, "header_footer", True) is True
    sticky_settings.set_sticky(key, "header_footer", False)
    assert sticky_settings.get_sticky(key, "header_footer", True) is False


def test_lru_eviction_drops_the_least_recently_written_entry(monkeypatch):
    monkeypatch.setattr(sticky_settings, "_MAX_ENTRIES", 3)
    keys = [sticky_settings.key_for(f"Doc {i}", None) for i in range(4)]
    for key in keys:
        sticky_settings.set_sticky(key, "header_footer", False)
    # keys[0] was the first written and the least recently touched --
    # it's the one that should have been evicted once the 4th arrived.
    assert sticky_settings.get_sticky(keys[0], "header_footer", True) is True
    for key in keys[1:]:
        assert sticky_settings.get_sticky(key, "header_footer", True) is False


def test_lru_eviction_treats_a_rewrite_as_a_fresh_touch(monkeypatch):
    monkeypatch.setattr(sticky_settings, "_MAX_ENTRIES", 2)
    a, b, c = (sticky_settings.key_for(f"Doc {i}", None) for i in range(3))
    sticky_settings.set_sticky(a, "header_footer", False)
    sticky_settings.set_sticky(b, "header_footer", False)
    sticky_settings.set_sticky(a, "header_footer", True)  # touches `a` again
    sticky_settings.set_sticky(c, "header_footer", False)  # over the cap: evicts the LRU one
    # `b` hasn't been touched since the first round -- it's the one that goes.
    assert sticky_settings.get_sticky(b, "header_footer", "missing") == "missing"
    assert sticky_settings.get_sticky(a, "header_footer", "missing") is True
    assert sticky_settings.get_sticky(c, "header_footer", "missing") is False

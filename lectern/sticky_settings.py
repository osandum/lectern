"""A small per-document store for preferences that should survive a
relaunch -- currently just the print header/footer toggle, but written to
hold others later (see #30).

Keyed not to a specific *file* but to whatever "the same document" means
across sessions: a checksum of the document's own title if it has one,
else its file name. Deliberately not the file path -- Lectern also ships
as a Flatpak, and the file-forwarding portal mounts the very same host
file at a different sandboxed path on every open, which would defeat a
path-based key outright. The trade-off is that two unrelated documents
sharing a title or file name (two different README.md's, say) share one
sticky entry; acceptable for a boolean toggle, worth remembering if this
store grows richer settings later.

Capped at a fixed number of entries, least-recently-*written* dropped
first -- not least-recently-*read*, so merely opening a document doesn't
itself count as a use and doesn't need a disk write on every window open.
A plain dict already tracks this: insertion order is preserved, so
popping and reinserting a key on write moves it to the end, and the
front of the dict is always the eviction candidate.
"""
import hashlib
import json

from gi.repository import GLib

_STORE_PATH = GLib.get_user_config_dir() + "/lectern/sticky-settings.json"
_MAX_ENTRIES = 200


def key_for(title, basename):
    """The sticky key for a document: its title if it has one, else its
    file name -- or None if neither is available (a new/unsaved document,
    say), meaning there is nothing to persist against."""
    identity = title or basename
    if not identity:
        return None
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _load():
    try:
        with open(_STORE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # A missing file is the common case (nothing saved yet); a
        # corrupt or unreadable one is treated the same way -- starting
        # over beats refusing to print because of a damaged cache file.
        return {}
    # json.load happily returns a list, string or number -- all valid
    # JSON, none of them the {key: {setting: value}} shape this store
    # requires. Treat that the same as a corrupt file rather than let
    # get_sticky/set_sticky blow up calling dict methods on it.
    return data if isinstance(data, dict) else {}


def _save(store):
    import os
    os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
    with open(_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f)


def get_sticky(key, setting, default):
    """The persisted value of `setting` for `key`, or `default` if either
    the document has no key (see key_for) or nothing's been saved for it
    yet."""
    if key is None:
        return default
    return _load().get(key, {}).get(setting, default)


def set_sticky(key, setting, value):
    """Persist `value` for `setting` under `key`, silently doing nothing
    if the document has no key (see key_for)."""
    if key is None:
        return
    store = _load()
    entry = store.pop(key, {})
    entry[setting] = value
    store[key] = entry
    while len(store) > _MAX_ENTRIES:
        store.pop(next(iter(store)))
    _save(store)

"""lectern/i18n.py: the fallback (no translation built, the state this
repo ships in today) and the real lookup path, each verified rather
than assumed -- the latter by actually compiling a tiny throwaway
catalogue with msgfmt and loading it, the same round trip a real
translation goes through (po/README.md documents the same steps for a
real language).
"""
import gettext
import shutil
import subprocess

import pytest

from lectern import i18n


def test_untranslated_string_passes_through_unchanged():
    # This is the actual shipped state: no lectern/locale/ built yet,
    # so gettext.translation's fallback=True hands back a
    # NullTranslations, and every _()/ngettext() call is a no-op.
    assert i18n._("Print…") == "Print…"


def test_plural_without_a_catalogue_falls_back_to_english_rules():
    assert i18n.ngettext("{count} item", "{count} items", 1) == "{count} item"
    assert i18n.ngettext("{count} item", "{count} items", 2) == "{count} items"


@pytest.mark.skipif(shutil.which("msgfmt") is None, reason="msgfmt not installed")
def test_a_compiled_catalogue_is_actually_used(tmp_path):
    po_path = tmp_path / "xx.po"
    po_path.write_text(
        'msgid ""\n'
        'msgstr ""\n'
        '"Content-Type: text/plain; charset=UTF-8\\n"\n'
        '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n'
        "\n"
        'msgid "Print…"\n'
        'msgstr "TRANSLATED-PRINT"\n'
        "\n"
        'msgid "{count} item"\n'
        'msgid_plural "{count} items"\n'
        'msgstr[0] "ONE-{count}"\n'
        'msgstr[1] "MANY-{count}"\n',
        encoding="utf-8",
    )
    mo_dir = tmp_path / "locale" / "xx" / "LC_MESSAGES"
    mo_dir.mkdir(parents=True)
    mo_path = mo_dir / "lectern.mo"
    subprocess.run(
        ["msgfmt", str(po_path), "--output-file", str(mo_path)], check=True,
    )

    # A fresh translation object against the compiled catalogue --
    # i18n.py's own module-level _translation was already bound at
    # import time (before this .mo existed), so this exercises the
    # exact same gettext.translation() call it makes, not the cached
    # result of it.
    translation = gettext.translation("lectern", str(tmp_path / "locale"), languages=["xx"])
    assert translation.gettext("Print…") == "TRANSLATED-PRINT"
    assert translation.ngettext("{count} item", "{count} items", 1) == "ONE-{count}"
    assert translation.ngettext("{count} item", "{count} items", 2) == "MANY-{count}"
    # A string genuinely absent from the catalogue still passes through.
    assert translation.gettext("Not translated anywhere") == "Not translated anywhere"


def test_every_module_using_gettext_is_in_potfiles():
    # A module missing from po/POTFILES.in never reaches the template, so
    # its strings stay English in every language without anything saying
    # so -- which is how Open Recent went untranslated from 0.4.0 on.
    import os
    import re
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo, "po", "POTFILES.in"), encoding="utf-8") as f:
        listed = {line.strip() for line in f if line.strip() and not line.startswith("#")}
    using = set()
    for root, _dirs, files in os.walk(os.path.join(repo, "lectern")):
        for name in files:
            path = os.path.join(root, name)
            if not name.endswith(".py") or name == "i18n.py":
                continue
            with open(path, encoding="utf-8") as f:
                if re.search(r"^from \.+i18n import", f.read(), re.M):
                    using.add(os.path.relpath(path, repo))
    assert using <= listed, f"missing from po/POTFILES.in: {sorted(using - listed)}"

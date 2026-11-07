"""Mermaid diagrams drawn natively, no browser involved.

A ```mermaid fence is parsed here into a diagram model, laid out into a
`scene.Scene` of absolute geometry, and painted with cairo -- on screen by
`view.DiagramView` and on paper by printing.py, from the same scene code.
That mirrors how the rest of Lectern works (native GTK, never HTML), and
it is the only option that keeps a diagram in the printed document: the
alternative backends are a headless browser (mermaid-cli, which a Flatpak
can't run and which costs hundreds of milliseconds a diagram) or a remote
render service, which would post the document's contents to a third party
on open -- exactly what images.py refuses to do.

The cost is coverage. This draws the mermaid *subset* Lectern
understands; anything else -- an unsupported diagram type, a construct
the parser doesn't know, a syntax error -- raises Unsupported and
renderer.py falls back to showing the fence as a highlighted code block.
That fallback is the design, not a stopgap: a diagram drawn from a
half-understood source is worse than an honest listing, because the
reader has no way to tell it from what the author wrote.
"""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Pango

from . import entities
from . import flowchart
from . import sequence
from . import text as textlib
from .common import Invalid, Unsupported
from .view import DiagramView

# First word of the source -> the module that parses that diagram type.
_PARSERS = {
    "flowchart": flowchart.parse,
    "graph": flowchart.parse,
    "sequencediagram": sequence.parse,
    "classdiagram": entities.parse_class,
    "erdiagram": entities.parse_er,
}

SUPPORTED_TYPES = tuple(sorted(_PARSERS))


def _first_word(source):
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("%%"):
            continue
        # "classDiagram-v2" is the same diagram as "classDiagram"; the
        # suffix is a mermaid renderer version, not a type.
        word = line.split()[0].rstrip(";").lower()
        return word[:-3] if word.endswith("-v2") else word
    return ""


def parse(source):
    """Mermaid source -> a diagram model with a `build_scene(measurer)`.
    Raises Unsupported for anything this package doesn't draw."""
    parser = _PARSERS.get(_first_word(source))
    if parser is None:
        raise Unsupported(f"unsupported diagram type {_first_word(source)!r}")
    return parser(source)


def ui_font():
    """The screen's UI font, which diagram text is laid out in so a
    diagram's labels look like the prose around them.

    Print substitutes its own family for a variable UI font (see
    printing._base_font) -- that's why the font travels into layout as an
    argument instead of being looked up in here: the scene has to be laid
    out with the same font it will be drawn with, and paper and screen
    don't always agree on which that is.
    """
    settings = Gtk.Settings.get_default()
    name = settings.get_property("gtk-font-name") if settings is not None else None
    return Pango.FontDescription.from_string(name) if name else Pango.FontDescription.new()


def build_scene(diagram, base_font, font_px=textlib.FONT_PX):
    return diagram.build_scene(textlib.Measurer(base_font, font_px))


def scene_for_source(source, base_font, font_px=textlib.FONT_PX):
    """Parse and lay out in one step. Raises Unsupported."""
    return build_scene(parse(source), base_font, font_px)

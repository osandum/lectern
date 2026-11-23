"""What a flowchart's `classDef`, `class`, `A:::name`, `style` and
`linkStyle` say about how a node or an edge is drawn.

Mermaid hands these declarations to the browser as CSS. Lectern reads the
few that change the drawing: `fill`, `stroke`, `stroke-width`,
`stroke-dasharray` and `color` (the label; `fill` means nothing to an
edge). Any other declaration is
ignored, as all of them were before this: a style changes how a node
looks, never what the diagram says, so a property Lectern can't draw is
no reason to refuse the diagram.

Colours are drawn as the author wrote them on a light page, and adapted
on a dark one (`for_page`). That is a deliberate departure from GitHub,
which draws them as written in both: an author picks colours against the
light page nearly every editor and renderer shows, and a muted `#eee`
box with a `#999` dashed border turns, on a dark page, into a glaring
light slab whose dashes are lost against its own edge.

The other liberty taken is the label of a node given a `fill` but no
`color`: that would be the theme's text colour, which a light fill in a
dark window (or the reverse) makes unreadable, so it is picked to
contrast with the fill instead.
"""
import colorsys

import gi
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk

# Label colours for a filled node whose author didn't give one.
_DARK_TEXT = (0.12, 0.14, 0.16)
_LIGHT_TEXT = (0.94, 0.95, 0.96)


class Style:
    __slots__ = ("fill", "stroke", "width", "dash", "color")

    def __init__(self):
        self.fill = self.stroke = self.color = None     # (r, g, b, a)
        self.width = None                               # px
        self.dash = None                                # [on, off, ...] px; [] is solid


def declarations(styles):
    """mermaid's addClass/addVertex: a statement's styles, split into
    single declarations. `\\,` is an escaped comma inside a value."""
    joined = ",".join(styles).replace("\\,", "\0").replace(",", ";").replace("\0", ",")
    return [part.strip() for part in joined.split(";") if part.strip()]


def node_style(declared):
    """The Style for a node's declarations, later ones winning, or None
    if none of them is one Lectern draws."""
    style = _style(declared)
    if style is not None and style.color is None and style.fill is not None \
            and style.fill[3] >= 0.5:
        style.color = (*(_DARK_TEXT if _luminance(style.fill) > 0.4 else _LIGHT_TEXT), 1.0)
    return style


def edge_style(declared):
    """The Style for an edge's declarations, or None. An edge has no fill,
    so its label is left the colour edge labels always are unless a
    `color` says otherwise."""
    style = _style(declared)
    if style is not None:
        style.fill = None
    return style


def _style(declared):
    style = Style()
    found = False
    for declaration in declared:
        name, colon, value = declaration.partition(":")
        if not colon:
            continue
        name = name.strip().lower()
        value = value.replace("!important", "").strip()
        parsed = _PARSERS.get(name, lambda _value: None)(value)
        if parsed is None:
            continue
        setattr(style, _FIELDS[name], parsed)
        found = True
    return style if found else None


def for_page(rgba, page, fg):
    """An author's colour, as drawn on a page of colour `page` with text
    of colour `fg` (both rgb).

    On a light page it is left alone. On a dark one its lightness is
    mapped across, keeping hue and saturation: white lands on the page,
    black on the text colour, and the greys between keep their order and
    their spacing. So a fill a shade off white stays a shade off the page,
    and a border or label keeps the contrast its author gave it.
    """
    page_l = colorsys.rgb_to_hls(*page)[1]
    if page_l >= 0.5:
        return rgba
    fg_l = colorsys.rgb_to_hls(*fg)[1]
    hue, lightness, saturation = colorsys.rgb_to_hls(*rgba[:3])
    lightness = page_l + (1.0 - lightness) * (fg_l - page_l)
    return (*colorsys.hls_to_rgb(hue, lightness, saturation), rgba[3])


def _color(value):
    rgba = Gdk.RGBA()
    if not rgba.parse(value):
        return None
    return (rgba.red, rgba.green, rgba.blue, rgba.alpha)


def _length(value):
    number = value.lower().removesuffix("px").strip()
    try:
        width = float(number)
    except ValueError:
        return None
    return width if width >= 0 else None


def _dash(value):
    if value.lower() == "none":
        return []
    lengths = [_length(part) for part in value.replace(",", " ").split()]
    if not lengths or None in lengths:
        return None
    if not any(lengths):
        return []
    # An odd count repeats, as in SVG; cairo would read it the same way,
    # but spelling it out keeps the pattern inspectable.
    return lengths * 2 if len(lengths) % 2 else lengths


def _luminance(rgba):
    def linear(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (linear(c) for c in rgba[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


_PARSERS = {
    "fill": _color,
    "stroke": _color,
    "color": _color,
    "stroke-width": _length,
    "stroke-dasharray": _dash,
}
_FIELDS = {"fill": "fill", "stroke": "stroke", "color": "color",
           "stroke-width": "width", "stroke-dasharray": "dash"}

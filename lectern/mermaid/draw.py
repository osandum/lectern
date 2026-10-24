"""Painting a Scene with cairo.

One function serves both the on-screen widget and printing.py. The scene
is in nominal 96dpi pixels, so a caller whose context is in some other
unit (a printer's points) scales the context first and everything --
stroke widths, arrowheads, absolute font sizes -- follows along.
"""
import cairo

import gi
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo

from . import scene as sc
from . import text as textlib

ARROW_LENGTH = 9.0
ARROW_HALF_WIDTH = 4.0
DASH_PATTERN = [4.0, 3.0]


def _set_color(cr, palette, role):
    cr.set_source_rgb(*palette[role])


def _rounded_rect(cr, x, y, w, h, radius):
    radius = min(radius, w / 2, h / 2)
    if radius <= 0:
        cr.rectangle(x, y, w, h)
        return
    from math import pi
    cr.new_sub_path()
    cr.arc(x + w - radius, y + radius, radius, -pi / 2, 0)
    cr.arc(x + w - radius, y + h - radius, radius, 0, pi / 2)
    cr.arc(x + radius, y + h - radius, radius, pi / 2, pi)
    cr.arc(x + radius, y + radius, radius, pi, 3 * pi / 2)
    cr.close_path()


def _ellipse_path(cr, x, y, w, h):
    from math import pi
    cr.save()
    cr.translate(x + w / 2, y + h / 2)
    cr.scale(max(w / 2, 0.001), max(h / 2, 0.001))
    cr.arc(0, 0, 1.0, 0, 2 * pi)
    cr.restore()


def _fill_and_stroke(cr, palette, shape, line_width=1.4):
    if shape.fill is not None:
        _set_color(cr, palette, shape.fill)
        if shape.stroke is not None:
            cr.fill_preserve()
        else:
            cr.fill()
    if shape.stroke is not None:
        _set_color(cr, palette, shape.stroke)
        cr.set_line_width(line_width)
        cr.stroke()


# ER cardinality glyphs, as (bars, has_circle, has_crow). "one" is two
# bars, "zero-one" a bar and a circle, and the "many" ones a crow's foot
# with whatever qualifies it behind -- the standard reading, where the
# marks nearest the entity say what that end may hold.
_CROW_FEET = {
    "one": (2, False, False),
    "zero-one": (1, True, False),
    "many": (0, False, True),
    "zero-many": (0, True, True),
    "one-many": (1, False, True),
}
CROW_LENGTH = 11.0
CROW_HALF_WIDTH = 5.0
BAR_HALF_WIDTH = 5.0


def _draw_crow_foot(cr, kind, tip, unit, normal):
    bars, has_circle, has_crow = _CROW_FEET[kind]
    ux, uy = unit
    px, py = normal
    cr.set_line_width(1.4)

    def at(distance):
        return tip[0] - ux * distance, tip[1] - uy * distance

    cursor = 0.0
    if has_crow:
        # The fan opens *at* the entity, not away from it: its apex is
        # the point up the line and its three prongs land on the box.
        # Drawn the other way round it reads as an arrowhead, which in ER
        # notation means nothing at all.
        apex = at(CROW_LENGTH)
        for side in (-1, 0, 1):
            cr.move_to(*apex)
            cr.line_to(tip[0] + px * CROW_HALF_WIDTH * side,
                       tip[1] + py * CROW_HALF_WIDTH * side)
        cr.stroke()
        cursor = CROW_LENGTH
    for index in range(bars):
        bar_x, bar_y = at(cursor + 5.0 + index * 5.0)
        cr.move_to(bar_x + px * BAR_HALF_WIDTH, bar_y + py * BAR_HALF_WIDTH)
        cr.line_to(bar_x - px * BAR_HALF_WIDTH, bar_y - py * BAR_HALF_WIDTH)
    if bars:
        cr.stroke()
        cursor += 5.0 * bars
    if has_circle:
        from math import pi
        radius = 3.6
        centre = at(cursor + radius + 3.0)
        cr.arc(centre[0], centre[1], radius, 0, 2 * pi)
        cr.stroke()


def _draw_head(cr, palette, kind, tip, come_from, role):
    """One arrow/circle/cross/triangle/diamond glyph at `tip`, pointing
    away from `come_from`."""
    dx, dy = tip[0] - come_from[0], tip[1] - come_from[1]
    length = (dx * dx + dy * dy) ** 0.5
    if length < 1e-6:
        return
    ux, uy = dx / length, dy / length
    px, py = -uy, ux          # unit normal, for the glyph's half-width
    _set_color(cr, palette, role)
    if kind in ("arrow", "triangle"):
        base = (tip[0] - ux * ARROW_LENGTH, tip[1] - uy * ARROW_LENGTH)
        cr.move_to(*tip)
        cr.line_to(base[0] + px * ARROW_HALF_WIDTH, base[1] + py * ARROW_HALF_WIDTH)
        cr.line_to(base[0] - px * ARROW_HALF_WIDTH, base[1] - py * ARROW_HALF_WIDTH)
        cr.close_path()
        if kind == "arrow":
            cr.fill()
        else:
            # Hollow, and filled with the node colour rather than left
            # transparent, so the line it caps doesn't show through it.
            _set_color(cr, palette, sc.NODE)
            cr.fill_preserve()
            _set_color(cr, palette, role)
            cr.set_line_width(1.4)
            cr.stroke()
    elif kind in ("diamond", "diamond-open"):
        half = ARROW_LENGTH * 0.75
        cr.move_to(*tip)
        cr.line_to(tip[0] - ux * half + px * half * 0.6, tip[1] - uy * half + py * half * 0.6)
        cr.line_to(tip[0] - ux * half * 2, tip[1] - uy * half * 2)
        cr.line_to(tip[0] - ux * half - px * half * 0.6, tip[1] - uy * half - py * half * 0.6)
        cr.close_path()
        if kind == "diamond":
            cr.fill()
        else:
            _set_color(cr, palette, sc.NODE)
            cr.fill_preserve()
            _set_color(cr, palette, role)
            cr.set_line_width(1.4)
            cr.stroke()
    elif kind == "circle":
        from math import pi
        radius = ARROW_HALF_WIDTH
        cr.arc(tip[0] - ux * radius, tip[1] - uy * radius, radius, 0, 2 * pi)
        cr.fill()
    elif kind == "open":
        # The half-arrow mermaid draws for an async message: one barb, so
        # it reads as "sent, not waited for" beside a filled arrowhead.
        base = (tip[0] - ux * ARROW_LENGTH, tip[1] - uy * ARROW_LENGTH)
        cr.set_line_width(1.4)
        cr.move_to(base[0] + px * ARROW_HALF_WIDTH, base[1] + py * ARROW_HALF_WIDTH)
        cr.line_to(*tip)
        cr.line_to(base[0] - px * ARROW_HALF_WIDTH, base[1] - py * ARROW_HALF_WIDTH)
        cr.stroke()
    elif kind in _CROW_FEET:
        _draw_crow_foot(cr, kind, tip, (ux, uy), (px, py))
    elif kind == "cross":
        reach = ARROW_HALF_WIDTH
        cx, cy = tip[0] - ux * reach, tip[1] - uy * reach
        cr.set_line_width(1.6)
        cr.move_to(cx + (px - ux) * reach, cy + (py - uy) * reach)
        cr.line_to(cx - (px - ux) * reach, cy - (py - uy) * reach)
        cr.move_to(cx + (px + ux) * reach, cy + (py + uy) * reach)
        cr.line_to(cx - (px + ux) * reach, cy - (py + uy) * reach)
        cr.stroke()


def _draw_line(cr, palette, line):
    if len(line.points) < 2:
        return
    _set_color(cr, palette, line.stroke)
    cr.set_line_width(line.width)
    cr.set_dash(DASH_PATTERN if line.dashed else [])
    cr.move_to(*line.points[0])
    for point in line.points[1:]:
        cr.line_to(*point)
    cr.stroke()
    cr.set_dash([])
    if line.head:
        _draw_head(cr, palette, line.head, line.points[-1], line.points[-2], line.stroke)
    if line.tail:
        _draw_head(cr, palette, line.tail, line.points[0], line.points[1], line.stroke)


def _draw_text(cr, palette, scene, shape, base_font):
    _set_color(cr, palette, shape.color)
    layout = textlib.make_layout(
        PangoCairo.create_context(cr), base_font, scene.font_px * shape.scale, shape.text,
        bold=shape.bold, italic=shape.italic, mono=shape.mono,
        width=shape.w, align=shape.align,
    )
    if shape.underline:
        attributes = layout.get_attributes() or Pango.AttrList()
        attributes.insert(Pango.attr_underline_new(Pango.Underline.SINGLE))
        layout.set_attributes(attributes)
    cr.move_to(shape.x, shape.y)
    PangoCairo.show_layout(cr, layout)
    # show_layout paints glyphs but leaves the move_to's current point
    # behind, and cairo_arc *connects* a line from the current point to
    # the start of the arc it appends. Without this, every node text drawn
    # before a circle node put a stray line across the diagram, from the
    # text's origin to the circle -- and the same stale point rides along
    # into whatever the next stroke is.
    cr.new_path()


def draw_scene(cr, scene, palette, base_font):
    """Paint `scene` at the context's current origin, in scene pixels."""
    cr.save()
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    for shape in scene.shapes:
        if isinstance(shape, sc.Rect):
            _rounded_rect(cr, shape.x, shape.y, shape.w, shape.h, shape.radius)
            if shape.dashed:
                cr.set_dash(DASH_PATTERN)
            _fill_and_stroke(cr, palette, shape)
            cr.set_dash([])
        elif isinstance(shape, sc.Ellipse):
            _ellipse_path(cr, shape.x, shape.y, shape.w, shape.h)
            _fill_and_stroke(cr, palette, shape)
        elif isinstance(shape, sc.Polygon):
            cr.move_to(*shape.points[0])
            for point in shape.points[1:]:
                cr.line_to(*point)
            cr.close_path()
            _fill_and_stroke(cr, palette, shape)
        elif isinstance(shape, sc.Line):
            _draw_line(cr, palette, shape)
        elif isinstance(shape, sc.Text):
            _draw_text(cr, palette, scene, shape, base_font)
    cr.restore()

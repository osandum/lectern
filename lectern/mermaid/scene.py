"""The drawing-primitive layer every diagram type lays out into.

A `Scene` is deliberately dumb: absolute geometry in nominal 96dpi pixels
(the same unit tags.py and renderer.py measure in, so printing.py's `pt()`
is all that stands between it and paper) plus *role names* instead of
colours. Roles are resolved at draw time, which is what lets one scene
draw into a light window, a dark window and a print job without being
laid out again -- the same split tags.py maintains between a tag's shape
and its palette.
"""

# Roles a shape can paint with. Kept as plain strings rather than an enum
# so a scene stays trivially inspectable in tests.
NODE = "node"            # a flowchart box / class body / participant
NODE_ALT = "node-alt"    # a second fill for headers and emphasis
CLUSTER = "cluster"      # subgraph-style container
EDGE = "edge"            # link strokes and arrowheads
FG = "fg"                # primary text
DIM = "dim"              # secondary text (cardinalities, notes)
LABEL_BG = "label-bg"    # the plate an edge label sits on
NOTE = "note"            # sequence-diagram notes


class Rect:
    __slots__ = ("x", "y", "w", "h", "radius", "fill", "stroke", "dashed")

    def __init__(self, x, y, w, h, *, radius=0.0, fill=None, stroke=None, dashed=False):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.radius = radius
        self.fill = fill
        self.stroke = stroke
        self.dashed = dashed


class Polygon:
    __slots__ = ("points", "fill", "stroke")

    def __init__(self, points, *, fill=None, stroke=None):
        self.points = points
        self.fill = fill
        self.stroke = stroke


class Ellipse:
    __slots__ = ("x", "y", "w", "h", "fill", "stroke")

    def __init__(self, x, y, w, h, *, fill=None, stroke=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.fill = fill
        self.stroke = stroke


class Line:
    """A polyline, optionally arrowheaded at either end.

    `head`/`tail` name the *glyph*, not just its presence, because mermaid
    spells three of them (`-->`, `--o`, `--x`) and they mean different
    things to whoever wrote the diagram: "arrow", "circle", "cross",
    "triangle" (class inheritance) and "diamond"/"diamond-open"
    (composition/aggregation).
    """
    __slots__ = ("points", "stroke", "dashed", "head", "tail", "width")

    def __init__(self, points, *, stroke=EDGE, dashed=False, head=None, tail=None, width=1.0):
        self.points = points
        self.stroke = stroke
        self.dashed = dashed
        self.head = head
        self.tail = tail
        self.width = width


class Text:
    """A string positioned by the box it was measured into.

    Carrying the box rather than a baseline keeps layout and drawing in
    agreement without either having to know Pango's vertical metrics:
    whoever draws re-lays the string out with the same font, wrap width
    and alignment, and lands where the measurement said it would.
    """
    __slots__ = ("x", "y", "w", "h", "text", "bold", "italic", "underline", "mono", "align",
                 "color", "scale")

    def __init__(self, x, y, w, h, text, *, bold=False, italic=False, underline=False,
                 mono=False, align="center", color=FG, scale=1.0):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.text = text
        self.bold = bold
        self.italic = italic
        self.underline = underline      # takes no room, so isn't measured
        self.mono = mono
        self.align = align
        self.color = color
        # Fraction of the scene's font size to draw at. Secondary strings
        # (edge labels, cardinalities) run smaller than node text, and
        # they have to be *measured* at that size too -- measuring at the
        # base size and drawing smaller leaves a box that's too wide,
        # measuring at base and drawing bigger re-wraps the string.
        self.scale = scale


class Link:
    """A clickable area over a node that the source linked with `click`.

    Nothing is painted for it -- draw.py has no branch for it, so paper
    and screen ignore it alike -- but it lives in the shape list so it is
    moved by `normalize` along with the node it covers, and the widget can
    hit-test the scene in the scene's own coordinates.
    """
    __slots__ = ("x", "y", "w", "h", "href")

    def __init__(self, x, y, w, h, href):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.href = href


class Scene:
    __slots__ = ("width", "height", "shapes", "font_px")

    def __init__(self, width, height, shapes, font_px):
        self.width = width
        self.height = height
        self.shapes = shapes
        # The absolute pixel size the scene's text was measured at.
        # Drawing has to use the same one or every string lands off its
        # box, so it travels with the scene rather than being a constant
        # both sides happen to share.
        self.font_px = font_px

    def add(self, shape):
        self.shapes.append(shape)
        return shape

    @property
    def has_links(self):
        return any(isinstance(shape, Link) for shape in self.shapes)

    def link_at(self, x, y):
        """The href of the link under scene point (x, y), or None. The last
        one added wins where two overlap, the way it would be drawn on top."""
        for shape in reversed(self.shapes):
            if (isinstance(shape, Link)
                    and shape.x <= x <= shape.x + shape.w
                    and shape.y <= y <= shape.y + shape.h):
                return shape.href
        return None

    def normalize(self, margin=1.0):
        """Shift the drawing so nothing sits at a negative coordinate, and
        set the scene box from what is actually in it.

        Layout works in whatever coordinates fall out of it -- a self-loop
        arcs above its node, an edge label overhangs the leftmost box --
        and the widget then draws the scene from its own origin. Without
        this the overhang is simply clipped away.
        """
        x0, y0, x1, y1 = _bounds(self.shapes)
        translate(self.shapes, margin - x0, margin - y0)
        self.width = (x1 - x0) + 2 * margin
        self.height = (y1 - y0) + 2 * margin
        return self


def _point_extents(shape):
    """(x0, y0, x1, y1) of one shape, in scene coordinates."""
    if isinstance(shape, (Rect, Ellipse, Text, Link)):
        return shape.x, shape.y, shape.x + shape.w, shape.y + shape.h
    xs = [p[0] for p in shape.points]
    ys = [p[1] for p in shape.points]
    return min(xs), min(ys), max(xs), max(ys)


def _bounds(shapes):
    if not shapes:
        return 0.0, 0.0, 0.0, 0.0
    extents = [_point_extents(shape) for shape in shapes]
    return (min(e[0] for e in extents), min(e[1] for e in extents),
            max(e[2] for e in extents), max(e[3] for e in extents))


def translate(shapes, dx, dy):
    for shape in shapes:
        if isinstance(shape, (Rect, Ellipse, Text, Link)):
            shape.x += dx
            shape.y += dy
        else:
            shape.points = [(x + dx, y + dy) for x, y in shape.points]

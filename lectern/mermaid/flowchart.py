"""`flowchart` / `graph`: source -> node-and-edge model -> Scene.

The layout is a cut-down Sugiyama: rank the nodes into layers along the
diagram's direction, order each layer to unpick crossings, then assign
positions across the layer. That is the same shape of algorithm dagre
(what mermaid itself uses) implements, minus the parts that need a solver
-- ordering is barycentre sweeps rather than an optimal ordering, and
cross-axis placement is priority-based rather than a network simplex. The
difference shows up as slightly wider spacing and the odd avoidable
crossing on dense graphs, not as unreadable output.

Parsing is deliberately strict: anything this module doesn't understand
raises Unsupported, and the caller falls back to rendering the fence as a
highlighted code block. A diagram drawn *wrongly* is worse than one not
drawn at all -- the reader can't tell a mis-parse from what the author
meant, whereas the code block is at least honest.
"""
import re

from . import layered
from . import scene as sc
from .common import Unsupported, parse_label

# -- geometry, in nominal 96dpi pixels -------------------------------------

NODE_PAD_X = 14.0
NODE_PAD_Y = 9.0
MIN_NODE_W = 46.0
LABEL_PAD = 3.0
CORNER_RADIUS = 7.0
EDGE_LABEL_FONT_SCALE = 0.92
# Clear space between a subgraph's frame and the nodes inside it, and the
# scale its title is drawn at.
CLUSTER_PAD = 14.0
CLUSTER_RADIUS = 8.0
CLUSTER_TITLE_SCALE = 0.95

_DIRECTIONS = {"TD": "TD", "TB": "TD", "BT": "BT", "LR": "LR", "RL": "RL"}


# -- parsing ---------------------------------------------------------------

_HEADER_RE = re.compile(r"^(?:flowchart|graph)(?:\s+(?P<dir>[A-Za-z]{2}))?\s*$")
_ID_RE = re.compile(r"[A-Za-z0-9_]+")
_IGNORED_STATEMENTS = ("style ", "classdef ", "class ", "click ", "linkstyle ",
                       "direction ", "%%")

# `subgraph Id[Title]`, `subgraph Id["Title"]`, or a bare `subgraph Title`
# -- which mermaid treats as both the id and the label.
_SUBGRAPH_RE = re.compile(r"""
    ^subgraph \s+
    (?:
        (?P<key>[A-Za-z0-9_]+) \s* \[ (?P<bracket>.*) \] \s*
      | (?P<bare>.+?) \s*
    )$
""", re.X | re.I)

# Shape delimiters, longest first -- "[[" has to be tried before "[", or
# every subroutine node parses as a rect whose text starts with "[". The
# four slanted forms share the "[/" and "[\" openings and differ only in
# how they close, so a candidate whose closing delimiter isn't found is
# skipped rather than treated as a syntax error (see _parse_node).
_SHAPES = [
    ("[[", "]]", "subroutine"),
    ("[(", ")]", "cylinder"),
    ("([", "])", "stadium"),
    ("((", "))", "circle"),
    ("{{", "}}", "hexagon"),
    ("[/", "/]", "parallelogram"),
    ("[\\", "\\]", "parallelogram-alt"),
    ("[/", "\\]", "trapezoid"),
    ("[\\", "/]", "trapezoid-alt"),
    ("[", "]", "rect"),
    ("(", ")", "round"),
    ("{", "}", "diamond"),
    (">", "]", "asymmetric"),
]

# Mermaid spells a labelled link two ways: `A -- text --> B` and
# `A -->|text| B`. Rewriting the first into the second up front means the
# link scanner below only has to know one of them.
_MID_LABEL_FORMS = [
    (re.compile(r"--\s+(?P<text>.+?)\s+(?P<tail>-{2,}[>ox]?)"), "solid"),
    (re.compile(r"-\.\s+(?P<text>.+?)\s+(?P<tail>\.-+[>ox]?)"), "dotted"),
    (re.compile(r"==\s+(?P<text>.+?)\s+(?P<tail>={2,}[>ox]?)"), "thick"),
]

_LINK_RE = re.compile(r"""
    \s*
    (?P<tail>[<ox])?
    (?:
        (?P<invisible>~{3,})
      | (?P<dotted>-\.-+|-\.)
      | (?P<thick>={2,})
      | (?P<solid>-{2,})
    )
    (?P<head>[>ox])?
    (?:\|(?P<label>[^|]*)\|)?
    \s*
""", re.X)


class FlowNode:
    __slots__ = ("key", "text", "shape", "w", "h", "x", "y", "rank", "order")

    def __init__(self, key, text, shape):
        self.key = key
        self.text = text
        self.shape = shape
        self.w = self.h = 0.0
        self.x = self.y = 0.0      # centre, filled in by layout
        self.rank = 0
        self.order = 0

    @property
    def virtual(self):
        return False


class FlowEdge:
    __slots__ = ("src", "dst", "label", "style", "head", "tail")

    def __init__(self, src, dst, label, style, head, tail):
        self.src = src
        self.dst = dst
        self.label = label
        self.style = style      # solid | dotted | thick
        self.head = head        # arrow | circle | cross | None
        self.tail = tail


class Subgraph:
    """A `subgraph ... end` block: a frame drawn around its members.

    Membership is recorded rather than the layout being constrained by it.
    The nodes are laid out exactly as they would be without the block, and
    the frame is then fitted around wherever they landed -- with a check
    (see _cluster_boxes) that nothing else landed inside it. That is the
    honest version of a cut-down layout: a frame that has swallowed a node
    it doesn't own says something the author didn't write, so the diagram
    falls back to its source instead.
    """
    __slots__ = ("key", "title", "members", "children", "parent")

    def __init__(self, key, title, parent=None):
        self.key = key
        self.title = title
        self.members = []       # node keys directly inside this block
        self.children = []      # nested Subgraphs
        self.parent = parent

    def node_keys(self):
        """Every node inside this block, nested ones included."""
        keys = list(self.members)
        for child in self.children:
            keys.extend(child.node_keys())
        return keys


class Flowchart:
    def __init__(self, direction, nodes, edges, subgraphs=()):
        self.direction = direction
        self.nodes = nodes          # ordered dict: key -> FlowNode
        self.edges = edges
        self.subgraphs = list(subgraphs)   # top-level blocks; each may nest

    def all_subgraphs(self):
        """Every block, outermost first, so a parent's frame is drawn
        before the child that sits on top of it."""
        out = []
        stack = list(reversed(self.subgraphs))
        while stack:
            group = stack.pop()
            out.append(group)
            stack.extend(reversed(group.children))
        return out

    def build_scene(self, measurer):
        return _build_scene(self, measurer)


def _strip_comment(line):
    # `%%{init: ...}%%` directives and plain `%%` comments both go; a `%%`
    # inside a quoted label does not.
    out, quoted = [], False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            quoted = not quoted
        if not quoted and line.startswith("%%", i):
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _split_statements(source):
    statements = []
    for raw_line in source.splitlines():
        line = _strip_comment(raw_line)
        # Semicolons separate statements; mermaid allows but doesn't
        # require them, so they're a separator here and not a terminator.
        # One inside a quoted label is just text.
        for part in _split_unquoted(line, ";"):
            part = part.strip()
            if part:
                statements.append(part)
    return statements


def _split_unquoted(line, sep):
    parts, start, quoted = [], 0, False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == sep and not quoted:
            parts.append(line[start:i])
            start = i + 1
    parts.append(line[start:])
    return parts


def _parse_node(statement, pos):
    """Parse `ID`, `ID[text]`, `ID{text}`, ... at `pos`.

    Returns (key, text_or_None, shape_or_None, new_pos). A bare `ID` is a
    reference, which is why text is optional: `A --> B` declares both ends
    with no shape of their own, and a later `A[Real label]` fills it in.
    """
    match = _ID_RE.match(statement, pos)
    if not match:
        raise Unsupported(f"expected a node id at {statement[pos:pos + 12]!r}")
    key = match.group(0)
    pos = match.end()
    opened = False
    for open_delim, close_delim, shape in _SHAPES:
        if not statement.startswith(open_delim, pos):
            continue
        opened = True
        start = pos + len(open_delim)
        if statement.startswith('"', start):
            end = statement.find('"', start + 1)
            if end < 0:
                raise Unsupported("unterminated quoted label")
            close = statement.find(close_delim, end + 1)
            text = statement[start:end + 1]
        else:
            close = statement.find(close_delim, start)
            text = statement[start:close] if close >= 0 else ""
        if close < 0:
            continue  # try the next shape sharing this opening delimiter
        return key, parse_label(text), shape, close + len(close_delim)
    if opened:
        raise Unsupported(f"unterminated shape on node {key}")
    return key, None, None, pos


def _canonical_link(style, tail):
    """The `-->`-style spelling of a link whose label was written between
    its dashes, so the one link scanner sees a single form."""
    head = tail[-1] if tail and tail[-1] in ">ox" else ""
    body = {"solid": "--", "dotted": "-.-", "thick": "=="}[style]
    if style == "dotted" and not head:
        return "-.-"
    return body + head if head else {"solid": "---", "thick": "==="}[style]


def _normalize_mid_labels(statement):
    for pattern, style in _MID_LABEL_FORMS:
        while True:
            match = pattern.search(statement)
            if not match:
                break
            label = match.group("text").strip()
            replacement = f"{_canonical_link(style, match.group('tail'))}|{label}|"
            statement = statement[:match.start()] + replacement + statement[match.end():]
    return statement


_HEAD_GLYPHS = {">": "arrow", "o": "circle", "x": "cross"}


def _parse_link(statement, pos):
    match = _LINK_RE.match(statement, pos)
    if not match:
        return None
    # `~~~` is mermaid's invisible link: it ranks its ends like any other
    # edge but draws nothing, which is how authors force a node into a
    # particular layer. Parsing it as a style rather than skipping the
    # statement keeps that layout effect.
    style = ("invisible" if match.group("invisible")
             else "dotted" if match.group("dotted")
             else "thick" if match.group("thick") else "solid")
    label = parse_label(match.group("label")) if match.group("label") else ""
    head = _HEAD_GLYPHS.get(match.group("head") or "")
    tail = "arrow" if match.group("tail") == "<" else _HEAD_GLYPHS.get(match.group("tail") or "")
    return style, label, head, tail, match.end()


def parse(source):
    """Mermaid flowchart source -> Flowchart. Raises Unsupported."""
    statements = _split_statements(source)
    if not statements:
        raise Unsupported("empty diagram")
    header = _HEADER_RE.match(statements[0])
    if not header:
        raise Unsupported("not a flowchart header")
    direction = _DIRECTIONS.get((header.group("dir") or "TD").upper())
    if direction is None:
        raise Unsupported(f"unknown direction {header.group('dir')!r}")

    nodes = {}
    edges = []
    subgraphs = []      # top-level blocks
    open_blocks = []    # the `subgraph`s currently being read, innermost last

    def touch(key, text, shape):
        node = nodes.get(key)
        if node is None:
            # A node first seen as a bare reference gets its id as its
            # label, which is what mermaid shows until a shaped
            # declaration turns up.
            node = nodes[key] = FlowNode(key, text if text is not None else key, shape or "rect")
            # Membership follows first mention, as mermaid's does: a node
            # named inside a block belongs to it, and naming it again
            # outside doesn't move it.
            if open_blocks:
                open_blocks[-1].members.append(key)
        elif text is not None:
            node.text = text
            node.shape = shape or node.shape
        return node

    for statement in statements[1:]:
        lowered = statement.lower()
        if lowered.startswith("subgraph"):
            match = _SUBGRAPH_RE.match(statement)
            if not match:
                raise Unsupported(f"unparsed subgraph header {statement!r}")
            if match.group("key") is not None:
                key, title = match.group("key"), parse_label(match.group("bracket"))
            else:
                key = title = parse_label(match.group("bare"))
            parent = open_blocks[-1] if open_blocks else None
            group = Subgraph(key, title, parent)
            (parent.children if parent is not None else subgraphs).append(group)
            open_blocks.append(group)
            continue
        if lowered == "end":
            if not open_blocks:
                raise Unsupported("`end` outside a subgraph")
            open_blocks.pop()
            continue
        if any(lowered.startswith(prefix) for prefix in _IGNORED_STATEMENTS):
            continue
        if "&" in statement:
            raise Unsupported("`&` node lists are not supported")
        statement = _normalize_mid_labels(statement)
        pos = 0
        key, text, shape, pos = _parse_node(statement, 0)
        touch(key, text, shape)
        while pos < len(statement):
            link = _parse_link(statement, pos)
            if link is None:
                raise Unsupported(f"unparsed trailing {statement[pos:][:16]!r}")
            style, label, head, tail, pos = link
            next_key, text, shape, pos = _parse_node(statement, pos)
            touch(next_key, text, shape)
            edges.append(FlowEdge(key, next_key, label, style, head, tail))
            key = next_key
    if open_blocks:
        raise Unsupported("unclosed subgraph")
    if not nodes:
        raise Unsupported("no nodes")
    return Flowchart(direction, nodes, edges, subgraphs)


# -- sizing ----------------------------------------------------------------

def _size_node(node, measurer):
    text_w, text_h = measurer.size(node.text)
    width = max(MIN_NODE_W, text_w + 2 * NODE_PAD_X)
    height = text_h + 2 * NODE_PAD_Y
    shape = node.shape
    if shape == "diamond":
        # A rhombus only contains its inscribed rectangle if it's grown by
        # roughly the text box's own half-diagonal in each axis; anything
        # tighter clips the corners of long labels.
        width = text_w + 2 * NODE_PAD_X + text_h
        height = text_h * 2 + NODE_PAD_Y
    elif shape == "circle":
        diameter = max(text_w, text_h) * 1.3 + 2 * NODE_PAD_Y
        width = height = max(diameter, MIN_NODE_W)
    elif shape == "hexagon":
        width += height * 0.5
    elif shape == "subroutine":
        width += 16
    elif shape == "asymmetric":
        width += 14
    elif shape == "cylinder":
        height += 12
    elif shape in ("parallelogram", "parallelogram-alt", "trapezoid", "trapezoid-alt"):
        # The slanted sides eat into the text's own box at top or bottom,
        # so the label needs the width back.
        width += height * 0.5
    node.w, node.h = width, height


# -- scene assembly --------------------------------------------------------

_DASHED = {"dotted": True, "solid": False, "thick": False}
_STROKE_WIDTH = {"thick": 2.4, "solid": 1.4, "dotted": 1.4}
INVISIBLE = "invisible"


def _node_shapes(node):
    """The scene primitives for one node's outline, sans its text."""
    x = node.x - node.w / 2
    y = node.y - node.h / 2
    w, h = node.w, node.h
    shape = node.shape
    if shape == "circle":
        return [sc.Ellipse(x, y, w, h, fill=sc.NODE, stroke=sc.EDGE)]
    if shape == "diamond":
        return [sc.Polygon(
            [(node.x, y), (x + w, node.y), (node.x, y + h), (x, node.y)],
            fill=sc.NODE, stroke=sc.EDGE)]
    if shape == "hexagon":
        inset = min(h * 0.5, w * 0.3)
        return [sc.Polygon(
            [(x + inset, y), (x + w - inset, y), (x + w, node.y),
             (x + w - inset, y + h), (x + inset, y + h), (x, node.y)],
            fill=sc.NODE, stroke=sc.EDGE)]
    if shape in ("parallelogram", "parallelogram-alt", "trapezoid", "trapezoid-alt"):
        slant = min(h * 0.45, w * 0.28)
        top_left, top_right, bottom_left, bottom_right = {
            "parallelogram": (slant, 0, 0, slant),
            "parallelogram-alt": (0, slant, slant, 0),
            "trapezoid": (slant, slant, 0, 0),
            "trapezoid-alt": (0, 0, slant, slant),
        }[shape]
        return [sc.Polygon(
            [(x + top_left, y), (x + w - top_right, y),
             (x + w - bottom_right, y + h), (x + bottom_left, y + h)],
            fill=sc.NODE, stroke=sc.EDGE)]
    if shape == "asymmetric":
        inset = min(14.0, w * 0.25)
        return [sc.Polygon(
            [(x, y), (x + w - inset, y), (x + w, node.y), (x + w - inset, y + h), (x, y + h)],
            fill=sc.NODE, stroke=sc.EDGE)]
    if shape == "stadium":
        return [sc.Rect(x, y, w, h, radius=h / 2, fill=sc.NODE, stroke=sc.EDGE)]
    if shape == "round":
        return [sc.Rect(x, y, w, h, radius=CORNER_RADIUS, fill=sc.NODE, stroke=sc.EDGE)]
    if shape == "subroutine":
        return [
            sc.Rect(x, y, w, h, fill=sc.NODE, stroke=sc.EDGE),
            sc.Line([(x + 8, y), (x + 8, y + h)], stroke=sc.EDGE),
            sc.Line([(x + w - 8, y), (x + w - 8, y + h)], stroke=sc.EDGE),
        ]
    if shape == "cylinder":
        cap = 10.0
        return [
            sc.Rect(x, y, w, h, radius=cap * 0.9, fill=sc.NODE, stroke=sc.EDGE),
            sc.Line([(x, y + cap), (x + w, y + cap)], stroke=sc.EDGE),
        ]
    return [sc.Rect(x, y, w, h, fill=sc.NODE, stroke=sc.EDGE)]


def _title_height(group, measurer):
    return measurer.size(group.title, wrap=False, scale=CLUSTER_TITLE_SCALE)[1]


def _reserve_cluster_room(chart, measurer):
    """Grow every node inside a subgraph by the room its frames need.

    The layout knows nothing about subgraphs, so the space around a block
    has to exist before it runs -- afterwards there is nowhere to put a
    frame that doesn't already have a node in it. Each enclosing block
    contributes its padding on both sides plus its title band, in both
    axes: which axis the band lands on depends on the diagram's direction,
    and one generous rank is a much better failure than a frame that
    overlaps the node above it.

    Returns the original sizes, to restore before the boxes are drawn.
    """
    original = {key: (node.w, node.h) for key, node in chart.nodes.items()}
    for group in chart.all_subgraphs():
        margin = 2 * CLUSTER_PAD + _title_height(group, measurer)
        for key in group.node_keys():
            node = chart.nodes[key]
            node.w += margin
            node.h += margin
    return original


def _cluster_boxes(chart, measurer):
    """(group, x, y, w, h, title_h) per subgraph, fitted to where its
    members actually landed.

    Raises Unsupported if a frame would enclose a node that isn't its own.
    The layout is free to interleave two blocks' members -- nothing above
    stops it -- and a frame drawn around a foreign node states a grouping
    the author didn't write, which is exactly the kind of quiet mis-draw
    this package falls back rather than commit.
    """
    boxes = {}
    for group in reversed(chart.all_subgraphs()):   # innermost first
        keys = group.node_keys()
        if not keys:
            raise Unsupported(f"empty subgraph {group.key!r}")
        rects = [(chart.nodes[k].x - chart.nodes[k].w / 2,
                  chart.nodes[k].y - chart.nodes[k].h / 2,
                  chart.nodes[k].w, chart.nodes[k].h) for k in keys]
        rects += [boxes[child.key][1:5] for child in group.children]
        x0 = min(r[0] for r in rects) - CLUSTER_PAD
        y0 = min(r[1] for r in rects) - CLUSTER_PAD
        x1 = max(r[0] + r[2] for r in rects) + CLUSTER_PAD
        y1 = max(r[1] + r[3] for r in rects) + CLUSTER_PAD
        title_h = _title_height(group, measurer)
        y0 -= title_h
        boxes[group.key] = (group, x0, y0, x1 - x0, y1 - y0, title_h)

    owned = {group.key: set(group.node_keys()) for group in chart.all_subgraphs()}
    for key, (group, x0, y0, w, h, _title_h) in boxes.items():
        for node_key, node in chart.nodes.items():
            if node_key in owned[key]:
                continue
            if (abs(node.x - (x0 + w / 2)) * 2 < w + node.w
                    and abs(node.y - (y0 + h / 2)) * 2 < h + node.h):
                raise Unsupported(
                    f"subgraph {group.key!r} would enclose {node_key!r}")
    return [boxes[group.key] for group in chart.all_subgraphs()]


def _build_scene(chart, measurer):
    for node in chart.nodes.values():
        _size_node(node, measurer)
    original_sizes = _reserve_cluster_room(chart, measurer)

    cells, routes, width, height = layered.layout(
        chart.nodes, chart.edges, chart.direction)

    for key, (w, h) in original_sizes.items():
        chart.nodes[key].w, chart.nodes[key].h = w, h
    clusters = _cluster_boxes(chart, measurer)

    scene = sc.Scene(width, height, [], measurer.font_px)

    # Frames first, outermost first: everything else in the diagram is
    # drawn on top of them. Their titles are held back to the very end --
    # an edge crossing the frame's top band puts its own opaque label plate
    # exactly where the title is, and the title is the more structural of
    # the two, so it wins.
    for group, x, y, w, h, title_h in clusters:
        scene.add(sc.Rect(x, y, w, h, radius=CLUSTER_RADIUS,
                          fill=sc.CLUSTER, stroke=sc.EDGE, dashed=True))

    # Edges first so a node's fill covers the stub of any line that
    # overshot its boundary, rather than the line being drawn over the box.
    for index, edge in enumerate(chart.edges):
        if edge.style == INVISIBLE:
            continue    # ranked above, drawn never
        if edge.src == edge.dst:
            points = layered.self_loop_points(cells[edge.src])
        else:
            points = layered.edge_points(cells, routes, index, edge)
        scene.add(sc.Line(
            points,
            dashed=_DASHED[edge.style],
            width=_STROKE_WIDTH[edge.style],
            head=edge.head,
            tail=edge.tail,
        ))
        if edge.label:
            text_w, text_h = measurer.size(
                edge.label, wrap=False, scale=EDGE_LABEL_FONT_SCALE)
            cx, cy = layered.label_position(points)
            scene.add(sc.Rect(
                cx - text_w / 2 - LABEL_PAD, cy - text_h / 2 - LABEL_PAD,
                text_w + 2 * LABEL_PAD, text_h + 2 * LABEL_PAD,
                radius=3.0, fill=sc.LABEL_BG,
            ))
            scene.add(sc.Text(
                cx - text_w / 2, cy - text_h / 2, text_w, text_h,
                edge.label, color=sc.DIM, scale=EDGE_LABEL_FONT_SCALE,
            ))

    for node in chart.nodes.values():
        for shape in _node_shapes(node):
            scene.add(shape)
        text_w, text_h = measurer.size(node.text)
        scene.add(sc.Text(
            node.x - text_w / 2, node.y - text_h / 2, text_w, text_h, node.text))

    for group, x, y, w, h, title_h in clusters:
        if not group.title:
            continue
        text_w, text_h = measurer.size(
            group.title, wrap=False, scale=CLUSTER_TITLE_SCALE)
        title_x = x + (w - text_w) / 2
        title_y = y + (title_h - text_h) / 2 + CLUSTER_PAD / 2
        scene.add(sc.Rect(title_x - LABEL_PAD, title_y - LABEL_PAD,
                          text_w + 2 * LABEL_PAD, text_h + 2 * LABEL_PAD,
                          radius=3.0, fill=sc.CLUSTER))
        scene.add(sc.Text(title_x, title_y, text_w, text_h, group.title,
                          color=sc.DIM, scale=CLUSTER_TITLE_SCALE))

    # Self loops arc above their node and edge labels overhang the
    # leftmost box, so the box the widget draws into comes from the shapes
    # themselves rather than from the node placement above.
    return scene.normalize()

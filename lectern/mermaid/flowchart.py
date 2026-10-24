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
import math
import re

from . import layered
from . import scene as sc
from .common import Unsupported, curve_setting, decode_entities, parse_label, preprocess

# -- geometry, in nominal 96dpi pixels -------------------------------------

NODE_PAD_X = 14.0
NODE_PAD_Y = 9.0
MIN_NODE_W = 46.0
LABEL_PAD = 3.0
CORNER_RADIUS = 7.0
# How edges are drawn unless an init directive says otherwise: at right
# angles, the way GitHub draws a flowchart.
DEFAULT_CURVE = layered.ORTHOGONAL
EDGE_LABEL_FONT_SCALE = 0.92
# Line left visible either side of an edge label's plate -- enough to
# clear an arrowhead and still read as a line, not a plate stuck to a box.
EDGE_LABEL_CLEARANCE = 16.0
# Clear space between a subgraph's frame and the nodes inside it, and the
# scale its title is drawn at.
CLUSTER_PAD = 14.0
CLUSTER_RADIUS = 8.0
CLUSTER_TITLE_SCALE = 0.95

class FlowNode:
    __slots__ = ("key", "text", "shape", "href", "w", "h", "x", "y", "rank", "order", "cap")

    def __init__(self, key, text, shape):
        self.key = key
        self.text = text
        self.shape = shape
        self.href = None        # from a `click` statement
        self.w = self.h = 0.0
        self.x = self.y = 0.0      # centre, filled in by layout
        self.rank = 0
        self.order = 0
        self.cap = 0.0          # a cylinder's rim, see _size_node

    @property
    def virtual(self):
        return False


class FlowEdge:
    __slots__ = ("src", "dst", "label", "style", "head", "tail",
                 "src_cluster", "dst_cluster", "src_path", "dst_path")

    def __init__(self, src, dst, label, style, head, tail):
        self.src = src
        self.dst = dst
        self.label = label
        self.style = style      # solid | dotted | thick
        self.head = head        # arrow | circle | cross | None
        self.tail = tail
        # An end naming a subgraph rather than a node: the edge is laid
        # out to a node inside it, with its bends kept outside the frame
        # (the paths, see layered.group_paths), and cut where it meets
        # the frame -- which is how mermaid draws one.
        self.src_cluster = self.dst_cluster = None
        self.src_path = self.dst_path = None


class Subgraph:
    """A `subgraph ... end` block: a frame drawn around its members.

    The layout keeps a block's members together and everything else out
    of the band they occupy (layered.fence_groups), and the frame is then
    fitted around wherever they landed -- with a check (see _cluster_boxes)
    that nothing else landed inside it anyway. A frame that has swallowed a
    node it doesn't own says something the author didn't write, so the
    diagram falls back to its source instead.
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
    def __init__(self, direction, nodes, edges, subgraphs=(), curve=DEFAULT_CURVE):
        self.direction = direction
        self.curve = curve          # "orthogonal" | "basis" | "linear"
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


# -- parsing ---------------------------------------------------------------
#
# mermaid's own lexer and a transcription of its grammar (flowchart.lark),
# run by parsing.py from generated tables. The actions below do what
# flow.jison's do to mermaid's flowDb, and the model they leave behind
# becomes a Flowchart. Anything mermaid reads that Lectern doesn't draw
# -- shape data, markdown strings, shapes without a drawing here -- raises
# Unsupported, as does anything mermaid refuses.

def _preprocess(source):
    """The source as mermaid's flowchart parser gets it: the common steps
    (common.preprocess), then flowParser.ts's own tidying up."""
    return re.sub(r"}\s*\n", "}\n", preprocess(source))


# mermaid's directions, vertex types, edge types and strokes -> Lectern's.
_DIRECTIONS = {"TB": "TD", "TD": "TD", "BT": "BT", "LR": "LR", "RL": "RL",
               "<": "RL", "^": "BT", ">": "LR", "v": "TD"}
_SHAPES = {None: "rect", "square": "rect", "round": "round", "circle": "circle",
           "stadium": "stadium", "subroutine": "subroutine", "cylinder": "cylinder",
           "diamond": "diamond", "hexagon": "hexagon", "lean_right": "parallelogram",
           "lean_left": "parallelogram-alt", "trapezoid": "trapezoid",
           "inv_trapezoid": "trapezoid-alt", "odd": "asymmetric"}
_ENDS = {"arrow_open": (None, None), "arrow_point": (None, "arrow"),
         "double_arrow_point": ("arrow", "arrow"), "arrow_circle": (None, "circle"),
         "double_arrow_circle": ("circle", "circle"), "arrow_cross": (None, "cross"),
         "double_arrow_cross": ("cross", "cross")}
_STROKES = {"normal": "solid", "dotted": "dotted", "thick": "thick", "invisible": "invisible"}


def _label(text):
    """A label as addVertex/addSingleLink keep it: trimmed, and unquoted
    if quoted. Lectern draws no markdown strings."""
    if text["type"] == "markdown":
        raise Unsupported("markdown string")
    label = text["text"].strip()
    if len(label) >= 2 and label.startswith('"') and label.endswith('"'):
        label = label[1:-1]
    return label


def _link_end(token):
    """flowDb.destructEndLink: (type, stroke) from a link's closing."""
    text = token.strip()
    line, kind = text[:-1], "arrow_open"
    last = text[-1:]
    if last in ("x", ">", "o"):
        kind = {"x": "arrow_cross", ">": "arrow_point", "o": "arrow_circle"}[last]
        if text.startswith({"x": "x", ">": "<", "o": "o"}[last]):
            kind = "double_" + kind
            line = line[1:]
    stroke = "normal"
    if line.startswith("="):
        stroke = "thick"
    if line.startswith("~"):
        stroke = "invisible"
    if "." in line:
        stroke = "dotted"
    return {"type": kind, "stroke": stroke}


def _link(end, start=None):
    """flowDb.destructLink: a link's type and stroke, from its closing and,
    for one with its text between the dashes, its opening."""
    info = _link_end(end)
    if start is None:
        return info
    text = start.strip()
    kind = "arrow_open"
    if text[:1] in ("<", "x", "o"):
        kind = {"<": "arrow_point", "x": "arrow_cross", "o": "arrow_circle"}[text[0]]
        text = text[1:]
    stroke = "dotted" if "." in text else "thick" if "=" in text else "normal"
    if stroke != info["stroke"]:
        raise Unsupported("a link opened and closed with different strokes")
    if kind == "arrow_open":
        kind = info["type"]
    elif kind != info["type"]:
        raise Unsupported("a link with different heads at either end")
    else:
        kind = "double_" + kind
    return {"type": kind, "stroke": stroke}


class _FlowDb:
    """What mermaid's flowDb records, as far as Lectern draws it."""

    def __init__(self):
        self.direction = None
        self.vertices = {}      # id -> {"text", "type", "declared"}, in mermaid's order
        self.edges = []         # (start, end, link)
        self.subgraphs = []     # in the order they close: inner ones first
        self.links = {}

    def add_vertex(self, key, text=None, shape=None):
        if not key.strip():
            return
        vertex = self.vertices.setdefault(key, {"text": None, "type": None, "declared": False})
        if text is not None:
            vertex["text"] = _label(text)
            vertex["declared"] = True
        elif vertex["text"] is None:
            vertex["text"] = key
        if shape is not None:
            vertex["type"] = shape
            vertex["declared"] = True

    def add_link(self, starts, ends, link):
        for start in starts:
            for end in ends:
                self.edges.append((start, end, link))

    def add_subgraph(self, key, mentioned, title):
        """addSubGraph: a block's members are every node its statements
        mention, and the blocks inside it -- less any node a block closed
        earlier already took, which is why an inner block keeps its own."""
        taken = {node for group in self.subgraphs for node in group["nodes"]}
        nodes = []
        for item in mentioned:
            if item.strip() and item not in nodes and item not in taken:
                nodes.append(item)
        if title is not None and title["type"] == "markdown":
            raise Unsupported("markdown subgraph title")
        key = key or f"subGraph{len(self.subgraphs)}"
        self.subgraphs.append({"id": key, "title": (title["text"] if title else "").strip(),
                               "nodes": nodes})
        return key

    def chart(self, source):
        direction = _DIRECTIONS.get(self.direction)
        if direction is None:
            raise Unsupported(f"unknown direction {self.direction!r}")
        nodes = {}
        for key, vertex in self.vertices.items():
            if vertex["type"] not in _SHAPES:
                raise Unsupported(f"no drawing for the {vertex['type']!r} shape")
            nodes[key] = FlowNode(key, parse_label(decode_entities(vertex["text"])), _SHAPES[vertex["type"]])
        edges = []
        for start, end, link in self.edges:
            tail, head = _ENDS[link["type"]]
            label = parse_label(decode_entities(_label(link["text"]))) if "text" in link else ""
            edges.append(FlowEdge(start, end, label, _STROKES[link["stroke"]], head, tail))
        keys = [group["id"] for group in self.subgraphs]
        if len(keys) != len(set(keys)):
            raise Unsupported("a subgraph id defined twice")
        blocks = {group["id"]: Subgraph(group["id"], parse_label(decode_entities(group["title"])))
                  for group in self.subgraphs}
        for group in self.subgraphs:
            block = blocks[group["id"]]
            for member in group["nodes"]:
                if member in blocks:
                    blocks[member].parent = block
                    block.children.append(blocks[member])
                else:
                    block.members.append(member)
        roots = [blocks[key] for key in keys if blocks[key].parent is None]
        declared = {key for key, vertex in self.vertices.items() if vertex["declared"]}
        _attach_cluster_edges(nodes, edges, roots, declared)
        if not nodes:
            raise Unsupported("no nodes")
        for key, href in self.links.items():
            if key in nodes:
                nodes[key].href = href
        return Flowchart(direction, nodes, edges, roots,
                         curve_setting(source, "flowchart", DEFAULT_CURVE))


class _Actions:
    """flow.jison's actions, one method per action name in flowchart.lark;
    parsing.parse calls them as the parser reduces, in jison's order."""

    def __init__(self, db):
        self.db = db

    def graph_config(self, children):
        direction = next((t.value for t in children if t.type == "DIR"), "TB")
        self.db.direction = direction.strip()

    def document(self, children):
        out = []
        for child in children:
            if isinstance(child, list):
                out += child
            elif isinstance(child, str):
                out.append(child)       # a subgraph's id
        return out

    def stmt_vertex(self, children):
        return children[0]["nodes"]

    def stmt_ignored(self, children):
        return []

    def style_statement(self, children):
        # mermaid declares a node a style names, if nothing else did.
        self.db.add_vertex(children[2])

    def link_style_statement(self, children):
        # mermaid styles the edges by index there and then, and throws on
        # one that doesn't exist (yet).
        for child in children:
            if isinstance(child, list) and any(int(n) >= len(self.db.edges) for n in child):
                raise Unsupported("linkStyle for an edge that doesn't exist")

    def num_list(self, children):
        return [token.value for token in children if token.type == "NUM"]

    def click_link(self, children):
        # setLink: only a node that exists by now gets the link. Callback
        # clicks name page JavaScript, and are ignored like styles.
        key = children[0].value
        url = next(c.value for c in children[1:] if c.type == "STR")
        if key in self.db.vertices:
            self.db.links[key] = url

    def subgraph_titled(self, children):
        _subgraph, _space, key, _sqs, title, _sqe, _sep, document, _end = children
        return self.db.add_subgraph(key["text"].strip(), document, title)

    def subgraph_bare(self, children):
        _subgraph, _space, title, _sep, document, _end = children
        # A bare title with whitespace in it is no id: mermaid makes one up.
        key = None if re.search(r"\s", title["text"]) else title["text"].strip()
        return self.db.add_subgraph(key, document, title)

    def subgraph_anon(self, children):
        _subgraph, _sep, document, _end = children
        return self.db.add_subgraph(None, document, None)

    def shape_data(self, children):
        raise Unsupported("shape data (`@{ … }`)")

    def vs_node(self, children):
        return {"stmt": children[0], "nodes": children[0]}

    def vs_link(self, children):
        statement, link, node = children[:3]
        self.db.add_link(statement["stmt"], node, link)
        return {"stmt": node, "nodes": node + statement["nodes"]}

    def node_one(self, children):
        return [children[0]]

    def node_more(self, children):
        return children[0] + [children[-1]]

    def styled_vertex(self, children):
        return children[0]

    def id_string(self, children):
        return "".join(token.value for token in children)

    def alpha_num(self, children):
        return self.id_string(children)

    def _vertex(shape):
        def action(self, children):
            key = children[0]
            text = next((c for c in children[1:] if isinstance(c, dict)), None)
            self.db.add_vertex(key, text, shape)
            return key
        return action

    v_square = _vertex("square")
    v_doublecircle = _vertex("doublecircle")
    v_circle = _vertex("circle")
    v_ellipse = _vertex("ellipse")
    v_stadium = _vertex("stadium")
    v_subroutine = _vertex("subroutine")
    v_props = _vertex("square")
    v_cylinder = _vertex("cylinder")
    v_round = _vertex("round")
    v_diamond = _vertex("diamond")
    v_hexagon = _vertex("hexagon")
    v_odd = _vertex("odd")
    v_trapezoid = _vertex("trapezoid")
    v_inv_trapezoid = _vertex("inv_trapezoid")
    v_lean_right = _vertex("lean_right")
    v_lean_left = _vertex("lean_left")
    v_plain = _vertex(None)
    del _vertex

    def text(self, children):
        """`t: token | t token | STR | MD_STR`: the first piece says what
        kind of label it is, and the rest is appended to it."""
        first = children[0].type
        kind = "string" if first == "STR" else "markdown" if first == "MD_STR" else "text"
        return {"text": "".join(token.value for token in children), "type": kind}

    def link_statement(self, children):
        return _link(children[-1].value)

    def arrow_text(self, children):
        return children[1]

    def link_plain(self, children):
        return children[0]

    def link_piped(self, children):
        return {**children[0], "text": children[1]}

    def link_texted(self, children):
        start, text, end = children[-3:]
        return {**_link(end.value, start.value), "text": text}


def parse(source):
    """Mermaid flowchart source -> Flowchart. Raises Unsupported."""
    from . import _flowchart_lexer, _flowchart_tables, parsing
    db = _FlowDb()
    parsing.parse(_flowchart_tables, parsing.lex(_flowchart_lexer, _preprocess(source)),
                  _Actions(db))
    return db.chart(source)


def _attach_cluster_edges(nodes, edges, subgraphs, declared):
    """Turn edge ends naming a subgraph into cluster ends.

    Each such end was read as a bare node reference, and stands in for the
    block's first node instead (mermaid's findNonClusterChild). A key that
    is also a declared node, or an edge between a block and something
    inside it, has no drawing anyone could read unambiguously."""
    blocks = {}
    stack = list(subgraphs)
    while stack:
        group = stack.pop()
        blocks[group.key] = group
        stack.extend(group.children)
    clustered = [key for key in nodes if key in blocks]
    if not clustered:
        return
    for key in clustered:
        if key in declared:
            raise Unsupported(f"{key!r} is both a node and a subgraph")
        del nodes[key]
    for group in blocks.values():
        group.members = [key for key in group.members if key not in blocks]
    for edge in edges:
        # What each end covers: a block, every node in it.
        covers = []
        for end in ("src", "dst"):
            key = getattr(edge, end)
            if key not in blocks:
                covers.append({key})
                continue
            inside = blocks[key].node_keys()
            if not inside:
                raise Unsupported(f"edge to empty subgraph {key!r}")
            setattr(edge, end + "_cluster", key)
            setattr(edge, end, inside[0])
            covers.append(set(inside))
        if (edge.src_cluster or edge.dst_cluster) and covers[0] & covers[1]:
            raise Unsupported("edge between a subgraph and its own contents")


# -- sizing ----------------------------------------------------------------

def _cylinder_cap(width):
    """Half-height of a cylinder's elliptical top and bottom: mermaid's
    proportions, flatter the wider the node."""
    return width / 2 / (2.5 + width / 50)


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
        # The rim of the top takes both halves of the cap out of the box,
        # the bottom only one.
        height += 3 * _cylinder_cap(width)
        node.cap = _cylinder_cap(width)     # where edges meet it (layered.boundary_point)
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
        # The body, its bottom the front half of an ellipse, with the whole
        # top ellipse drawn over it: that covers the body's straight top
        # edge and leaves the rim showing.
        cap = _cylinder_cap(w)
        steps = 24
        bottom = [(node.x - w / 2 * math.cos(math.pi * i / steps),
                   y + h - cap + cap * math.sin(math.pi * i / steps))
                  for i in range(steps + 1)]
        return [
            sc.Polygon([(x, y + cap)] + bottom + [(x + w, y + cap)],
                       fill=sc.NODE, stroke=sc.EDGE),
            sc.Ellipse(x, y, w, 2 * cap, fill=sc.NODE, stroke=sc.EDGE),
        ]
    return [sc.Rect(x, y, w, h, fill=sc.NODE, stroke=sc.EDGE)]


def _title_height(group, measurer):
    return measurer.size(group.title, wrap=False, scale=CLUSTER_TITLE_SCALE)[1]


def _title_box(group, x, y, w, title_h, measurer):
    """(x, y, w, h) of a frame's title text, centred in its title band."""
    if not group.title:
        return (x, y, 0.0, 0.0)
    text_w, text_h = measurer.size(
        group.title, wrap=False, scale=CLUSTER_TITLE_SCALE)
    return (x + (w - text_w) / 2, y + (title_h - text_h) / 2 + CLUSTER_PAD / 2,
            text_w, text_h)


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


def _group_paths(chart):
    """Node key -> the keys of the blocks it sits in, outermost first."""
    paths = {}
    for group in chart.all_subgraphs():     # outermost first
        for key in group.node_keys():
            paths[key] = paths.get(key, ()) + (group.key,)
    return paths


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


def _clip_to_frame(points, frame):
    """`points` up to where they first enter `frame` -- an edge to a whole
    block ends at its frame, not at the node it was laid out to. Unchanged
    if they never do, or start inside it."""
    x0, y0, w, h = frame
    x1, y1 = x0 + w, y0 + h
    for index, ((px, py), (qx, qy)) in enumerate(zip(points, points[1:])):
        if x0 <= px <= x1 and y0 <= py <= y1:
            return points if index == 0 else points[:index + 1]
        # Liang-Barsky: the fraction of the segment at which it enters.
        enter, leave = 0.0, 1.0
        for delta, near, far in ((qx - px, x0 - px, x1 - px), (qy - py, y0 - py, y1 - py)):
            if delta == 0:
                if near > 0 or far < 0:
                    enter, leave = 1.0, 0.0
                continue
            t_near, t_far = sorted((near / delta, far / delta))
            enter, leave = max(enter, t_near), min(leave, t_far)
        if enter <= leave:
            return points[:index + 1] + [(px + (qx - px) * enter, py + (qy - py) * enter)]
    return points


def _build_scene(chart, measurer):
    for node in chart.nodes.values():
        _size_node(node, measurer)
    original_sizes = _reserve_cluster_room(chart, measurer)

    vertical = chart.direction in layered.VERTICAL_DIRECTIONS
    edge_room, label_sizes = {}, {}
    for index, edge in enumerate(chart.edges):
        if edge.label and edge.style != INVISIBLE:
            text_w, text_h = measurer.size(
                edge.label, wrap=False, scale=EDGE_LABEL_FONT_SCALE)
            edge_room[index] = ((text_h if vertical else text_w)
                                + 2 * LABEL_PAD + 2 * EDGE_LABEL_CLEARANCE)
            label_sizes[index] = (text_w + 2 * LABEL_PAD, text_h + 2 * LABEL_PAD)
    paths = _group_paths(chart)
    for edge in chart.edges:
        # A block's end counts as being where the block is, not inside it.
        for end in ("src", "dst"):
            cluster = getattr(edge, end + "_cluster")
            if cluster is not None:
                path = paths[getattr(edge, end)]
                setattr(edge, end + "_path", path[:path.index(cluster)])
    cells, routes, width, height = layered.layout(
        chart.nodes, chart.edges, chart.direction, edge_room=edge_room,
        groups=paths, label_sizes=label_sizes,
        square=[index for index, edge in enumerate(chart.edges) if edge.style != INVISIBLE]
        if chart.curve == layered.ORTHOGONAL else None)

    # The layers' extents with the room reserved for frames still in them,
    # which is what right-angled edges keep their tracks out of.
    bands = layered.rank_bands(cells, vertical)
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
    frames = {group.key: (x, y, w, h) for group, x, y, w, h, _title_h in clusters}

    # Edges first so a node's fill covers the stub of any line that
    # overshot its boundary, rather than the line being drawn over the box.
    # Their labels follow once every line is down, placed clear of each
    # other and of the nodes (layered.place_labels).
    labels = []
    drawn_lines = []    # (edge index, drawn path), for place_labels
    orthogonal = chart.curve == layered.ORTHOGONAL
    if orthogonal:
        square, _tracks = layered.orthogonal_routes(
            cells, {index: route for index, route in routes.items()
                    if chart.edges[index].style != INVISIBLE},
            chart.edges, vertical, label_sizes, bands)
    paths = {}
    for index, edge in enumerate(chart.edges):
        if edge.style == INVISIBLE:
            continue    # ranked above, drawn never
        if edge.src == edge.dst:
            points = layered.self_loop_points(cells[edge.src])
        elif orthogonal:
            points = square[index]
        else:
            points = layered.edge_points(cells, routes, index, edge)
        if edge.dst_cluster:
            points = _clip_to_frame(points, frames[edge.dst_cluster])
        if edge.src_cluster:
            points = _clip_to_frame(points[::-1], frames[edge.src_cluster])[::-1]
        paths[index] = points
    # Where two right-angled edges cross, one hops the other, which is
    # what keeps a crossing from reading as a junction.
    hops = layered.crossings(
        {index: points for index, points in paths.items()
         if chart.edges[index].src != chart.edges[index].dst},
        vertical) if orthogonal else {}

    for index, points in paths.items():
        edge = chart.edges[index]
        if orthogonal:
            drawn = layered.rounded(points, hops=hops.get(index))
        else:
            drawn = layered.curved(points, chart.curve, [
                (n.x - n.w / 2, n.y - n.h / 2, n.w, n.h) for key, n in chart.nodes.items()
                if key not in (edge.src, edge.dst)])
        drawn_lines.append((index, drawn))
        scene.add(sc.Line(
            drawn,
            dashed=_DASHED[edge.style],
            width=_STROKE_WIDTH[edge.style],
            head=edge.head,
            tail=edge.tail,
        ))
        if edge.label:
            text_w, text_h = measurer.size(
                edge.label, wrap=False, scale=EDGE_LABEL_FONT_SCALE)
            anchor = layered.label_anchor(cells, routes, index)
            if anchor is None and orthogonal:
                anchor = layered.run_anchor(points, vertical)
            labels.append((points, drawn, edge, text_w, text_h, anchor, index))

    obstacles = [(n.x - n.w / 2, n.y - n.h / 2, n.w, n.h) for n in chart.nodes.values()]
    # A frame's title wins over an edge label drawn where it is (see the
    # end), so a label has to keep clear of it, the same as of a node.
    titles = [_title_box(group, x, y, w, title_h, measurer)
              for group, x, y, w, _h, title_h in clusters]
    obstacles += [(tx - LABEL_PAD, ty - LABEL_PAD, tw + 2 * LABEL_PAD, th + 2 * LABEL_PAD)
                  for tx, ty, tw, th in titles if tw]
    owner = {label[-1]: request for request, label in enumerate(labels)}
    centres = layered.place_labels(
        [(points, w + 2 * LABEL_PAD, h + 2 * LABEL_PAD, drawn, anchor)
         for points, drawn, _edge, w, h, anchor, _index in labels],
        obstacles,
        [(owner.get(index), line) for index, line in drawn_lines])
    for (_points, _drawn, edge, text_w, text_h, _anchor, _index), (cx, cy) in zip(labels, centres):
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
        # Centred in a cylinder's body: below the rim, not in the box.
        text_y = node.y + (_cylinder_cap(node.w) / 2 if node.shape == "cylinder" else 0.0)
        scene.add(sc.Text(
            node.x - text_w / 2, text_y - text_h / 2, text_w, text_h, node.text))
        if node.href:
            scene.add(sc.Link(node.x - node.w / 2, node.y - node.h / 2,
                              node.w, node.h, node.href))

    for (group, _x, _y, _w, _h, _title_h), (title_x, title_y, text_w, text_h) \
            in zip(clusters, titles):
        if not group.title:
            continue
        scene.add(sc.Rect(title_x - LABEL_PAD, title_y - LABEL_PAD,
                          text_w + 2 * LABEL_PAD, text_h + 2 * LABEL_PAD,
                          radius=3.0, fill=sc.CLUSTER))
        scene.add(sc.Text(title_x, title_y, text_w, text_h, group.title,
                          color=sc.DIM, scale=CLUSTER_TITLE_SCALE))

    # Self loops arc above their node and edge labels overhang the
    # leftmost box, so the box the widget draws into comes from the shapes
    # themselves rather than from the node placement above.
    return scene.normalize()

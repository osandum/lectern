"""`stateDiagram` / `stateDiagram-v2`: source -> state model -> Scene.

Parsing is mermaid's own, the way the flowchart's is: mermaid's compiled
state lexer (_state_lexer.py) driving a transcription of
stateDiagram.jison (state.lark, run from _state_tables.py). The actions
below build the statements jison's actions do, and `_StateDb` turns them
into nodes and edges the way mermaid's StateDB and dataFetcher do --
which is where most of what a state diagram means is decided: `[*]`
becomes a start or an end of the block it is in, a state given a second
description is drawn with a title, and a composite state becomes a frame
around its own states. The conformance corpus holds all of it to
mermaid's reading.

The model is drawn by the flowchart's scene code, since a state diagram
is a flowchart with a few more shapes: states are rounded boxes,
composite states are frames (`flowchart.Subgraph`, solid rather than
dashed), and a transition to a composite state ends at its frame, as an
edge to a subgraph does. Transitions are drawn at right angles, as
GitHub draws them: mermaid draws a state diagram's edges with the
flowchart's curve setting.

What isn't drawn, and so falls back to the source: notes, concurrent
regions (`--`), and a composite state laid out in another direction than
the diagram.
"""
import re

from . import flowchart
from . import styles
from .common import Invalid, Unsupported, curve_setting, decode_entities, label_lines, preprocess

# mermaid's directions -> Lectern's. A nested document's default is TB,
# like the diagram's (stateCommon's DEFAULT_NESTED_DOC_DIR).
_DIRECTIONS = {"TB": "TD", "BT": "BT", "LR": "LR", "RL": "RL"}

# mermaid's node shapes -> (the flowchart shape the layout meets, how
# it is drawn: flowchart.FlowNode.kind).
_SHAPES = {"rect": ("round", None), "rectWithTitle": ("round", "titled"),
           "stateStart": ("circle", "start"), "stateEnd": ("circle", "end"),
           "choice": ("diamond", "choice"), "fork": ("rect", "bar"), "join": ("rect", "bar")}

# Label text mermaid's markdown labels would draw as emphasis or code,
# which Lectern would show as written: refused rather than misdrawn.
_MARKDOWN_RE = re.compile(r"[*`\\]|(?<!\w)_|_(?!\w)")


# -- the statements, as stateDiagram.jison's actions build them ------------

def _trim_colon(text):
    return text[1:].strip() if text.startswith(":") else text.strip()


def _value(token):
    return token.value


class _Actions:
    """stateDiagram.jison's actions, one method per action name in
    state.lark. A statement that builds nothing (`hide empty
    description`, a bare `state X`, accessibility text) is None: jison
    keeps its token's text, which nothing downstream reads."""

    def __init__(self):
        self.dividers = 0

    def root(self, children):
        return children[1]

    def pass_on(self, children):
        return children[1]

    def doc_empty(self, children):
        return []

    def doc_line(self, children):
        document, line = children
        if line is not None:
            document.append(line)
        return document

    def line_spaced(self, children):
        return children[1]

    def line_plain(self, children):
        return children[0]

    def line_nl(self, children):
        return None

    def stmt_pass(self, children):
        return children[0]

    def stmt_none(self, children):
        return None

    def stmt_described(self, children):
        state, descr = children
        state["description"] = _trim_colon(descr.value)
        return state

    def stmt_relation(self, children):
        statement = {"stmt": "relation", "state1": children[0], "state2": children[2]}
        if len(children) == 4:
            statement["description"] = _trim_colon(children[3].value)
        return statement

    def stmt_composite(self, children):
        return {"stmt": "state", "id": children[0].value, "type": "default",
                "description": "", "doc": children[2]}

    def stmt_aliased(self, children):
        descr, _as, key = children
        key, description = key.value, descr.value.strip()
        if ":" in key:
            parts = key.split(":")
            key, description = parts[0], [description, parts[1]]
        return {"stmt": "state", "id": key, "type": "default", "description": description}

    def stmt_aliased_composite(self, children):
        descr, _as, key, _start, document, _stop = children
        return {"stmt": "state", "id": key.value, "type": "default",
                "description": descr.value, "doc": document}

    def _special(kind):
        def action(self, children):
            return {"stmt": "state", "id": children[0].value, "type": kind}
        return action

    stmt_fork = _special("fork")
    stmt_join = _special("join")
    stmt_choice = _special("choice")
    del _special

    def stmt_divider(self, children):
        self.dividers += 1
        return {"stmt": "state", "id": f"divider-id-{self.dividers}", "type": "divider"}

    def stmt_note(self, children):
        _note, position, key, text = children
        return {"stmt": "state", "id": key.value.strip(),
                "note": {"position": position.strip(), "text": text.value.strip()}}

    def note_position(self, children):
        return children[0].value

    def stmt_click(self, children):
        return {"stmt": "click", "id": children[1], "url": children[2].value,
                "tooltip": children[3].value}

    def stmt_click_href(self, children):
        return {"stmt": "click", "id": children[1], "url": children[3].value, "tooltip": ""}

    def class_def(self, children):
        return {"stmt": "classDef", "id": children[1].value.strip(),
                "classes": children[2].value.strip()}

    def style_def(self, children):
        return {"stmt": "style", "id": children[1].value.strip(),
                "styleClass": children[2].value.strip()}

    def apply_class(self, children):
        return {"stmt": "applyClass", "id": children[1].value.strip(),
                "styleClass": children[2].value.strip()}

    def _direction(value):
        def action(self, children):
            return {"stmt": "dir", "value": value}
        return action

    dir_tb = _direction("TB")
    dir_bt = _direction("BT")
    dir_rl = _direction("RL")
    dir_lr = _direction("LR")
    del _direction

    def id_plain(self, children):
        return {"stmt": "state", "id": children[0].value.strip(), "type": "default",
                "description": ""}

    def id_classed(self, children):
        return {"stmt": "state", "id": children[0].value.strip(),
                "classes": [children[2].value.strip()], "type": "default", "description": ""}


# -- what mermaid's StateDB makes of them ----------------------------------

def _translate(parent, node, first):
    """StateDB.docTranslator: `[*]` becomes the start (`<block>_start`) or
    the end (`<block>_end`) of the block it is in, depending on which end
    of a transition it is. Concurrent regions are left for the caller to
    refuse; mermaid's re-nesting of them isn't needed for that."""
    if node.get("stmt") == "relation":
        _translate(parent, node["state1"], True)
        _translate(parent, node["state2"], False)
        return
    if node.get("stmt") == "state":
        if node["id"] == "[*]":
            node["id"] = parent["id"] + ("_start" if first else "_end")
            node["start"] = first
        else:
            node["id"] = node["id"].strip()
    if node.get("stmt") not in ("root", "state") or node.get("doc") is None:
        return
    for child in node["doc"]:
        _translate(node, child, True)


def _direction(document, default="TB"):
    """getDir: a document's last `direction`, else the default."""
    direction = default
    for statement in document:
        if statement.get("stmt") == "dir":
            direction = statement["value"]
    return direction


class _StateDb:
    """What mermaid's StateDB records of the diagram's top level (its
    `extract`), which is where classes, styles and links come from -- only
    the top level's statements are read; a `class` inside a composite
    state does nothing in mermaid either."""

    def __init__(self):
        self.states = {}        # id -> {"type", "doc", "classes", "styles"}
        self.classes = {}       # classDef name -> its declarations
        self.links = {}         # state id -> url

    def add_state(self, key, kind="default", doc=None, classes=None, styles=None):
        key = key.strip()
        state = self.states.get(key)
        if state is None:
            state = self.states[key] = {"type": kind, "doc": doc, "classes": [], "styles": []}
        else:
            if state["doc"] is None:
                state["doc"] = doc
            if not state["type"]:
                state["type"] = kind
        for name in classes or []:
            self.set_class(key, name.strip())
        for style in styles or []:
            state["styles"].append(style.strip())

    def set_class(self, keys, name):
        for key in keys.split(","):
            if key not in self.states:
                self.add_state(key)
            self.states[key.strip()]["classes"].append(name)

    def style(self, keys, declared):
        for key in keys.strip().split(","):
            if key not in self.states:
                self.add_state(key)
            self.states[key.strip()]["styles"] = [
                part.replace(";", "").strip() for part in declared.split(",")]

    def add_class(self, name, declared):
        found = self.classes.setdefault(name, [])
        for attribute in declared.split(",") if declared else []:
            found.append(re.sub(r"([^;]*);", r"\1", attribute, count=1).strip())

    def extract(self, document):
        for item in document:
            stmt = item.get("stmt")
            if stmt == "state":
                self.add_state(item["id"], item.get("type"), item.get("doc"))
            elif stmt == "relation":
                for end in (item["state1"], item["state2"]):
                    self.add_state(end["id"], end.get("type"), end.get("doc"),
                                   end.get("classes"), end.get("styles"))
            elif stmt == "classDef":
                self.add_class(item["id"], item["classes"])
            elif stmt == "style":
                self.style(item["id"], item["styleClass"])
            elif stmt == "applyClass":
                self.set_class(item["id"], item["styleClass"])
            elif stmt == "click":
                self.links[item["id"]["id"]] = item["url"]


class _Fetcher:
    """mermaid's dataFetcher: the statement tree walked into the nodes and
    edges its renderer lays out. A node is made the first time its id is
    met anywhere in the tree, and later mentions add to it."""

    def __init__(self, db):
        self.db = db
        self.seen = {}          # dataFetcher's nodeDb
        self.nodes = []         # in mermaid's order, each a dict
        self.edges = []         # (start, end, label)

    def document(self, parent, document):
        for item in document:
            stmt = item.get("stmt")
            if stmt in ("state", "default"):
                self.item(parent, item)
            elif stmt == "relation":
                self.item(parent, item["state1"])
                self.item(parent, item["state2"])
                self.edges.append((item["state1"]["id"], item["state2"]["id"],
                                   item.get("description") or ""))

    def item(self, parent, item):
        key = item["id"]
        state = self.db.states.get(key)
        if key != "root":
            self._node(parent, item, key, state)
        if item.get("doc") is not None:
            self.document(item, item["doc"])

    def _node(self, parent, item, key, state):
        shape = "rect"
        if item.get("start") is True:
            shape = "stateStart"
        elif item.get("start") is False:
            shape = "stateEnd"
        if item.get("type") != "default":
            shape = item.get("type")
        node = self.seen.get(key)
        if node is None:
            node = self.seen[key] = {"shape": shape, "description": key, "type": None,
                                     "dir": None, "classes": list(state["classes"]) if state else [],
                                     "styles": state["styles"] if state else []}
        description = item.get("description")
        if description:
            if isinstance(description, list):
                raise Unsupported("a state description with a colon in its id")
            if isinstance(node["description"], list):
                node["shape"] = "rectWithTitle"
                node["description"].append(description)
            elif node["description"]:
                node["shape"] = "rectWithTitle"
                node["description"] = ([description] if node["description"] == key
                                       else [node["description"], description])
            else:
                node["shape"] = "rect"
                node["description"] = description
        if len(node["description"]) == 1 and node["shape"] == "rectWithTitle":
            node["shape"] = "roundedWithTitle" if node["type"] == "group" else "rect"
        if node["type"] is None and item.get("doc") is not None:
            node["type"] = "group"
            node["dir"] = _direction(item["doc"])
            node["shape"] = "divider" if item.get("type") == "divider" else "roundedWithTitle"
        data = {"id": key, "shape": node["shape"], "label": node["description"],
                "dir": node["dir"], "group": node["type"] == "group",
                "classes": node["classes"], "styles": node["styles"]}
        if data["shape"] == "divider":
            data["label"] = ""
        if parent is not None and parent["id"] != "root":
            data["parent"] = parent["id"]
        if item.get("note"):
            raise Unsupported("notes")
        if key in ("</join></fork>", "</choice>"):
            return
        for existing in self.nodes:
            if existing["id"] == key:
                existing.update(data)
                return
        self.nodes.append(data)


class Model:
    """A state diagram as mermaid's renderer is handed it: nodes (dicts of
    id, shape, label, description, parent, ...) and edges (start, end,
    label), with the diagram's direction and links. `build_scene` draws
    it."""

    def __init__(self, direction, nodes, edges, links, classes, curve):
        self.direction = direction
        self.nodes = nodes
        self.edges = edges
        self.links = links
        self.classes = classes
        self.curve = curve

    def build_scene(self, measurer):
        return self.chart().build_scene(measurer)

    def chart(self):
        """The diagram as a flowchart.Flowchart, which is what draws it."""
        nodes, blocks = {}, {}
        for data in self.nodes:
            if data["group"]:
                blocks[data["id"]] = flowchart.Subgraph(data["id"], _label(data["label"]))
                blocks[data["id"]].dashed = False
                blocks[data["id"]].style = self._style(data)
                if _DIRECTIONS[data["dir"]] != self.direction:
                    raise Unsupported("a composite state laid out in another direction")
            # A composite state stands in the node list as itself until
            # the edges to it have been turned into edges to its frame.
            shape, kind = ("rect", None) if data["group"] else _SHAPES.get(data["shape"], (None, None))
            if shape is None:
                raise Unsupported(f"no drawing for the {data['shape']!r} state shape")
            # A pseudo-state's label is its id, which is never shown.
            shown = kind not in ("start", "end", "choice", "bar")
            node = flowchart.FlowNode(data["id"], _label(data["label"]) if shown else "",
                                      shape, kind)
            if kind == "titled":
                node.body = "\n".join(_label(d) for d in data["description"])
            node.style = self._style(data)
            node.href = self.links.get(data["id"])
            nodes[data["id"]] = node
        for data in self.nodes:
            parent = data.get("parent")
            if parent is None:
                continue
            if parent not in blocks:
                raise Unsupported(f"{data['id']!r} is inside a state that isn't composite")
            if data["group"]:
                blocks[data["id"]].parent = blocks[parent]
                blocks[parent].children.append(blocks[data["id"]])
            else:
                blocks[parent].members.append(data["id"])
        edges = [flowchart.FlowEdge(start, end, _label(label), "solid", "arrow", None)
                 for start, end, label in self.edges]
        roots = [block for block in blocks.values() if block.parent is None]
        flowchart._attach_cluster_edges(nodes, edges, roots, declared=set())
        _lay_out_through_the_block(edges, blocks)
        return flowchart.Flowchart(self.direction, nodes, edges, roots, self.curve)

    def _style(self, data):
        declared = []
        for name in data["classes"]:
            declared += self.classes.get(name, [])
        return styles.node_style(styles.declarations(declared + data["styles"]))


def _lay_out_through_the_block(edges, blocks):
    """Lay a transition to a composite state out to the state its own
    transitions start from, and one leaving it out from the state they
    lead to last.

    A flowchart lays an edge to a subgraph out to the block's first node,
    which for a state diagram is its `[*]` start: a transition leaving the
    block then pulled the next state up level with the block's insides,
    beside the frame rather than after it. mermaid lays a composite state
    out as one box, so whatever follows it comes after all of it."""
    inner = [(edge.src, edge.dst) for edge in edges
             if not (edge.src_cluster or edge.dst_cluster) and edge.src != edge.dst]
    for edge in edges:
        for end, deepest in (("src", True), ("dst", False)):
            cluster = getattr(edge, end + "_cluster")
            if cluster is None:
                continue
            keys = blocks[cluster].node_keys()
            depth = _depths(keys, inner)
            pick = max(keys, key=lambda k: (depth[k], keys.index(k))) if deepest \
                else min(keys, key=lambda k: (depth[k], keys.index(k)))
            setattr(edge, end, pick)


def _depths(keys, pairs):
    """Each of `keys`' longest path from a state nothing inside leads to,
    along the transitions among them. A cycle is followed round once at
    most, so it can't run away."""
    inside = set(keys)
    pairs = [(a, b) for a, b in pairs if a in inside and b in inside]
    depth = dict.fromkeys(keys, 0)
    for _round in range(len(keys)):
        changed = False
        for a, b in pairs:
            if depth[a] + 1 > depth[b] and depth[a] + 1 < len(keys):
                depth[b] = depth[a] + 1
                changed = True
        if not changed:
            break
    return depth


def _label(text):
    text = label_lines(decode_entities(text))
    if _MARKDOWN_RE.search(text):
        raise Unsupported("markdown in a state diagram label")
    return text


def _refuse_regions(document):
    for item in document:
        if item.get("type") == "divider":
            raise Unsupported("concurrent regions (`--`)")
        if item.get("doc") is not None:
            _refuse_regions(item["doc"])


def read(source):
    """Mermaid state diagram source -> (direction, nodes, edges, links,
    classes) as mermaid's renderer gets them, nodes as dicts. Raises
    Invalid where mermaid's parser would fail."""
    from . import _state_lexer, _state_tables, parsing
    actions = _Actions()
    document = parsing.parse(_state_tables, parsing.lex(_state_lexer, preprocess(source)), actions)
    root = {"id": "root", "stmt": "root", "doc": document}
    _translate(root, root, True)
    db = _StateDb()
    db.extract(document)
    fetcher = _Fetcher(db)
    fetcher.item(None, root)
    nodes = fetcher.nodes
    for node in nodes:
        label = node["label"]
        if isinstance(label, list):
            node["description"] = label[1:]
            if node["group"] and node["description"]:
                raise Invalid(f"a composite state with a description: {node['id']!r}")
            node["label"] = label[0]
        else:
            node["description"] = []
    links = {key: url.strip('"') for key, url in db.links.items()
             if any(node["id"] == key for node in nodes)}
    return _direction(document), nodes, fetcher.edges, links, db.classes, document


def parse(source):
    """Mermaid state diagram source -> Model. Raises Unsupported."""
    direction, nodes, edges, links, classes, document = read(source)
    _refuse_regions(document)
    if not nodes:
        raise Unsupported("no states")
    model = Model(_DIRECTIONS[direction], nodes, edges, links, classes,
                  curve_setting(source, "flowchart", flowchart.DEFAULT_CURVE))
    model.chart()       # what the drawing would refuse, refused now
    return model

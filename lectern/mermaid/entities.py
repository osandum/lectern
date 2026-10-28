"""`classDiagram` and `erDiagram`: two syntaxes, one drawing.

Both are boxes with a title compartment over a list of members, joined by
typed relations, so they share everything below the parsers -- sizing, the
layered layout, and the compartment drawing. What differs is only what the
compartments hold (fields and methods, or columns) and which glyphs sit at
the ends of a relation: UML's triangles and diamonds, or ER's crow's feet.
"""
import re

from . import layered
from . import scene as sc
from .common import Unsupported, parse_click, parse_label

# -- geometry, in nominal 96dpi pixels -------------------------------------

BOX_PAD_X = 12.0
BOX_PAD_Y = 7.0
MIN_BOX_W = 90.0
MEMBER_GAP = 3.0
LABEL_PAD = 3.0
LABEL_SCALE = 0.92
RANK_GAP = 74.0               # room for a label plate and two cardinalities
CARDINALITY_OFFSET = 12.0     # along the line, past the end glyph
CARDINALITY_SIDE_OFFSET = 9.0  # beside it, clear of the relation's label

_CLASS_HEADER_RE = re.compile(r"^classdiagram(?:-v2)?$")
_ER_HEADER_RE = re.compile(r"^erdiagram$")
_DIRECTION_RE = re.compile(r"^direction\s+(?P<dir>TB|TD|BT|LR|RL)$", re.I)

# UML relation ends. Longest first: "<|" has to beat "<", or an
# inheritance arrow parses as a plain association.
_UML_TAILS = [("<|", "triangle"), ("*", "diamond"), ("o", "diamond-open"), ("<", "arrow")]
_UML_HEADS = [("|>", "triangle"), ("*", "diamond"), ("o", "diamond-open"), (">", "arrow")]

# ER cardinalities. The left symbol reads outward from the line, the right
# one inward, which is why the two tables aren't the same one reversed.
_ER_LEFT = {"||": "one", "|o": "zero-one", "}o": "zero-many", "}|": "one-many"}
_ER_RIGHT = {"||": "one", "o|": "zero-one", "o{": "zero-many", "|{": "one-many"}

_CLASS_RELATION_RE = re.compile(r"""
    ^(?P<src>[A-Za-z_][\w]*)\s*
    (?:"(?P<src_card>[^"]*)"\s*)?
    (?P<relation>[-.<>|*o]{2,})\s*
    (?:"(?P<dst_card>[^"]*)"\s*)?
    (?P<dst>[A-Za-z_][\w]*)\s*
    (?::\s*(?P<label>.*))?$
""", re.X)

_ER_RELATION_RE = re.compile(r"""
    ^(?P<src>[A-Za-z_][\w-]*)\s+
    (?P<relation>[|}o{.\-]{4,})\s+
    (?P<dst>[A-Za-z_][\w-]*)\s*
    (?::\s*(?P<label>.*))?$
""", re.X)

_MEMBER_RE = re.compile(r"^(?P<key>[A-Za-z_][\w]*)\s*:\s*(?P<member>.+)$")
_STEREOTYPE_RE = re.compile(r"^<<(?P<name>.+)>>$")
# `class Id`, with optional `~Generic~` and `["Label"]`, then nothing, a
# `{` opening a member block, a whole one-line `{ member }` block, or a
# `:::style` shorthand (styling is ignored, as in flowcharts). Mermaid's
# lexer reads a one-line block's text as a single member.
_CLASS_DECL_RE = re.compile(r"""
    ^class \s+ (?P<key>[A-Za-z_]\w*)
    (?: ~ (?P<generic>[^~]+) ~ )?
    \s* (?: \[ \s* "(?P<label>[^"]*)" \s* \] )?
    \s* (?: :::[\w-]+ )?
    \s* (?: (?P<open>\{) \s* (?: (?P<body>[^{}]*?) \s* (?P<close>\}) )? )?
    $
""", re.X | re.I)


# `ENTITY {` opening an ER entity's attributes, with an optional
# `[alias]`, and a `:::style` shorthand that is styling and ignored.
_ER_BLOCK_RE = re.compile(
    r"^(?P<key>[A-Za-z_][\w-]*)(?:\s*\[\s*(?P<alias>[^\]]*?)\s*\])?(?::::[\w,-]+)?\s*\{$")

# `click A callback`, `click A call fn(args)`, with an optional tooltip:
# the forms that name page JavaScript rather than a link.
_CALLBACK_RE = re.compile(
    r'^click\s+[\w-]+(?:~[^~\s]*~)?\s+(?:call\s+[\w$.]+\([^)]*\)|[\w$.]+)(?:\s+"[^"]*")?$', re.I)

# Accessibility metadata: read by mermaid, drawn by no one.
_ACC_LINE_RE = re.compile(r"^acc(?:Title|Descr)\s*:", re.I)
_ACC_BLOCK_RE = re.compile(r"^accDescr\s*\{", re.I)


class Member:
    """A line in a box's member list, as mermaid shows it: a class member
    abstract (`*`, italic) or static (`$`, underlined), or an ER
    attribute, which is neither."""
    __slots__ = ("text", "italic", "underline")

    def __init__(self, text, italic=False, underline=False):
        self.text = text
        self.italic = italic
        self.underline = underline

    def __repr__(self):
        return f"Member({self.text!r})"


_VISIBILITY = ("+", "-", "#", "~")


def _generics(text):
    """`~T~` as `<T>`, the way mermaid's parseGenericTypes reads them:
    within each comma-separated piece -- or across a comma, where it
    splits a `~K, V~` -- the outermost pair of tildes at a time."""
    pieces = re.split(r"(,)", text)
    out = []
    index = 0
    while index < len(pieces):
        piece = pieces[index]
        if (piece == "," and 0 < index < len(pieces) - 1
                and pieces[index - 1].count("~") == 1 and pieces[index + 1].count("~") == 1):
            piece = out.pop() + "," + pieces[index + 1]
            index += 1
        if piece.count("~") > 1:
            starts_with_tilde = piece.count("~") % 2 and piece.startswith("~")
            chars = list(piece[1:] if starts_with_tilde else piece)
            while "~" in chars:
                first = chars.index("~")
                last = len(chars) - 1 - chars[::-1].index("~")
                if first == last:
                    break
                chars[first], chars[last] = "<", ">"
            piece = ("~" if starts_with_tilde else "") + "".join(chars)
        out.append(piece)
        index += 1
    return "".join(out)


def _class_member(raw):
    """A class member as mermaid shows it (classTypes.ClassMember):
    visibility, name, a method's `(parameters) : return type`, generics
    as `<T>`, and a trailing `*` or `$` turned into italics or underlining
    rather than shown."""
    text = raw.strip()
    visibility = classifier = ""
    if text.find(")") > 0:
        match = re.search(r"([#+~-])?(.+)\((.*)\)([\s$*])?(.*)([$*])?", text)
        if not match:
            raise Unsupported(f"unparsed method {text!r}")
        visibility = match.group(1) or ""
        name = match.group(2)
        parameters = (match.group(3) or "").strip()
        classifier = (match.group(4) or "").strip()
        returns = (match.group(5) or "").strip()
        if not classifier and returns[-1:] in ("$", "*"):
            classifier, returns = returns[-1], returns[:-1]
    else:
        if text[:1] in _VISIBILITY:
            visibility = text[0]
        if text[-1:] in ("$", "*"):
            classifier = text[-1]
        name = text[len(visibility):len(text) - len(classifier)]
    name = " " + name.strip() if name.startswith(" ") else name.strip()
    shown = visibility + _generics(name)
    if text.find(")") > 0:
        shown += f"({_generics(parameters)})"
        if returns:
            shown += " : " + _generics(returns)
    return Member(shown.strip(), italic=classifier == "*", underline=classifier == "$")


def _er_attribute(raw):
    """An ER attribute as written, less the backquotes mermaid lets a
    type or name be quoted in."""
    return Member(re.sub(r"`([^`]*)`", r"\1", parse_label(raw)))


class Entity:
    """One box: a title, an optional «stereotype», and a list of members."""
    __slots__ = ("key", "title", "stereotype", "members", "href", "w", "h", "x", "y",
                 "rank", "order", "title_h")

    def __init__(self, key, title=None):
        self.key = key
        self.title = title if title is not None else key
        self.stereotype = None
        self.members = []
        self.href = None        # classDiagram `click`/`link`
        self.w = self.h = self.title_h = 0.0
        self.x = self.y = 0.0
        self.rank = 0
        self.order = 0


class Relation:
    __slots__ = ("src", "dst", "label", "dashed", "tail", "head", "src_card", "dst_card")

    def __init__(self, src, dst, *, label="", dashed=False, tail=None, head=None,
                 src_card="", dst_card=""):
        self.src = src
        self.dst = dst
        self.label = label
        self.dashed = dashed
        self.tail = tail        # glyph at the source end
        self.head = head        # glyph at the target end
        self.src_card = src_card
        self.dst_card = dst_card


class EntityDiagram:
    def __init__(self, entities, relations, direction="TD"):
        self.entities = entities
        self.relations = relations
        self.direction = direction
        # Mermaid has no setting for these; it rounds them like a
        # flowchart's default edges.
        self.curve = layered.DEFAULT_CURVE

    def build_scene(self, measurer):
        return _build_scene(self, measurer)


def _statements(source):
    for raw_line in source.splitlines():
        line = raw_line.split("%%")[0].strip().rstrip(";")
        if line:
            yield line


def _decode_uml_relation(token):
    """`<|--`, `*..`, `-->` ... -> (tail glyph, head glyph, dashed)."""
    tail = head = None
    for prefix, glyph in _UML_TAILS:
        if token.startswith(prefix):
            tail, token = glyph, token[len(prefix):]
            break
    for suffix, glyph in _UML_HEADS:
        if token.endswith(suffix):
            head, token = glyph, token[:-len(suffix)]
            break
    if token in ("--", "---"):
        return tail, head, False
    if token in ("..", "..."):
        return tail, head, True
    raise Unsupported(f"unknown relation {token!r}")


def _decode_er_relation(token):
    for separator, dashed in (("--", False), ("..", True)):
        index = token.find(separator)
        if index <= 0:
            continue
        left, right = token[:index], token[index + len(separator):]
        if left in _ER_LEFT and right in _ER_RIGHT:
            return _ER_LEFT[left], _ER_RIGHT[right], dashed
    raise Unsupported(f"unknown ER relation {token!r}")


def _parse_common(source, header_re, kind):
    """The shared skeleton of both parsers: header, `direction`, blocks of
    members in braces, relations, and one-line member declarations."""
    lines = list(_statements(source))
    if not lines or not header_re.match(lines[0].lower()):
        raise Unsupported(f"not a {kind} header")

    entities = {}
    relations = []
    direction = "TD"
    open_entity = None
    in_description = False      # inside an `accDescr { ... }`
    hrefs = {}          # entity key -> link target, applied once all are read
    member = _class_member if kind == "classDiagram" else _er_attribute

    def touch(key, title=None):
        entity = entities.get(key)
        if entity is None:
            entity = entities[key] = Entity(key, title)
        elif title is not None:
            entity.title = title
        return entity

    for line in lines[1:]:
        if in_description:
            in_description = not line.endswith("}")
            continue
        if open_entity is not None:
            if line == "}":
                open_entity = None
                continue
            stereotype = _STEREOTYPE_RE.match(line)
            if stereotype:
                open_entity.stereotype = stereotype.group("name").strip()
            else:
                open_entity.members.append(member(line))
            continue

        if _ACC_LINE_RE.match(line):
            continue
        if _ACC_BLOCK_RE.match(line):
            in_description = not line.endswith("}")
            continue

        directive = _DIRECTION_RE.match(line)
        if directive:
            raw = directive.group("dir").upper()
            direction = "TD" if raw == "TB" else raw
            continue

        if kind == "classDiagram" and line.lower().startswith("namespace"):
            # A frame around the classes in it, which isn't drawn yet --
            # and read as anything else, its `class` lines become members.
            raise Unsupported("namespace")

        if kind == "classDiagram" and line.lower().startswith("class "):
            declaration = _CLASS_DECL_RE.match(line)
            if not declaration:
                raise Unsupported(f"unparsed class declaration {line!r}")
            key = declaration.group("key")
            # Only a line that gives a label sets the title, so a later
            # bare `class Id {` doesn't undo an earlier `class Id["..."]`.
            # A label of only spaces is a blank title, as in mermaid.
            label = declaration.group("label")
            title = parse_label(label) if label is not None else None
            if declaration.group("generic"):
                title = f"{title if title is not None else key}<{declaration.group('generic').strip()}>"
            entity = touch(key, title)
            if declaration.group("close"):
                body = parse_label(declaration.group("body"))
                stereotype = _STEREOTYPE_RE.match(body)
                if stereotype:
                    entity.stereotype = stereotype.group("name").strip()
                elif body:
                    entity.members.append(member(body))
            elif declaration.group("open"):
                open_entity = entity
            continue

        if kind == "erDiagram" and line.endswith("{"):
            # `ENTITY {` -- the brace opens its attributes. (A class's
            # block needs `class` before it; mermaid refuses one without.)
            block = _ER_BLOCK_RE.match(line)
            if not block:
                raise Unsupported(f"unparsed entity opening {line!r}")
            alias = block.group("alias")
            open_entity = touch(block.group("key"), parse_label(alias) if alias else block.group("key"))
            continue

        parsed = _parse_relation(line, kind)
        if parsed is not None:
            relation, src, dst = parsed
            touch(src)
            touch(dst)
            relations.append(relation)
            continue

        if kind == "classDiagram":
            # Only classDiagram has links: mermaid's erDiagram grammar has
            # no `click`, and reads one as entity names instead.
            if line.lower().startswith(("click ", "link ")):
                # A generic class is linked by its id with the generic on.
                link = parse_click(re.sub(r"^((?:click|link)\s+[\w-]+)~[^~\s]*~", r"\1", line,
                                          flags=re.I))
                if link is not None:
                    hrefs[link[0]] = link[1]
                elif not _CALLBACK_RE.match(line):
                    raise Unsupported(f"unparsed {line.split()[0]} {line!r}")
                # A callback names page JavaScript, and is ignored like a style.
                continue
            one = _MEMBER_RE.match(line)
            if one:
                # `Animal : +swim()` -- the one-member-at-a-time spelling.
                entity = touch(one.group("key"))
                text = parse_label(one.group("member"))
                stereotype = _STEREOTYPE_RE.match(text)
                if stereotype:
                    entity.stereotype = stereotype.group("name").strip()
                else:
                    entity.members.append(member(text))
                continue
        raise Unsupported(f"unparsed statement {line!r}")

    if open_entity is not None:
        raise Unsupported("unclosed block")
    if not entities:
        raise Unsupported("no entities")
    for key, href in hrefs.items():
        if key in entities:
            entities[key].href = href
    return EntityDiagram(entities, relations, direction)


def _parse_relation(line, kind):
    if kind == "classDiagram":
        match = _CLASS_RELATION_RE.match(line)
        if not match:
            return None
        tail, head, dashed = _decode_uml_relation(match.group("relation"))
        relation = Relation(
            match.group("src"), match.group("dst"),
            label=parse_label(match.group("label") or ""), dashed=dashed,
            tail=tail, head=head,
            src_card=(match.group("src_card") or "").strip(),
            dst_card=(match.group("dst_card") or "").strip(),
        )
    else:
        match = _ER_RELATION_RE.match(line)
        if not match:
            return None
        tail, head, dashed = _decode_er_relation(match.group("relation"))
        relation = Relation(
            match.group("src"), match.group("dst"),
            label=parse_label(match.group("label") or ""), dashed=dashed,
            tail=tail, head=head,
        )
    return relation, relation.src, relation.dst


def parse_class(source):
    return _parse_common(source, _CLASS_HEADER_RE, "classDiagram")


def parse_er(source):
    return _parse_common(source, _ER_HEADER_RE, "erDiagram")


# -- layout ----------------------------------------------------------------

def _size_entity(entity, measurer):
    title_w, title_h = measurer.size(entity.title, bold=True, wrap=False)
    width = title_w
    height = title_h + 2 * BOX_PAD_Y
    if entity.stereotype:
        stereo_w, stereo_h = measurer.size(
            f"«{entity.stereotype}»", wrap=False, scale=LABEL_SCALE)
        width = max(width, stereo_w)
        height += stereo_h + MEMBER_GAP
    entity.title_h = height
    for member in entity.members:
        member_w, member_h = measurer.size(member.text, italic=member.italic, wrap=False,
                                           scale=LABEL_SCALE)
        width = max(width, member_w)
        height += member_h + MEMBER_GAP
    if entity.members:
        height += 2 * BOX_PAD_Y - MEMBER_GAP
    entity.w = max(MIN_BOX_W, width + 2 * BOX_PAD_X)
    entity.h = height


def _entity_shapes(entity, measurer, scene):
    x = entity.x - entity.w / 2
    y = entity.y - entity.h / 2
    scene.add(sc.Rect(x, y, entity.w, entity.h, radius=4.0,
                      fill=sc.NODE, stroke=sc.EDGE))
    if entity.href:
        scene.add(sc.Link(x, y, entity.w, entity.h, entity.href))
    cursor = y + BOX_PAD_Y
    title_w, title_h = measurer.size(entity.title, bold=True, wrap=False)
    scene.add(sc.Text(entity.x - title_w / 2, cursor, title_w, title_h,
                      entity.title, bold=True))
    cursor += title_h
    if entity.stereotype:
        stereo = f"«{entity.stereotype}»"
        stereo_w, stereo_h = measurer.size(stereo, wrap=False, scale=LABEL_SCALE)
        cursor += MEMBER_GAP
        scene.add(sc.Text(entity.x - stereo_w / 2, cursor, stereo_w, stereo_h,
                          stereo, color=sc.DIM, scale=LABEL_SCALE))
        cursor += stereo_h
    if not entity.members:
        return
    divider_y = y + entity.title_h
    scene.add(sc.Line([(x, divider_y), (x + entity.w, divider_y)], stroke=sc.EDGE))
    cursor = divider_y + BOX_PAD_Y
    for member in entity.members:
        member_w, member_h = measurer.size(member.text, italic=member.italic, wrap=False,
                                           scale=LABEL_SCALE)
        scene.add(sc.Text(x + BOX_PAD_X, cursor, member_w, member_h, member.text,
                          italic=member.italic, underline=member.underline,
                          align="left", scale=LABEL_SCALE))
        cursor += member_h + MEMBER_GAP


def _plate_label(scene, measurer, text, cx, cy, *, color=sc.DIM, plate=True):
    text_w, text_h = measurer.size(text, wrap=False, scale=LABEL_SCALE)
    if plate:
        scene.add(sc.Rect(cx - text_w / 2 - LABEL_PAD, cy - text_h / 2 - LABEL_PAD,
                          text_w + 2 * LABEL_PAD, text_h + 2 * LABEL_PAD,
                          radius=3.0, fill=sc.LABEL_BG))
    scene.add(sc.Text(cx - text_w / 2, cy - text_h / 2, text_w, text_h,
                      text, color=color, scale=LABEL_SCALE))


def _cardinality_position(points, from_start, distance, offset):
    """Where a cardinality sits: `distance` in from its own end of the
    relation, then `offset` to one side of it.

    Both parts matter. Along the line it has to clear the arrowhead or
    crow's foot drawn at that end; beside the line it has to clear the
    relation's own label, which sits centred on the longest segment and
    would otherwise collide with it on any short relation.
    """
    (x0, y0), (x1, y1) = ((points[0], points[1]) if from_start
                          else (points[-1], points[-2]))
    length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 or 1.0
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    ratio = min(distance, length * 0.45)
    return x0 + ux * ratio - uy * offset, y0 + uy * ratio + ux * offset


def _build_scene(diagram, measurer):
    for entity in diagram.entities.values():
        _size_entity(entity, measurer)

    # RANK_GAP already holds one label and the cardinalities at both ends;
    # what a relation adds is its plate and the clearance place_labels keeps
    # around it, so that several labels fanning out of one entity into the
    # same gap can stack along it instead of the gap fitting just one. That
    # only bites past two of them, so most diagrams lay out as before.
    vertical = diagram.direction in layered.VERTICAL_DIRECTIONS
    edge_room, label_sizes = {}, {}
    for index, relation in enumerate(diagram.relations):
        if relation.label:
            text_w, text_h = measurer.size(relation.label, wrap=False, scale=LABEL_SCALE)
            edge_room[index] = ((text_h if vertical else text_w)
                                + 2 * LABEL_PAD + 2 * layered.LABEL_CLEARANCE)
            label_sizes[index] = (text_w + 2 * LABEL_PAD, text_h + 2 * LABEL_PAD)
    cells, routes, width, height = layered.layout(
        diagram.entities, diagram.relations, diagram.direction,
        rank_gap=RANK_GAP, edge_room=edge_room, label_sizes=label_sizes)

    scene = sc.Scene(width, height, [], measurer.font_px)

    # Lines and cardinalities first, then the labels: a label is the one
    # thing here that can move, so it is placed around everything else
    # (layered.place_labels), and drawn after every line so no later line
    # crosses its plate.
    obstacles = [(e.x - e.w / 2, e.y - e.h / 2, e.w, e.h) for e in diagram.entities.values()]
    labels = []
    for index, relation in enumerate(diagram.relations):
        if relation.src == relation.dst:
            points = layered.self_loop_points(cells[relation.src])
        else:
            points = layered.edge_points(cells, routes, index, relation)
        drawn = layered.curved(points, diagram.curve, [
            (e.x - e.w / 2, e.y - e.h / 2, e.w, e.h) for key, e in diagram.entities.items()
            if key not in (relation.src, relation.dst)])
        scene.add(sc.Line(drawn, dashed=relation.dashed,
                          head=relation.head, tail=relation.tail))
        if relation.label:
            labels.append((points, drawn, relation.label,
                           layered.label_anchor(cells, routes, index)))
        for from_start, cardinality in ((True, relation.src_card),
                                        (False, relation.dst_card)):
            if not cardinality:
                continue
            cx, cy = _cardinality_position(
                points, from_start, CARDINALITY_OFFSET + measurer.font_px,
                CARDINALITY_SIDE_OFFSET)
            _plate_label(scene, measurer, cardinality, cx, cy, plate=False)
            text_w, text_h = measurer.size(cardinality, wrap=False, scale=LABEL_SCALE)
            obstacles.append((cx - text_w / 2, cy - text_h / 2, text_w, text_h))

    requests = []
    for points, drawn, label, anchor in labels:
        text_w, text_h = measurer.size(label, wrap=False, scale=LABEL_SCALE)
        requests.append((points, text_w + 2 * LABEL_PAD, text_h + 2 * LABEL_PAD, drawn, anchor))
    for (_points, _drawn, label, _anchor), (cx, cy) in zip(labels, layered.place_labels(requests, obstacles)):
        _plate_label(scene, measurer, label, cx, cy)

    for entity in diagram.entities.values():
        _entity_shapes(entity, measurer, scene)

    return scene.normalize()

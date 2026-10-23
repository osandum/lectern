"""`sequenceDiagram`: source -> participants, messages and blocks -> Scene.

Nothing here needs the graph machinery flowchart.py has: the vertical axis
is simply the order the statements were written in, and the horizontal one
is fixed by the participant columns. What it does need, and flowcharts
don't, is a *stack* -- `loop`/`alt`/`opt`/`par` frames nest, and a frame's
height isn't known until its `end` turns up, so frames are opened during
the row walk and only turned into geometry when they close.
"""
import re

from . import scene as sc
from .common import Unsupported, parse_label

# -- geometry, in nominal 96dpi pixels -------------------------------------

BOX_PAD_X = 14.0
BOX_PAD_Y = 8.0
MIN_BOX_W = 66.0
COLUMN_GAP = 34.0          # minimum clear space between two boxes
HEAD_GAP = 18.0            # between the participant boxes and the first row
ROW_PAD = 16.0             # under a message's label, above its line
SELF_MESSAGE_W = 42.0      # how far a self-call juts out from its lifeline
SELF_MESSAGE_H = 34.0
NOTE_PAD_X = 10.0
NOTE_PAD_Y = 7.0
ACTIVATION_W = 9.0
FRAME_PAD = 12.0           # around the rows a loop/alt frame encloses
FRAME_LABEL_PAD = 4.0
LABEL_SCALE = 0.92
LIFELINE_TAIL = 12.0

# `->>` and friends. The line is dotted when the dashes are doubled, and
# the glyph at the end is what the last character says: mermaid gives `->`
# no arrowhead at all, which is why a single ">" maps to None rather than
# to a smaller arrow.
_ARROWS = {">>": "arrow", ")": "open", "x": "cross", ">": None}

_MESSAGE_RE = re.compile(r"""
    ^(?P<src>[A-Za-z0-9_]+)\s*
    (?P<dashes>-{1,2})(?P<glyph>>>|>|x|\))\s*
    (?P<activation>[+-])?\s*
    (?P<dst>[A-Za-z0-9_]+)\s*
    :\s*(?P<text>.*)$
""", re.X)

_PARTICIPANT_RE = re.compile(
    r"^(?:participant|actor)\s+(?P<key>[A-Za-z0-9_]+)(?:\s+as\s+(?P<label>.+))?$")
_NOTE_RE = re.compile(
    r"^note\s+(?P<where>left of|right of|over)\s+(?P<who>[^:]+):\s*(?P<text>.*)$",
    re.I)
_ACTIVATE_RE = re.compile(r"^(?P<verb>activate|deactivate)\s+(?P<key>[A-Za-z0-9_]+)$")

# Frames. `par` uses "and" where `alt` uses "else"; both mean "divider,
# new compartment, same frame".
_BLOCK_OPENERS = ("loop", "alt", "opt", "par", "critical", "break", "rect")
_BLOCK_DIVIDERS = ("else", "and", "option")

# Statements that are accepted and have no effect on what gets drawn.
_IGNORED = ("autonumber", "%%")


class Participant:
    __slots__ = ("key", "label", "x", "w", "h")

    def __init__(self, key, label):
        self.key = key
        self.label = label
        self.x = self.w = self.h = 0.0


class Message:
    __slots__ = ("src", "dst", "text", "dotted", "head", "activation")

    def __init__(self, src, dst, text, dotted, head, activation):
        self.src = src
        self.dst = dst
        self.text = text
        self.dotted = dotted
        self.head = head
        # "+" activates the target as the message arrives, "-" deactivates
        # the source as it leaves -- mermaid's shorthand for a matching
        # activate/deactivate pair.
        self.activation = activation


class Note:
    __slots__ = ("placement", "keys", "text")

    def __init__(self, placement, keys, text):
        self.placement = placement      # left | right | over
        self.keys = keys
        self.text = text


class Activation:
    __slots__ = ("key", "start")

    def __init__(self, key, start):
        self.key = key
        self.start = start              # True = activate, False = deactivate


class BlockStart:
    __slots__ = ("kind", "label")

    def __init__(self, kind, label):
        self.kind = kind
        self.label = label


class BlockDivider:
    __slots__ = ("label",)

    def __init__(self, label):
        self.label = label


class BlockEnd:
    __slots__ = ()


class SequenceDiagram:
    def __init__(self, participants, events):
        self.participants = participants        # ordered dict key -> Participant
        self.events = events

    def build_scene(self, measurer):
        return _build_scene(self, measurer)


def parse(source):
    lines = []
    for raw_line in source.splitlines():
        line = raw_line.split("%%")[0].strip().rstrip(";")
        if line:
            lines.append(line)
    if not lines or lines[0].split()[0].lower() != "sequencediagram":
        raise Unsupported("not a sequenceDiagram header")

    participants = {}
    events = []
    depth = 0

    def touch(key, label=None):
        participant = participants.get(key)
        if participant is None:
            participant = participants[key] = Participant(key, label or key)
        elif label:
            participant.label = label
        return participant

    for line in lines[1:]:
        lowered = line.lower()
        if any(lowered.startswith(prefix) for prefix in _IGNORED):
            continue

        declaration = _PARTICIPANT_RE.match(line)
        if declaration:
            touch(declaration.group("key"), parse_label(declaration.group("label") or "") or None)
            continue

        note = _NOTE_RE.match(line)
        if note:
            keys = [key.strip() for key in note.group("who").split(",") if key.strip()]
            if not keys:
                raise Unsupported("note with no participant")
            for key in keys:
                touch(key)
            placement = note.group("where").lower().replace(" of", "")
            events.append(Note(placement, keys, parse_label(note.group("text"))))
            continue

        activation = _ACTIVATE_RE.match(line)
        if activation:
            touch(activation.group("key"))
            events.append(Activation(
                activation.group("key"), activation.group("verb").lower() == "activate"))
            continue

        keyword = lowered.split()[0]
        if keyword in _BLOCK_OPENERS:
            # `rect` carries a colour rather than a caption ("rect
            # rgb(200,200,255)"), which is presentation we don't apply --
            # so it gets a frame with no label instead of one captioned
            # with a colour literal.
            caption = "" if keyword == "rect" else parse_label(line[len(keyword):])
            events.append(BlockStart(keyword, caption))
            depth += 1
            continue
        if keyword in _BLOCK_DIVIDERS:
            if depth == 0:
                raise Unsupported(f"{keyword!r} outside a block")
            events.append(BlockDivider(parse_label(line[len(keyword):])))
            continue
        if lowered == "end":
            if depth == 0:
                raise Unsupported("`end` outside a block")
            depth -= 1
            events.append(BlockEnd())
            continue

        message = _MESSAGE_RE.match(line)
        if not message:
            raise Unsupported(f"unparsed statement {line!r}")
        touch(message.group("src"))
        touch(message.group("dst"))
        events.append(Message(
            message.group("src"), message.group("dst"), parse_label(message.group("text")),
            dotted=len(message.group("dashes")) == 2,
            head=_ARROWS[message.group("glyph")],
            activation=message.group("activation"),
        ))

    if depth:
        raise Unsupported("unclosed block")
    if not participants:
        raise Unsupported("no participants")
    return SequenceDiagram(participants, events)


# -- layout ----------------------------------------------------------------

class _Row:
    """One event's vertical slot, resolved in a first pass so frames know
    how tall they are before anything is drawn."""
    __slots__ = ("event", "y", "height", "label_size")

    def __init__(self, event, height, label_size=(0.0, 0.0)):
        self.event = event
        self.y = 0.0
        self.height = height
        self.label_size = label_size


def _place_columns(diagram, measurer, rows):
    """Participant x centres.

    Columns start at their own box widths and are then pushed apart until
    every message label fits in the span it is drawn across -- a long
    label between two participants widens exactly that gap, and one drawn
    across three columns widens them evenly, rather than every column
    inheriting the widest label in the diagram.
    """
    order = list(diagram.participants.values())
    index_of = {participant.key: index for index, participant in enumerate(order)}
    for participant in order:
        text_w, text_h = measurer.size(participant.label, bold=True)
        participant.w = max(MIN_BOX_W, text_w + 2 * BOX_PAD_X)
        participant.h = text_h + 2 * BOX_PAD_Y

    gaps = [COLUMN_GAP] * max(len(order) - 1, 0)

    def required(low, high, width):
        # Space needed *between* the two end boxes: a label may overhang
        # the halves of the boxes it starts and ends on, and every box
        # between them is already counted in the columns' own widths.
        inner = sum(order[index].w for index in range(low + 1, high))
        return width - (order[low].w + order[high].w) / 2 - inner

    for row in rows:
        event = row.event
        if not isinstance(event, (Message, Note)):
            continue
        if isinstance(event, Message):
            if event.src == event.dst:
                continue
            low, high = sorted((index_of[event.src], index_of[event.dst]))
            needed = row.label_size[0] + 2 * ROW_PAD
        else:
            indices = [index_of[key] for key in event.keys]
            low, high = min(indices), max(indices)
            needed = row.label_size[0] + 2 * NOTE_PAD_X
            if low == high:
                # A one-participant note hangs off its lifeline rather
                # than spanning a gap, so it can't push columns apart --
                # except the one next to it, which it would overlap.
                if event.placement == "right" and low < len(order) - 1:
                    low, high = low, low + 1
                elif event.placement == "left" and low > 0:
                    low, high = low - 1, low
                else:
                    continue
        shortfall = required(low, high, needed) - sum(gaps[low:high])
        if shortfall > 0:
            share = shortfall / (high - low)
            for index in range(low, high):
                gaps[index] += share

    x = 0.0
    for index, participant in enumerate(order):
        participant.x = x + participant.w / 2
        if index < len(gaps):
            x += participant.w + gaps[index]
    return order, index_of


def _row_for(event, measurer):
    if isinstance(event, Message):
        text_w, text_h = measurer.size(event.text, scale=LABEL_SCALE)
        height = text_h + 2 * ROW_PAD
        if event.src == event.dst:
            height += SELF_MESSAGE_H
        return _Row(event, height, (text_w, text_h))
    if isinstance(event, Note):
        text_w, text_h = measurer.size(event.text)
        return _Row(event, text_h + 2 * NOTE_PAD_Y + ROW_PAD, (text_w, text_h))
    if isinstance(event, BlockStart):
        text_w, text_h = measurer.size(event.label or event.kind, scale=LABEL_SCALE)
        return _Row(event, text_h + 2 * FRAME_LABEL_PAD + FRAME_PAD, (text_w, text_h))
    if isinstance(event, BlockDivider):
        text_w, text_h = measurer.size(event.label, scale=LABEL_SCALE)
        return _Row(event, text_h + 2 * FRAME_LABEL_PAD + FRAME_PAD / 2, (text_w, text_h))
    if isinstance(event, BlockEnd):
        return _Row(event, FRAME_PAD)
    return _Row(event, 0.0)


def _arrow_points(src_x, dst_x, y, activations):
    """Message line endpoints, moved clear of any activation bar already
    drawn on either lifeline."""
    def offset(x, other):
        if activations.get(x, 0) <= 0:
            return x
        return x + (ACTIVATION_W / 2 if other > x else -ACTIVATION_W / 2)
    return (offset(src_x, dst_x), y), (offset(dst_x, src_x), y)


def _build_scene(diagram, measurer):
    rows = [_row_for(event, measurer) for event in diagram.events]
    order, index_of = _place_columns(diagram, measurer, rows)

    head_h = max((participant.h for participant in order), default=0.0)
    y = head_h + HEAD_GAP
    for row in rows:
        row.y = y
        y += row.height
    body_bottom = y + FRAME_PAD

    scene = sc.Scene(0.0, 0.0, [], measurer.font_px)
    left = order[0].x - order[0].w / 2
    right = order[-1].x + order[-1].w / 2

    # Frames first, so their fill sits behind the messages they enclose.
    stack = []
    for index, row in enumerate(rows):
        event = row.event
        if isinstance(event, BlockStart):
            stack.append((row, [], index))
        elif isinstance(event, BlockDivider):
            if stack:
                stack[-1][1].append(row)
        elif isinstance(event, BlockEnd):
            if not stack:
                continue
            start_row, dividers, _start_index = stack.pop()
            depth = len(stack)
            inset = depth * 6.0
            x0 = left - FRAME_PAD + inset
            x1 = right + FRAME_PAD - inset
            top = start_row.y
            bottom = row.y + row.height
            scene.add(sc.Rect(x0, top, x1 - x0, bottom - top, stroke=sc.EDGE, dashed=True))
            label = start_row.event.label
            kind_w = measurer.size(start_row.event.kind, bold=True, scale=LABEL_SCALE)[0]
            label_w = start_row.label_size[0] if label else 0.0
            tab_w = kind_w + label_w + FRAME_LABEL_PAD * (3 if label else 2)
            tab_h = start_row.label_size[1] + 2 * FRAME_LABEL_PAD
            scene.add(sc.Rect(x0, top, tab_w, tab_h, fill=sc.NODE_ALT, stroke=sc.EDGE))
            scene.add(sc.Text(
                x0 + FRAME_LABEL_PAD, top + FRAME_LABEL_PAD, kind_w, tab_h - 2 * FRAME_LABEL_PAD,
                start_row.event.kind, bold=True, align="left", scale=LABEL_SCALE))
            if label:
                scene.add(sc.Text(
                    x0 + FRAME_LABEL_PAD * 2 + kind_w, top + FRAME_LABEL_PAD,
                    start_row.label_size[0], start_row.label_size[1],
                    label, align="left", color=sc.DIM, scale=LABEL_SCALE))
            for divider in dividers:
                divider_y = divider.y
                scene.add(sc.Line([(x0, divider_y), (x1, divider_y)],
                                  stroke=sc.EDGE, dashed=True))
                if divider.event.label:
                    scene.add(sc.Text(
                        x0 + FRAME_LABEL_PAD * 2, divider_y + FRAME_LABEL_PAD,
                        divider.label_size[0], divider.label_size[1],
                        divider.event.label, align="left", color=sc.DIM, scale=LABEL_SCALE))

    # Lifelines, then the boxes that cap them at both ends -- mermaid
    # repeats the participants at the foot of the diagram, which is what
    # makes a tall one readable without scrolling back up.
    lifeline_bottom = body_bottom + LIFELINE_TAIL
    for participant in order:
        scene.add(sc.Line(
            [(participant.x, participant.h), (participant.x, lifeline_bottom)],
            stroke=sc.EDGE, dashed=True))

    activations = {}      # x -> open activation count
    open_bars = {}        # x -> y the current bar started at
    for row in rows:
        event = row.event
        if isinstance(event, Activation):
            x = diagram.participants[event.key].x
            if event.start:
                activations[x] = activations.get(x, 0) + 1
                open_bars.setdefault(x, row.y)
            else:
                activations[x] = max(0, activations.get(x, 0) - 1)
                if activations[x] == 0 and x in open_bars:
                    top = open_bars.pop(x)
                    scene.add(sc.Rect(x - ACTIVATION_W / 2, top, ACTIVATION_W,
                                      max(row.y - top, ROW_PAD),
                                      fill=sc.NODE_ALT, stroke=sc.EDGE))
        elif isinstance(event, Message):
            src = diagram.participants[event.src]
            dst = diagram.participants[event.dst]
            line_y = row.y + row.height - ROW_PAD / 2
            if event.activation == "+":
                activations[dst.x] = activations.get(dst.x, 0) + 1
                open_bars.setdefault(dst.x, line_y)
            elif event.activation == "-":
                activations[src.x] = max(0, activations.get(src.x, 0) - 1)
                if activations[src.x] == 0 and src.x in open_bars:
                    top = open_bars.pop(src.x)
                    scene.add(sc.Rect(src.x - ACTIVATION_W / 2, top, ACTIVATION_W,
                                      max(line_y - top, ROW_PAD),
                                      fill=sc.NODE_ALT, stroke=sc.EDGE))
            text_w, text_h = row.label_size
            if event.src == event.dst:
                top = row.y + ROW_PAD
                x = src.x + (ACTIVATION_W / 2 if activations.get(src.x, 0) else 0)
                points = [(x, top), (x + SELF_MESSAGE_W, top),
                          (x + SELF_MESSAGE_W, top + SELF_MESSAGE_H),
                          (x, top + SELF_MESSAGE_H)]
                scene.add(sc.Line(points, dashed=event.dotted, head=event.head))
                if event.text:
                    scene.add(sc.Text(
                        x + SELF_MESSAGE_W + 6, top + SELF_MESSAGE_H / 2 - text_h / 2,
                        text_w, text_h, event.text, align="left", scale=LABEL_SCALE))
            else:
                start, end = _arrow_points(src.x, dst.x, line_y, activations)
                scene.add(sc.Line([start, end], dashed=event.dotted, head=event.head))
                if event.text:
                    middle = (start[0] + end[0]) / 2
                    scene.add(sc.Text(
                        middle - text_w / 2, line_y - text_h - 3, text_w, text_h,
                        event.text, scale=LABEL_SCALE))
        elif isinstance(event, Note):
            text_w, text_h = row.label_size
            width = text_w + 2 * NOTE_PAD_X
            height = text_h + 2 * NOTE_PAD_Y
            keys = [diagram.participants[key] for key in event.keys]
            if event.placement == "over":
                centre = (min(p.x for p in keys) + max(p.x for p in keys)) / 2
                width = max(width, max(p.x for p in keys) - min(p.x for p in keys) + MIN_BOX_W)
                x = centre - width / 2
            elif event.placement == "right":
                x = keys[0].x + keys[0].w / 4
            else:
                x = keys[0].x - keys[0].w / 4 - width
            scene.add(sc.Rect(x, row.y, width, height, fill=sc.NOTE, stroke=sc.EDGE))
            scene.add(sc.Text(x + NOTE_PAD_X, row.y + NOTE_PAD_Y,
                              width - 2 * NOTE_PAD_X, text_h, event.text))

    # Any activation left open at the end runs to the foot of its lifeline
    # rather than being dropped -- an unbalanced `activate` is the author's
    # slip, and showing it is more useful than silently ignoring it.
    for x, top in open_bars.items():
        scene.add(sc.Rect(x - ACTIVATION_W / 2, top, ACTIVATION_W,
                          max(body_bottom - top, ROW_PAD),
                          fill=sc.NODE_ALT, stroke=sc.EDGE))

    for participant in order:
        for top in (0.0, lifeline_bottom):
            scene.add(sc.Rect(
                participant.x - participant.w / 2, top, participant.w, participant.h,
                radius=4.0, fill=sc.NODE, stroke=sc.EDGE))
            text_w, text_h = measurer.size(participant.label, bold=True)
            scene.add(sc.Text(
                participant.x - text_w / 2, top + (participant.h - text_h) / 2,
                text_w, text_h, participant.label, bold=True))

    return scene.normalize()

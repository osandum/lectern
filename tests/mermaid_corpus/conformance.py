"""Lectern's mermaid parsers against what mermaid's own parser makes of
the same sources (recorded/, written by record/record.mjs -- see README).

Each case comes out as one of:

  agree        both read it the same way, or both refuse it
  unsupported  mermaid reads it and Lectern refuses it, which is allowed:
               Lectern draws a subset, and falls back to the source for
               the rest
  rejected     mermaid refuses it and Lectern draws something
  differs      both read it, differently -- a silent mis-parse, the one
               outcome the fallback rule exists to prevent

Both sides are reduced to the same plain form first, holding only what
Lectern models: what a reader would see differ. Edge length (extra
dashes) and styling are not in it.

    python tests/mermaid_corpus/conformance.py [family]

prints where each family stands.
"""
import html
import json
import pathlib
import re
import sys
import urllib.parse

HERE = pathlib.Path(__file__).parent
FAMILIES = ("flowchart", "sequence", "class", "er")


def cases(family):
    """Every case of `family`, in file order: dicts of id, origin, source."""
    out = []
    for path in sorted((HERE / "cases").glob(f"*.{family}.json")):
        out += json.loads(path.read_text())
    return out


def recorded(family):
    return json.loads((HERE / "recorded" / f"{family}.json").read_text())["results"]


def deviations():
    """Case id -> why Lectern currently deviates from mermaid there."""
    return json.loads((HERE / "deviations.json").read_text())


# -- mermaid's side --------------------------------------------------------

_BR_RE = re.compile(r"<br\s*/?>", re.I)
# mermaid's spelling of a character reference, `#quot;` or `#35;`, which
# its parser stores encoded as these placeholders until it renders.
_MERMAID_REF_RE = re.compile(r"(?<!&)#(\w+);")
_PLACEHOLDERS = (("ﬂ°°", "&#"), ("ﬂ°", "&"), ("¶ß", ";"))


def _shown(text):
    """Label text as a reader sees it: character references decoded,
    whichever way they were spelled or stored."""
    text = _MERMAID_REF_RE.sub(
        lambda m: f"&#{m.group(1)};" if m.group(1).isdigit() else f"&{m.group(1)};", text)
    return html.unescape(text)


def _text(text):
    """A label as mermaid's renderer would show it, broken into lines."""
    for placeholder, spelled in _PLACEHOLDERS:
        text = (text or "").replace(placeholder, spelled)
    return _shown(_BR_RE.sub("\n", text).replace("\\n", "\n").strip())


def _url(href):
    """mermaid normalizes a link's URL; a bare host gains its `/`."""
    parts = urllib.parse.urlsplit(href)
    return parts._replace(path=parts.path or "/").geturl() if parts.netloc else href


# mermaid's vertex types -> Lectern's shape names. A vertex without a type
# was only ever referenced, which mermaid draws as a rect.
_SHAPES = {
    None: "rect", "square": "rect", "round": "round", "circle": "circle",
    "stadium": "stadium", "subroutine": "subroutine", "cylinder": "cylinder",
    "diamond": "diamond", "hexagon": "hexagon", "lean_right": "parallelogram",
    "lean_left": "parallelogram-alt", "trapezoid": "trapezoid",
    "inv_trapezoid": "trapezoid-alt", "odd": "asymmetric",
}

# mermaid's edge types -> (tail, head) in Lectern's arrowhead names.
_ENDS = {
    "arrow_open": (None, None),
    "arrow_point": (None, "arrow"), "double_arrow_point": ("arrow", "arrow"),
    "arrow_circle": (None, "circle"), "double_arrow_circle": ("circle", "circle"),
    "arrow_cross": (None, "cross"), "double_arrow_cross": ("cross", "cross"),
}
_STROKES = {"normal": "solid", "dotted": "dotted", "thick": "thick", "invisible": "invisible"}


def mermaid_flowchart(result):
    """A recorded flowchart in the plain form, or None if mermaid refused
    it."""
    if "error" in result:
        return None
    if "direction" not in result:
        # mermaid read it as another type of diagram, which no flowchart
        # Lectern could draw from it would match.
        return {"type": result["type"]}
    direction = result["direction"]
    # An edge naming a subgraph leaves a vertex of that id behind, which
    # mermaid's renderer draws as the frame, not as a node.
    frames = {s["id"] for s in result["subgraphs"]}
    return {
        "direction": "TD" if direction == "TB" else direction,
        "nodes": {n["id"]: {"text": _text(n["text"]), "shape": _SHAPES.get(n.get("type"), n.get("type"))}
                  for n in result["nodes"] if n["id"] not in frames},
        "edges": [{"src": e["start"], "dst": e["end"], "text": _text(e["text"]),
                   "stroke": _STROKES.get(e["stroke"], e["stroke"]),
                   "ends": list(_ENDS.get(e["type"], (e["type"], e["type"])))}
                  for e in result["edges"]],
        "subgraphs": {s["id"]: {"title": _text(s["title"]), "members": sorted(s["nodes"])}
                      for s in result["subgraphs"]},
        "links": {n["id"]: _url(n["link"]) for n in result["nodes"] if n.get("link")},
    }


# -- Lectern's side --------------------------------------------------------

def lectern_flowchart(source):
    """Lectern's parse of `source` in the plain form, or None if it raises
    Unsupported."""
    from lectern import mermaid
    from lectern.mermaid.common import Unsupported
    try:
        chart = mermaid.parse(source)
    except Unsupported:
        return None
    return {
        "direction": chart.direction,
        "nodes": {key: {"text": _shown(node.text), "shape": node.shape}
                  for key, node in chart.nodes.items()},
        "edges": [{"src": edge.src_cluster or edge.src, "dst": edge.dst_cluster or edge.dst,
                   "text": _shown(edge.label or ""),
                   "stroke": edge.style,
                   "ends": [edge.tail, edge.head]}
                  for edge in chart.edges],
        "subgraphs": {group.key: {"title": _shown(group.title),
                                  "members": sorted(group.members + [c.key for c in group.children])}
                      for group in chart.all_subgraphs()},
        "links": {key: _url(node.href) for key, node in chart.nodes.items() if node.href},
    }


# mermaid's sequence message types (sequenceDb's LINETYPE) -> how Lectern
# models them: (dotted, arrowhead) for a message, or the event they are.
_MESSAGES = {0: (False, "arrow"), 1: (True, "arrow"), 5: (False, None), 6: (True, None),
             3: (False, "cross"), 4: (True, "cross"), 24: (False, "open"), 25: (True, "open")}
_BLOCKS = {10: ("start", "loop"), 11: ("end",), 12: ("start", "alt"), 13: ("divider",),
           14: ("end",), 15: ("start", "opt"), 16: ("end",), 19: ("start", "par"),
           20: ("divider",), 21: ("end",), 22: ("start", "rect"), 23: ("end",),
           27: ("start", "critical"), 28: ("divider",), 29: ("end",),
           30: ("start", "break"), 31: ("end",)}
_PLACEMENTS = {0: "left", 1: "right", 2: "over"}


def mermaid_sequence(result):
    """A recorded sequence diagram in the plain form: participants in
    order, then every event. Whether a participant is drawn as an actor is
    not in it -- Lectern draws them all as boxes, a matter of looks like
    styling."""
    if "error" in result:
        return None
    if "actors" not in result:
        return {"type": result["type"]}
    events = []
    for m in result["messages"]:
        kind = m["type"]
        if kind in _MESSAGES:
            dotted, head = _MESSAGES[kind]
            events.append(["message", m["from"], m["to"], _text(m["message"]), dotted, head])
        elif kind == 2:
            keys = [m["from"]] if m["from"] == m["to"] else [m["from"], m["to"]]
            events.append(["note", _PLACEMENTS[m["placement"]], keys, _text(m["message"])])
        elif kind in (17, 18):
            events.append(["activate" if kind == 17 else "deactivate", m["from"]])
        elif kind in _BLOCKS:
            block = list(_BLOCKS[kind])
            # A rect's "label" is its colour, which isn't drawn as text.
            label = "" if block == ["start", "rect"] else _text(m["message"])
            events.append(block + ([label] if block[0] != "end" else []))
        else:
            events.append(["mermaid", kind])   # nothing Lectern has a name for
    return {
        "participants": [[a["name"], _text(a["description"])] for a in result["actors"]],
        "events": events,
    }


def lectern_sequence(source):
    from lectern.mermaid import sequence
    from lectern.mermaid.common import Unsupported
    try:
        diagram = sequence.parse(source)
    except Unsupported:
        return None
    events = []
    for event in diagram.events:
        if isinstance(event, sequence.Message):
            events.append(["message", event.src, event.dst, _shown(event.text),
                           event.dotted, event.head])
            # The `+`/`-` shorthand: mermaid records the activation as
            # an event of its own, right after the message.
            if event.activation == "+":
                events.append(["activate", event.dst])
            elif event.activation == "-":
                events.append(["deactivate", event.src])
        elif isinstance(event, sequence.Note):
            events.append(["note", event.placement, event.keys, _shown(event.text)])
        elif isinstance(event, sequence.Activation):
            events.append(["activate" if event.start else "deactivate", event.key])
        elif isinstance(event, sequence.BlockStart):
            events.append(["start", event.kind, _shown(event.label)])
        elif isinstance(event, sequence.BlockDivider):
            events.append(["divider", _shown(event.label)])
        else:
            events.append(["end"])
    return {
        "participants": [[key, _shown(p.label)] for key, p in diagram.participants.items()],
        "events": events,
    }


# mermaid's class relation types (classDb's relationType) -> Lectern's end
# glyphs, and its ER cardinalities -> Lectern's.
_UML_ENDS = {"none": None, 0: "diamond-open", 1: "triangle", 2: "diamond", 3: "arrow"}
_CARDINALITIES = {"ONLY_ONE": "one", "ZERO_OR_ONE": "zero-one",
                  "ZERO_OR_MORE": "zero-many", "ONE_OR_MORE": "one-many"}


def _member(text):
    """A class member as shown: mermaid escapes a leading visibility
    sign so its markdown doesn't read it as a list item."""
    return _text(text.replace("\\", "", 1) if text.startswith("\\") else text)


def mermaid_class(result):
    if "error" in result:
        return None
    if "classes" not in result:
        return {"type": result["type"]}
    if result["notes"] or result["namespaces"]:
        return {"notes": result["notes"], "namespaces": result["namespaces"]}
    return {
        "direction": "TD" if result["direction"] == "TB" else result["direction"],
        "entities": {c["id"]: {
            "title": _text(c["label"]) + (f"<{c['type']}>" if c.get("type") else ""),
            "stereotypes": c["annotations"],
            "members": sorted(_member(m) for m in c["members"] + c["methods"]),
        } for c in result["classes"]},
        "relations": [{
            "src": r["id1"], "dst": r["id2"],
            "ends": [_UML_ENDS.get(r["type1"], r["type1"]), _UML_ENDS.get(r["type2"], r["type2"])],
            "dashed": r["lineType"] == 1, "label": _text(r.get("title", "")),
            "cards": ["" if r.get(k, "none") == "none" else _text(r[k])
                      for k in ("relationTitle1", "relationTitle2")],
        } for r in result["relations"]],
        "links": {c["id"]: _url(c["link"]) for c in result["classes"] if c.get("link")},
    }


def mermaid_er(result):
    if "error" in result:
        return None
    if "entities" not in result:
        return {"type": result["type"]}

    def attribute(a):
        words = [a["type"], a["name"]]
        if a.get("keys"):
            words.append(", ".join(a["keys"]))
        if a.get("comment"):
            words.append(f'"{a["comment"]}"')
        return " ".join(words)
    return {
        "direction": "TD" if result["direction"] == "TB" else result["direction"],
        "entities": {e["name"]: {
            "title": _text(e["alias"] or e["name"]),
            "stereotypes": [],
            "members": sorted(attribute(a) for a in e["attributes"]),
        } for e in result["entities"]},
        # mermaid names the cardinalities from the far end: cardA is the
        # one drawn at entity B.
        "relations": [{
            "src": r["entityA"], "dst": r["entityB"],
            "ends": [_CARDINALITIES.get(r["cardB"], r["cardB"]),
                     _CARDINALITIES.get(r["cardA"], r["cardA"])],
            "dashed": r["relType"] == "NON_IDENTIFYING", "label": _text(r["roleA"]),
            "cards": ["", ""],
        } for r in result["relationships"]],
        "links": {},
    }


def _lectern_entities(parse, source):
    from lectern.mermaid.common import Unsupported
    try:
        diagram = parse(source)
    except Unsupported:
        return None
    return {
        "direction": diagram.direction,
        "entities": {key: {
            "title": _shown(entity.title),
            "stereotypes": [entity.stereotype] if entity.stereotype else [],
            # As written, but spaced the way mermaid shows an ER key list.
            "members": sorted(_shown(re.sub(r"\s*,\s*", ", ", " ".join(m.split())))
                              for m in entity.members),
        } for key, entity in diagram.entities.items()},
        "relations": [{
            "src": r.src, "dst": r.dst, "ends": [r.tail, r.head], "dashed": r.dashed,
            "label": _shown(r.label), "cards": [r.src_card, r.dst_card],
        } for r in diagram.relations],
        "links": {key: _url(e.href) for key, e in diagram.entities.items() if e.href},
    }


def lectern_class(source):
    from lectern.mermaid import entities
    return _lectern_entities(entities.parse_class, source)


def lectern_er(source):
    from lectern.mermaid import entities
    return _lectern_entities(entities.parse_er, source)


COMPARE = {
    "flowchart": (mermaid_flowchart, lectern_flowchart),
    "sequence": (mermaid_sequence, lectern_sequence),
    "class": (mermaid_class, lectern_class),
    "er": (mermaid_er, lectern_er),
}


def outcome(family, case, result):
    """(outcome, mermaid's form, Lectern's form) for one case."""
    from_mermaid, from_lectern = COMPARE[family]
    theirs, ours = from_mermaid(result), from_lectern(case["source"])
    if theirs is None:
        return ("agree" if ours is None else "rejected"), theirs, ours
    if ours is None:
        return "unsupported", theirs, ours
    return ("agree" if ours == theirs else "differs"), theirs, ours


def report(family):
    results = recorded(family)
    known = deviations()
    tally = {}
    for case in cases(family):
        kind, _theirs, _ours = outcome(family, case, results[case["id"]])
        tally[kind] = tally.get(kind, 0) + 1
        if kind in ("rejected", "differs"):
            note = "" if case["id"] in known else "  (not in deviations.json)"
            print(f"{kind:9} {case['id']}{note}")
    print(f"{family}: " + ", ".join(f"{n} {kind}" for kind, n in sorted(tally.items())))


if __name__ == "__main__":
    sys.path.insert(0, str(HERE.parent.parent))
    for family in sys.argv[1:] or [f for f in FAMILIES if f in COMPARE]:
        report(family)

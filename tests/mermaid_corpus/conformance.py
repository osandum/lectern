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


COMPARE = {"flowchart": (mermaid_flowchart, lectern_flowchart)}


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

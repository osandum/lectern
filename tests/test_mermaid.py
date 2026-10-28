"""Mermaid parsing and layout.

Parsing is pure data and needs nothing from GTK. Layout does need Pango
to measure text, but not a display -- it measures against an image
surface (see mermaid/text.py), so these run wherever the rest of the
suite does.

Geometry is asserted as *relations* -- this node is left of that one,
these two boxes don't overlap -- rather than as coordinates. The
coordinates depend on the font the machine happens to have, exactly as
test_layout.py's do, and a diagram that reads correctly is one whose
parts are in the right order and don't collide, not one whose nodes are
at particular pixels.
"""
import pytest

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from markdown_it.tree import SyntaxTreeNode

from lectern import mermaid
from lectern.document import make_parser
from lectern.mermaid import flowchart, layered, scene as sc, sequence, text as textlib
from lectern.mermaid.common import Unsupported, parse_init
from lectern.renderer import MarkdownRenderer
from lectern.tags import create_tag_table

_PARSER = make_parser()

SIMPLE = """flowchart LR
    A[Open file] --> B{Cached?}
    B -- yes --> C[Render]
    B -- no --> D[Parse] --> C
"""


def build(source):
    return mermaid.scene_for_source(source, mermaid.ui_font())


def texts(scene):
    return [shape.text for shape in scene.shapes if isinstance(shape, sc.Text)]


def boxes(scene):
    return [shape for shape in scene.shapes if isinstance(shape, (sc.Rect, sc.Ellipse))]


# -- parsing ---------------------------------------------------------------

def test_parses_nodes_edges_and_direction():
    chart = mermaid.parse(SIMPLE)
    assert chart.direction == "LR"
    assert list(chart.nodes) == ["A", "B", "C", "D"]
    assert chart.nodes["A"].text == "Open file"
    assert chart.nodes["B"].shape == "diamond"
    assert [(e.src, e.dst) for e in chart.edges] == [
        ("A", "B"), ("B", "C"), ("B", "D"), ("D", "C")]


def test_label_between_dashes_and_in_pipes_are_the_same_link():
    piped = mermaid.parse("flowchart TD\n  A -->|yes| B\n")
    dashed = mermaid.parse("flowchart TD\n  A -- yes --> B\n")
    for chart in (piped, dashed):
        assert len(chart.edges) == 1
        edge = chart.edges[0]
        assert (edge.label, edge.style, edge.head) == ("yes", "solid", "arrow")


@pytest.mark.parametrize("link, style, head", [
    ("-->", "solid", "arrow"),
    ("---", "solid", None),
    ("-.->", "dotted", "arrow"),
    ("-.-", "dotted", None),
    ("==>", "thick", "arrow"),
    ("===", "thick", None),
    ("--o", "solid", "circle"),
    ("--x", "solid", "cross"),
])
def test_link_styles_and_arrowheads(link, style, head):
    chart = mermaid.parse(f"flowchart TD\n  A {link} B\n")
    edge = chart.edges[0]
    assert (edge.style, edge.head) == (style, head)


def test_chain_declares_every_hop():
    chart = mermaid.parse("flowchart TD\n  A --> B --> C --> D\n")
    assert [(e.src, e.dst) for e in chart.edges] == [("A", "B"), ("B", "C"), ("C", "D")]


@pytest.mark.parametrize("link", ["---", "===", "-.-"])
def test_a_chain_of_unlabelled_links_is_not_read_as_a_label(link):
    """`-- B --` inside `A --- B --- C` looks like a label between dashes,
    which used to turn B into the label of one link from A to C."""
    chart = mermaid.parse(f"flowchart TD\n  A {link} B {link} C\n")
    assert [(e.src, e.dst, e.label) for e in chart.edges] == [("A", "B", ""), ("B", "C", "")]


@pytest.mark.parametrize("link, style", [
    ("-.yes.->", "dotted"), ("-. yes .->", "dotted"),
    ("==yes==>", "thick"), ("== yes ==>", "thick"),
])
def test_a_dotted_or_thick_label_may_touch_its_dashes(link, style):
    chart = mermaid.parse(f"flowchart TD\n  A {link} B\n")
    edge, = chart.edges
    assert (edge.src, edge.dst, edge.label, edge.style, edge.head) == ("A", "B", "yes", style, "arrow")


def test_bare_reference_takes_its_id_as_label_until_declared():
    chart = mermaid.parse("flowchart TD\n  A --> B\n  B[Real label]\n")
    assert chart.nodes["A"].text == "A"
    assert chart.nodes["B"].text == "Real label"


@pytest.mark.parametrize("source, shape", [
    ("A[rect]", "rect"),
    ("A(round)", "round"),
    ("A([stadium])", "stadium"),
    ("A[[subroutine]]", "subroutine"),
    ("A[(cylinder)]", "cylinder"),
    ("A((circle))", "circle"),
    ("A{diamond}", "diamond"),
    ("A{{hexagon}}", "hexagon"),
    ("A>asymmetric]", "asymmetric"),
    ("A[/parallelogram/]", "parallelogram"),
    ("A[\\alt\\]", "parallelogram-alt"),
    ("A[/trapezoid\\]", "trapezoid"),
    ("A[\\trap alt/]", "trapezoid-alt"),
])
def test_node_shapes(source, shape):
    chart = mermaid.parse(f"flowchart TD\n  {source}\n")
    assert chart.nodes["A"].shape == shape


def test_quoted_label_may_contain_delimiters():
    chart = mermaid.parse('flowchart TD\n  A["a [b] {c}"] --> B\n')
    assert chart.nodes["A"].text == "a [b] {c}"


def test_br_becomes_a_line_break():
    chart = mermaid.parse("flowchart TD\n  A[first<br/>second]\n")
    assert chart.nodes["A"].text == "first\nsecond"


@pytest.mark.parametrize("label, markup", [
    ("<b>bold</b> and <em>it</em>", "<b>bold</b> and <i>it</i>"),
    ("<STRONG>a</STRONG>", "<b>a</b>"),
    ("List<String> & co", "List&lt;String&gt; &amp; co"),   # not ours: escaped
    ("<b>open", "&lt;b&gt;open"),                           # unbalanced: verbatim
    ("<b><i>x</b></i>", "&lt;b&gt;&lt;i&gt;x&lt;/b&gt;&lt;/i&gt;"),
    ("&lt;app&gt;-&lt;env&gt;", "&lt;app&gt;-&lt;env&gt;"),  # references decoded ...
    ("&lt;b&gt;x&lt;/b&gt;", "&lt;b&gt;x&lt;/b&gt;"),       # ... into text, not tags
    ("<b>&amp;</b> &#35;1", "<b>&amp;</b> #1"),
    ("#quot;hi#quot; #9829;", "&quot;hi&quot; ♥"),   # mermaid's spelling
])
def test_label_markup(label, markup):
    assert textlib.label_markup(label) == markup


def test_bold_label_measures_as_bold_text():
    measurer = textlib.Measurer(mermaid.ui_font())
    tagged = measurer.size("<b>Phase zero</b>")
    assert tagged > measurer.size("Phase zero")
    # The tags themselves take no room.
    assert tagged == measurer.size("Phase zero", bold=True)


def test_semicolon_in_a_quoted_label_does_not_end_the_statement():
    chart = mermaid.parse('flowchart TD\n  A["one; two"] --> B; B --> C\n')
    assert chart.nodes["A"].text == "one; two"
    assert len(chart.edges) == 2


def test_comments_and_presentational_statements_are_ignored():
    chart = mermaid.parse(
        "flowchart TD\n"
        "%% a comment\n"
        "  A --> B\n"
        "  style A fill:#f9f\n"
        "  classDef big font-size:20px\n"
        "  click A callback\n"
    )
    assert list(chart.nodes) == ["A", "B"]
    assert len(chart.edges) == 1


def test_a_comment_after_code_falls_back_as_mermaid_refuses_it():
    # Only a whole line is a `%%` comment to mermaid; after code it is a
    # syntax error, and the diagram doesn't render on GitHub either.
    with pytest.raises(Unsupported):
        mermaid.parse("flowchart TD\n  A --> B  %% trailing\n")


@pytest.mark.parametrize("source", [
    "gantt\n  title Nope\n",                       # a type we don't draw
    "stateDiagram-v2\n  [*] --> Still\n",          # ditto
    "flowchart TD\n  subgraph one\n  A\n",         # unclosed subgraph
    "flowchart TD\n  A\n  end\n",                  # `end` with no subgraph
    "flowchart TD\n  A --> B &\n",                # `&` with nothing after it
    "flowchart XY\n  A --> B\n",                   # unknown direction
    "flowchart TD\n  A[unterminated\n",
    "",
])
def test_unsupported_sources_raise(source):
    with pytest.raises(mermaid.Unsupported):
        mermaid.parse(source)


# -- subgraphs -------------------------------------------------------------

def test_a_subgraph_holds_every_node_named_inside_it():
    chart = mermaid.parse(
        "flowchart TD\n"
        "  A --> D\n"
        "  subgraph s[Inside]\n    A --> B\n  end\n"
        "  B --> C\n")
    group, = chart.subgraphs
    assert group.key == "s" and group.title == "Inside"
    # mermaid's rule: named inside the block, it's a member -- A too,
    # though named outside before. Naming B outside afterwards doesn't
    # move it, and C was never named inside.
    assert set(group.node_keys()) == {"A", "B"}


def test_a_node_stays_in_the_first_subgraph_to_close_around_it():
    chart = mermaid.parse(
        "flowchart TD\n"
        "  subgraph outer\n    subgraph inner\n      A\n    end\n    A --> B\n  end\n")
    outer, = chart.subgraphs
    inner, = outer.children
    assert inner.members == ["A"] and outer.members == ["B"]


def test_a_bare_subgraph_title_is_its_own_id():
    chart = mermaid.parse("flowchart TD\n  subgraph Payments\n    A\n  end\n")
    group, = chart.subgraphs
    assert (group.key, group.title) == ("Payments", "Payments")


def test_nested_subgraphs_nest():
    chart = mermaid.parse(
        "flowchart TD\n"
        "  subgraph outer[Outer]\n"
        "    A\n"
        "    subgraph inner[Inner]\n      B\n    end\n"
        "  end\n")
    outer, = chart.subgraphs
    inner, = outer.children
    assert outer.node_keys() == ["A", "B"]
    assert inner.node_keys() == ["B"]


def test_a_subgraph_frame_encloses_its_own_nodes_and_no_others():
    """The frame is fitted to where the layout put things, so this is the
    assertion that the fitting is honest -- see _cluster_boxes."""
    scene = build(
        "flowchart TD\n"
        "  subgraph s[Group]\n    A[Inside one] --> B[Inside two]\n  end\n"
        "  B --> C[Outside]\n")
    frame = next(shape for shape in boxes(scene)
                 if getattr(shape, "fill", None) == sc.CLUSTER)
    inside = [t for t in texts(scene) if t.startswith("Inside")]
    assert len(inside) == 2
    for shape in scene.shapes:
        if not isinstance(shape, sc.Text):
            continue
        x0, y0, x1, y1 = sc._point_extents(shape)
        enclosed = (x0 >= frame.x and x1 <= frame.x + frame.w
                    and y0 >= frame.y and y1 <= frame.y + frame.h)
        assert enclosed == (shape.text in inside or shape.text == "Group"), shape.text


def test_an_outsider_ranked_between_members_is_kept_out_of_the_frame():
    """X sits a rank between A and B, both in the block, so a layout
    ignoring membership puts it right inside their frame -- which used to
    mean the whole diagram fell back to source."""
    for direction in ("TD", "LR"):
        scene = build(
            f"flowchart {direction}\n"
            "  subgraph g[Group]\n    A[Inside one]\n    B[Inside two]\n  end\n"
            "  S[Start] --> A\n  S --> B\n  A --> X[Outside] --> B\n")
        frame = next(shape for shape in boxes(scene)
                     if getattr(shape, "fill", None) == sc.CLUSTER)
        outside = [shape for shape in scene.shapes if isinstance(shape, sc.Text)
                   and shape.text in ("Start", "Outside")]
        assert len(outside) == 2
        for shape in outside:
            x0, y0, x1, y1 = sc._point_extents(shape)
            assert (x1 <= frame.x or x0 >= frame.x + frame.w
                    or y1 <= frame.y or y0 >= frame.y + frame.h), (direction, shape.text)


def test_sibling_frames_sharing_only_some_layers_stay_apart():
    """Data spans the layers below Edge's, ECS spans both, and in each
    layer one frame has no node in. Fenced a layer at a time, one block's
    nodes could land either side of another's, and frames pushing each
    other's nodes back and forth never settled -- the diagram fell back
    to source. Cut down from a real document."""
    scene = build(
        "flowchart TB\n"
        "  subgraph Edge\n    WAF\n    CFI\n  end\n"
        "  subgraph ECS\n"
        "    subgraph FC\n      FR\n    end\n"
        "    subgraph AC\n      AR\n    end\n"
        "  end\n"
        "  subgraph Data\n    PG\n    S3B\n  end\n"
        "  User --> CFI --> IALB --> IMG --> S3B\n")
    owners = {"WAF": {"Edge"}, "CFI": {"Edge"}, "FR": {"ECS", "FC"},
              "AR": {"ECS", "AC"}, "PG": {"Data"}, "S3B": {"Data"},
              "User": set(), "IALB": set(), "IMG": set()}
    text = {shape.text: sc._point_extents(shape)
            for shape in scene.shapes if isinstance(shape, sc.Text)}

    def within(extents, frame):
        x0, y0, x1, y1 = extents
        return (x0 >= frame.x and x1 <= frame.x + frame.w
                and y0 >= frame.y and y1 <= frame.y + frame.h)

    frames = {}
    for frame in boxes(scene):
        if getattr(frame, "fill", None) != sc.CLUSTER:
            continue
        # A frame's title is the topmost frame title inside it.
        titles = [t for t in ("Edge", "ECS", "FC", "AC", "Data") if within(text[t], frame)]
        title = min(titles, key=lambda t: text[t][1])
        # Title plates are filled like frames; they are a title high.
        if frame.h > 2 * (text[title][3] - text[title][1]):
            frames[title] = frame
    assert set(frames) == {"Edge", "ECS", "FC", "AC", "Data"}
    for node, mine in owners.items():
        assert {key for key, frame in frames.items() if within(text[node], frame)} == mine, node


def test_an_edge_to_a_subgraph_ends_at_its_frame():
    """Mermaid lets either end of an edge name a subgraph, and draws the
    edge to the frame -- not to a node conjured up with the block's id."""
    for direction in ("TD", "LR"):
        source = (f"flowchart {direction}\n"
                  "  subgraph S[Store]\n    K1[Inside one]\n    K2[Inside two]\n  end\n"
                  "  R[Outside] -.bootstraps.-> S\n")
        chart = mermaid.parse(source)
        assert set(chart.nodes) == {"K1", "K2", "R"}
        edge, = chart.edges
        assert edge.dst_cluster == "S"
        scene = build(source)
        frame = max((shape for shape in boxes(scene)
                     if getattr(shape, "fill", None) == sc.CLUSTER), key=lambda f: f.h)
        line, = [shape for shape in scene.shapes if isinstance(shape, sc.Line)]
        x, y = line.points[-1]
        on_left_or_right = abs(x - frame.x) < 0.5 or abs(x - (frame.x + frame.w)) < 0.5
        on_top_or_bottom = abs(y - frame.y) < 0.5 or abs(y - (frame.y + frame.h)) < 0.5
        assert (frame.x - 0.5 <= x <= frame.x + frame.w + 0.5
                and frame.y - 0.5 <= y <= frame.y + frame.h + 0.5
                and (on_left_or_right or on_top_or_bottom)), (direction, x, y, frame)


def test_an_edge_from_a_subgraph_starts_at_its_frame():
    chart = mermaid.parse(
        "flowchart TD\n  subgraph S\n    K\n  end\n  S --> R\n")
    edge, = chart.edges
    assert (edge.src, edge.src_cluster, edge.dst) == ("K", "S", "R")
    build("flowchart TD\n  subgraph S\n    K\n  end\n  S --> R\n")


@pytest.mark.parametrize("source", [
    "flowchart TD\n  subgraph S\n    K\n  end\n  S[Node too] --> R\n",   # node or block?
    "flowchart TD\n  subgraph S\n    K\n  end\n  S --> K\n",             # into itself
    "flowchart TD\n  subgraph S\n    subgraph T\n      K\n    end\n  end\n  S --> T\n",
])
def test_an_ambiguous_edge_to_a_subgraph_falls_back(source):
    with pytest.raises(mermaid.Unsupported):
        mermaid.parse(source)


def test_a_frame_that_would_swallow_a_foreign_node_falls_back():
    """Two blocks whose members the layout interleaves can't both be framed
    honestly. Better an honest code block than a grouping the author never
    wrote -- so this must raise rather than draw."""
    interleaved = (
        "flowchart LR\n"
        "  subgraph one[One]\n    A --> B\n  end\n"
        "  subgraph two[Two]\n    C --> D\n  end\n"
        "  A --> D\n  C --> B\n")
    try:
        build(interleaved)
    except mermaid.Unsupported:
        return
    # If the layout happened to keep them apart, the frames must at least
    # not overlap each other.
    scene = build(interleaved)
    frames = [s for s in boxes(scene) if getattr(s, "fill", None) == sc.CLUSTER]
    for a, b in zip(frames, frames[1:]):
        assert not (abs((a.x + a.w / 2) - (b.x + b.w / 2)) * 2 < a.w + b.w
                    and abs((a.y + a.h / 2) - (b.y + b.h / 2)) * 2 < a.h + b.h)


def test_an_invisible_link_ranks_its_ends_but_draws_nothing():
    """`~~~` is mermaid's layout-only edge."""
    visible = build("flowchart TD\n  A --> B\n")
    invisible = build("flowchart TD\n  A ~~~ B\n")
    lines = lambda scene: [s for s in scene.shapes if isinstance(s, sc.Line)]
    assert len(lines(visible)) == 1
    assert lines(invisible) == []
    # Still ranked: B sits below A, exactly as the visible edge puts it.
    assert mermaid.parse("flowchart TD\n  A ~~~ B\n").edges[0].style == "invisible"


# -- labels ----------------------------------------------------------------

def test_label_line_breaks_use_both_mermaid_spellings():
    """`<br>` and a literal backslash-n both break a line -- mermaid 11
    renders each as one line per break, checked in a browser."""
    chart = mermaid.parse('flowchart TD\n  A["one<br>two\\nthree"]\n')
    assert chart.nodes["A"].text == "one\ntwo\nthree"


# -- layout ----------------------------------------------------------------

def test_scene_carries_every_label():
    scene = build(SIMPLE)
    assert set(texts(scene)) == {"Open file", "Cached?", "Render", "Parse", "yes", "no"}


def test_scene_box_contains_every_shape():
    scene = build(SIMPLE)
    for shape in scene.shapes:
        x0, y0, x1, y1 = sc._point_extents(shape)
        assert x0 >= 0 and y0 >= 0
        assert x1 <= scene.width and y1 <= scene.height


def test_node_boxes_do_not_overlap():
    chart = mermaid.parse(SIMPLE)
    mermaid.build_scene(chart, mermaid.ui_font())
    nodes = list(chart.nodes.values())
    for index, a in enumerate(nodes):
        for b in nodes[index + 1:]:
            gap_x = abs(a.x - b.x) - (a.w + b.w) / 2
            gap_y = abs(a.y - b.y) - (a.h + b.h) / 2
            assert gap_x > 0 or gap_y > 0, f"{a.key} overlaps {b.key}"


def test_direction_decides_which_axis_ranks_run_along():
    source = "flowchart {}\n  A --> B --> C\n"
    left_right = mermaid.parse(source.format("LR"))
    mermaid.build_scene(left_right, mermaid.ui_font())
    assert left_right.nodes["A"].x < left_right.nodes["B"].x < left_right.nodes["C"].x

    top_down = mermaid.parse(source.format("TD"))
    mermaid.build_scene(top_down, mermaid.ui_font())
    assert top_down.nodes["A"].y < top_down.nodes["B"].y < top_down.nodes["C"].y


def test_reversed_directions_mirror_the_rank_axis():
    bottom_top = mermaid.parse("flowchart BT\n  A --> B --> C\n")
    mermaid.build_scene(bottom_top, mermaid.ui_font())
    assert bottom_top.nodes["A"].y > bottom_top.nodes["B"].y > bottom_top.nodes["C"].y

    right_left = mermaid.parse("flowchart RL\n  A --> B --> C\n")
    mermaid.build_scene(right_left, mermaid.ui_font())
    assert right_left.nodes["A"].x > right_left.nodes["B"].x > right_left.nodes["C"].x


def test_a_cycle_still_ranks_and_terminates():
    # Every retry loop is a cycle; ranking has to treat the edge that
    # closes it as a back edge rather than looping forever on it.
    chart = mermaid.parse("flowchart TD\n  A --> B\n  B --> C\n  C --> A\n")
    mermaid.build_scene(chart, mermaid.ui_font())
    assert [chart.nodes[k].rank for k in "ABC"] == [0, 1, 2]


def test_long_edge_is_routed_through_the_ranks_it_spans():
    # A --> C skips a rank. Without a virtual node on rank 1 the line
    # would be drawn straight across whatever sits there.
    chart = mermaid.parse("flowchart TD\n  A --> B --> C\n  A --> C\n")
    scene = mermaid.build_scene(chart, mermaid.ui_font())
    lines = [shape for shape in scene.shapes if isinstance(shape, sc.Line)]
    assert any(len(line.points) > 2 for line in lines)


def test_wider_label_makes_a_wider_node():
    narrow = mermaid.parse("flowchart TD\n  A[x]\n")
    wide = mermaid.parse("flowchart TD\n  A[a much longer label than x]\n")
    mermaid.build_scene(narrow, mermaid.ui_font())
    mermaid.build_scene(wide, mermaid.ui_font())
    assert wide.nodes["A"].w > narrow.nodes["A"].w


def test_edge_label_is_drawn_on_a_plate():
    # The plate is what keeps a label legible where it crosses its own
    # edge -- without it the line runs through the text.
    scene = build("flowchart LR\n  A -->|yes| B\n")
    label = next(s for s in scene.shapes if isinstance(s, sc.Text) and s.text == "yes")
    plate = next(s for s in scene.shapes if isinstance(s, sc.Rect) and s.fill == sc.LABEL_BG)
    assert plate.x <= label.x and plate.y <= label.y
    assert plate.x + plate.w >= label.x + label.w
    assert plate.y + plate.h >= label.y + label.h


@pytest.mark.parametrize("direction", ["LR", "TD"])
def test_a_long_edge_label_fits_between_the_nodes_it_joins(direction):
    # A label longer than the default gap between layers used to cover
    # the gap entirely, its ends hidden under the nodes either side.
    scene = build(f"flowchart {direction}\n"
                  "  A[One] -- \"a label far wider than the gap<br/>and two lines tall\" --> B[Two]\n")
    plate = next(s for s in scene.shapes if isinstance(s, sc.Rect) and s.fill == sc.LABEL_BG)
    first, second = sorted((s for s in scene.shapes
                            if isinstance(s, sc.Rect) and s.fill != sc.LABEL_BG),
                           key=lambda s: (s.x, s.y))
    if direction == "LR":
        assert first.x + first.w < plate.x
        assert plate.x + plate.w < second.x
    else:
        assert first.y + first.h < plate.y
        assert plate.y + plate.h < second.y


# -- edge curves -----------------------------------------------------------

# A --> C spans two ranks, so it is routed through a virtual node and bends.
BENT = "flowchart TD\n  A --> B --> C\n  A -- skips B --> C\n"
LINEAR = "%%{init: {'flowchart': {'curve': 'linear'}}}%%\n"
BASIS = "%%{init: {'flowchart': {'curve': 'basis'}}}%%\n"


@pytest.mark.parametrize("directive, settings", [
    ("%%{init: {'flowchart': {'curve': 'linear'}}}%%", {"flowchart": {"curve": "linear"}}),
    ('%%{init: {"flowchart": {"curve": "linear"}}}%%', {"flowchart": {"curve": "linear"}}),
    ("%%{ initialize: { flowchart: { curve: 'linear', }, } }%%", {"flowchart": {"curve": "linear"}}),
    ("%%{init: {'flowchart': }}%%", {}),     # unparseable: dropped, not fatal
    ("%% just a comment", {}),
])
def test_init_directive_is_read_leniently(directive, settings):
    assert parse_init(directive + "\nflowchart TD\n  A\n") == settings


def test_flowchart_edges_are_square_unless_asked_otherwise():
    assert mermaid.parse(BENT).curve == "orthogonal"
    assert mermaid.parse(LINEAR + BENT).curve == "linear"
    assert mermaid.parse(BASIS + BENT).curve == "basis"
    # Mermaid's step curves are right angles too; every other curve is a
    # way of rounding the corners, and draws as the basis spline.
    for curve, drawn in (("stepAfter", "orthogonal"), ("cardinal", "basis")):
        assert mermaid.parse(
            f"%%{{init: {{'flowchart': {{'curve': '{curve}'}}}}}}%%\n" + BENT).curve == drawn


def longest_line(scene):
    return max((s for s in scene.shapes if isinstance(s, sc.Line)),
               key=lambda line: len(line.points))


def test_a_curve_keeps_the_routes_ends_and_end_directions():
    route = [(0.0, 0.0), (0.0, 100.0), (100.0, 200.0), (100.0, 300.0)]
    path = layered.curved(route)
    assert path[0] == route[0] and path[-1] == route[-1]
    assert len(path) > len(route)

    def direction(a, b):
        length = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
        return round((b[0] - a[0]) / length, 6), round((b[1] - a[1]) / length, 6)
    # Arrowheads are drawn off the last segment, so they must not turn.
    assert direction(path[1], path[0]) == direction(route[1], route[0])
    assert direction(path[-2], path[-1]) == direction(route[-2], route[-1])
    assert layered.curved(route, "linear") == route


def test_a_curve_that_would_cut_through_a_box_is_not_drawn_through_it():
    route = [(0.0, 0.0), (0.0, 100.0), (100.0, 100.0)]
    corner_box = (2.0, 70.0, 26.0, 26.0)    # just inside the bend
    assert any(2 < x < 28 and 70 < y < 96 for x, y in layered.curved(route))
    path = layered.curved(route, boxes=[corner_box])
    assert not any(2 < x < 28 and 70 < y < 96 for x, y in path)
    assert path[0] == route[0] and path[-1] == route[-1]


def test_a_bent_edge_is_drawn_curved_and_its_label_sits_on_it():
    curved_line = longest_line(build(BASIS + BENT))
    straight_line = longest_line(build(LINEAR + BENT))
    # In, through and out of B's layer (see layered.Virtual).
    assert len(straight_line.points) == 4
    assert len(curved_line.points) > 3

    scene = build(BASIS + BENT)
    line = longest_line(scene)
    plate = next(s for s in scene.shapes if isinstance(s, sc.Rect) and s.fill == sc.LABEL_BG)
    centre = (plate.x + plate.w / 2, plate.y + plate.h / 2)
    nearest = layered._nearest_on(line.points, centre)
    assert ((nearest[0] - centre[0]) ** 2 + (nearest[1] - centre[1]) ** 2) ** 0.5 < 0.5


# Long edges from a source far off to one side, labels on two of them: the
# first version of this drew the dotted NS --> BF through Live-Front's
# corner and put a label over the arrowhead of U --> IIP.
FANNED = """flowchart TB
  U["Internet users"] --> LF["Live-Front<br/>nginx"]
  U --> FR["Freja3<br/>Apache · www, bra.dk"]
  U --> IIP["IIPImage<br/>iipsrv · img."]
  LF --> TH["Thor-1…4<br/>Nomad · Docker"]
  LF --> BF["Bifrost-1…4<br/>Nomad · JBoss"]
  FR --> BF
  TH <--> BF
  BF --> ORA[("Miro<br/>Oracle 11g")]
  TH --> NFS[("NetApp NFS")]
  IIP --> NFS
  NS["Nomad · Consul · Vault<br/>3 servers"] -.schedules.-> TH
  NS -.schedules.-> BF
"""


def sampled(line, steps=20):
    for (x1, y1), (x2, y2) in zip(line.points, line.points[1:]):
        for step in range(steps + 1):
            t = step / steps
            yield x1 + (x2 - x1) * t, y1 + (y2 - y1) * t


def inside(point, rect, inset=0.0):
    x, y = point
    return (rect.x + inset < x < rect.x + rect.w - inset
            and rect.y + inset < y < rect.y + rect.h - inset)


@pytest.mark.parametrize("prefix", ["", LINEAR, BASIS])
def test_a_long_edge_runs_past_the_boxes_in_the_layers_it_crosses(prefix):
    scene = build(prefix + FANNED)
    nodes = [s for s in scene.shapes if isinstance(s, sc.Rect) and s.fill == sc.NODE]
    for line in (s for s in scene.shapes if isinstance(s, sc.Line)):
        assert not any(inside(point, node, inset=1.0)
                       for point in sampled(line) for node in nodes)


def test_a_long_edge_label_gets_a_slot_of_its_own():
    scene = build(FANNED)
    plates = [s for s in scene.shapes if isinstance(s, sc.Rect) and s.fill == sc.LABEL_BG]
    lines = [s for s in scene.shapes if isinstance(s, sc.Line)]
    assert len(plates) == 2
    for plate in plates:
        # Exactly one line runs under it -- its own.
        crossing = [line for line in lines
                    if any(inside(point, plate) for point in sampled(line))]
        assert len(crossing) == 1


# -- right-angled edges ----------------------------------------------------

def square_routes(source, monkeypatch):
    """The scene's lines with their corners left square."""
    monkeypatch.setattr(layered, "rounded", lambda points, *args, **kwargs: list(points))
    return [s.points for s in build(source).shapes if isinstance(s, sc.Line)]


@pytest.mark.parametrize("direction", ["TD", "BT", "LR", "RL"])
def test_square_edges_run_only_along_and_across(direction, monkeypatch):
    source = FANNED.replace("flowchart TB", f"flowchart {direction}")
    for points in square_routes(source, monkeypatch):
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            assert abs(x1 - x2) < 1e-6 or abs(y1 - y2) < 1e-6


def test_square_edges_out_of_one_side_leave_from_ports_of_their_own(monkeypatch):
    starts = [points[0] for points in square_routes(
        "flowchart TD\n  A --> B & C & D\n", monkeypatch)]
    assert len({round(x, 3) for x, _y in starts}) == 3
    # ... in the order of where they're going, so they don't cross.
    ends = [points[-1] for points in square_routes(
        "flowchart TD\n  A --> B & C & D\n", monkeypatch)]
    assert [x for x, _y in sorted(starts)] == sorted(x for x, _y in starts)
    assert sorted(range(3), key=lambda i: starts[i][0]) == \
        sorted(range(3), key=lambda i: ends[i][0])


def test_a_square_edge_between_lined_up_nodes_is_straight(monkeypatch):
    [points] = square_routes("flowchart TD\n  A --> B\n", monkeypatch)
    assert len(points) == 2


def test_square_edges_never_share_a_run(monkeypatch):
    across = []
    for line, points in enumerate(square_routes(FANNED, monkeypatch)):
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            if abs(y1 - y2) < 1e-6 and abs(x1 - x2) > 1e-6:
                across.append((line, y1, min(x1, x2), max(x1, x2)))
    for i, (line_a, y_a, low_a, high_a) in enumerate(across):
        for line_b, y_b, low_b, high_b in across[i + 1:]:
            if line_a != line_b and abs(y_a - y_b) < 1e-6:
                assert high_a <= low_b or high_b <= low_a


@pytest.mark.parametrize("source", [
    "flowchart TD\n  A --> B & C\n  A -- label --> D\n",
    "flowchart TD\n  A -- one --> B\n  A -- two --> C\n  A --> D\n",
    FANNED,
])
def test_a_square_edge_label_hides_no_other_edge(source):
    # Right-angled edges run side by side a few pixels apart, so a plate
    # centred on one easily covers the next (layered.place_labels).
    scene = build(source)
    plates = [s for s in scene.shapes if isinstance(s, sc.Rect) and s.fill == sc.LABEL_BG]
    lines = [s for s in scene.shapes if isinstance(s, sc.Line)]
    for plate in plates:
        under = [line for line in lines if any(inside(p, plate) for p in sampled(line))]
        assert len(under) == 1


# Every cylinder has one edge in and one out, and Nomad's top is too
# narrow for its three edges at the usual port spacing.
PIPELINE = """flowchart TB
  G["GitHub / Forgejo<br/>/ SVN on Codebase"] --> J["Jenkins<br/>Docker · JDK 8–21"]
  J -->|"Maven release"| AF[("Artifactory")]
  J -->|"Jib / docker build"| DG[("degas:5000<br/>registry")]
  J -->|"nomad job run"| NM["Nomad"]
  AF -->|"artifact block"| NM
  DG -->|"docker pull"| NM
"""


def test_a_lone_square_edge_meets_a_cylinder_in_the_middle():
    # Off-centre, it lands on the slope of the rim and looks like a miss.
    scene = build(PIPELINE)
    tops = sorted((s for s in scene.shapes if isinstance(s, sc.Ellipse)), key=lambda s: s.x)
    bodies = sorted((s for s in scene.shapes if isinstance(s, sc.Polygon)),
                    key=lambda s: min(x for x, _y in s.points))
    ends = [p for s in scene.shapes if isinstance(s, sc.Line)
            for p in (s.points[0], s.points[-1])]
    assert len(tops) == len(bodies) == 2
    for top, body in zip(tops, bodies):
        bottom = max(y for _x, y in body.points)
        mine = [x for x, y in ends
                if top.x <= x <= top.x + top.w and top.y - 1 <= y <= bottom + 1]
        assert len(mine) == 2       # one in, one out
        assert all(x == pytest.approx(top.x + top.w / 2) for x in mine)


def test_a_crowded_side_squeezes_its_ports_to_keep_an_edge_straight(monkeypatch):
    routes = square_routes(PIPELINE, monkeypatch)
    # Artifactory --> Nomad: straight down, both ends in the middle of the
    # cylinder, with Nomad's other two ports packed in beside it.
    assert sum(1 for points in routes if len(points) == 2) >= 3


# The sources are packed tight across their layer, so the last sweep,
# pulling them toward their targets, can't line them up; the targets have
# the room to come to meet them.
PACKED_SOURCES = """flowchart LR
  FA["app"]
  FA -- "SMTP :1025" --> SNLB["internal NLB"] --> MP["mailpit<br/>(catches all mail)"]
  FA -- "HTTP :80" --> DALB["internal ALB"] --> DMS["proxy"] --> GW(("AS4<br/>gateway"))
  BR(("on-prem")) -- "nightly dumps" --> S3[("dump")]
  FIVE(("Fivetran → Snowflake")) -- "5432" --> PG[("Postgres")]
  ECS["ECS tasks"] -- "logs, metrics, APM" --> DD(("Datadog"))
"""


def test_a_one_to_one_edge_runs_straight_where_its_target_has_room(monkeypatch):
    routes = square_routes(PACKED_SOURCES, monkeypatch)
    # All but the two out of "app", which fan out of one node.
    assert sum(1 for points in routes if len(points) == 2) == len(routes) - 2


def test_fencing_a_group_keeps_a_straight_edge_straight():
    # A long edge passing a frame, one of its virtuals grown for a label:
    # pushed clear of the frame cell by cell, the wide one went further
    # than the rest and the edge came out kinked beside the frame.
    from types import SimpleNamespace
    def node(key, rank, w):
        return SimpleNamespace(key=key, w=w, h=20.0, x=0.0, y=0.0, rank=rank, order=0)
    # The frame is as wide as G0 in both layers, so V1, packed against
    # the narrow G1, starts out inside it.
    g0, g1 = node("G0", 0, 200.0), node("G1", 1, 20.0)
    v0, v1 = layered.Virtual("v0", 0), layered.Virtual("v1", 1)
    v1.w = 73.0
    layers = [[v0, g0], [v1, g1]]
    up = {"G0": [], "G1": [], "v0": [], "v1": ["v0"]}
    down = {"G0": [], "G1": [], "v0": ["v1"], "v1": []}
    paths = {"G0": ("g",), "G1": ("g",)}
    layered.place(layers, up, down, True, [46.0, 46.0], paths, {"g": 0})
    assert v0.x == pytest.approx(v1.x)
    assert v1.x + v1.w / 2 + layered.NODE_GAP <= g0.x - g0.w / 2 + 1e-6


def circle_lines(label):
    """How many lines a circle node's label is drawn on."""
    measurer = textlib.Measurer(mermaid.ui_font())
    return round(flowchart._circle_text_size(label, measurer)[1] / measurer.line_px)


def test_a_circle_wraps_its_label_to_a_square_block():
    # On one line, the widest the usual wrap allows, the circle would be
    # mostly empty above and below the text.
    assert circle_lines("Fivetran → Snowflake") == 2
    scene = build('flowchart LR\n  A(("Fivetran → Snowflake"))\n')
    wide = build('flowchart LR\n  A["Fivetran → Snowflake"]\n')
    assert scene.width < wide.width


def test_a_circle_keeps_its_authors_line_breaks():
    assert circle_lines("SKAT AS4<br/>gateway".replace("<br/>", "\n")) == 2


def test_a_circle_wraps_its_label_only_between_words():
    # Pango would break after the hyphen: "BR on-" / "prem".
    assert circle_lines("BR on-prem") == 1


def test_rounded_corners_keep_the_end_segments():
    route = [(0.0, 0.0), (0.0, 50.0), (80.0, 50.0), (80.0, 100.0)]
    path = layered.rounded(route)
    assert path[0] == route[0] and path[-1] == route[-1]
    assert path[1] == (0.0, 45.0) and path[-2] == (80.0, 55.0)


def test_a_run_across_hops_a_run_along_where_they_cross():
    across = [(0.0, 50.0), (100.0, 50.0)]
    along = [(40.0, 0.0), (40.0, 100.0)]
    assert layered.crossings({"a": across, "b": along}, vertical=True) == {"a": {0: [40.0]}}
    # Drawn left to right, the other way: the across run is the vertical.
    assert layered.crossings({"a": across, "b": along}, vertical=False) == {"b": {0: [50.0]}}


@pytest.mark.parametrize("along", [
    [(40.0, 0.0), (40.0, 50.0)],        # a T: ends on the run
    [(0.0, 50.0), (0.0, 100.0)],        # meets it at its end
])
def test_lines_that_only_meet_dont_hop(along):
    assert layered.crossings({"a": [(0.0, 50.0), (100.0, 50.0)], "b": along}, True) == {}


def test_a_hop_is_a_half_circle_up_over_the_crossing():
    path = layered.rounded([(0.0, 50.0), (100.0, 50.0)], hops={0: [40.0]})
    assert path[0] == (0.0, 50.0) and path[-1] == (100.0, 50.0)
    top = min(path, key=lambda p: p[1])
    assert top == pytest.approx((40.0, 50.0 - layered.HOP_RADIUS))
    off = [x for x, y in path if y < 50.0 - 1e-6]
    assert min(off) >= 40.0 - layered.HOP_RADIUS and max(off) <= 40.0 + layered.HOP_RADIUS


def test_crossings_close_together_are_hopped_as_one():
    path = layered.rounded([(0.0, 50.0), (100.0, 50.0)], hops={0: [40.0, 44.0]})
    raised = [x for x, y in path if y == pytest.approx(50.0 - layered.HOP_RADIUS)]
    # One bump, level across both crossings, not two half circles.
    assert min(raised) == pytest.approx(40.0) and max(raised) == pytest.approx(44.0)
    assert sum(1 for (x1, y1), (x2, y2) in zip(path, path[1:])
               if y1 == pytest.approx(50.0) and y2 < 50.0 - 1e-6) == 1


def test_a_crossing_too_close_to_a_corner_is_not_hopped():
    route = [(0.0, 0.0), (0.0, 50.0), (100.0, 50.0)]
    near = layered.CORNER_RADIUS + layered.HOP_RADIUS - 1
    assert layered.rounded(route, hops={1: [near]}) == layered.rounded(route)


@pytest.mark.parametrize("direction", ["TB", "LR"])
def test_crossing_square_edges_are_drawn_with_a_hop(direction, monkeypatch):
    drawn = []
    rounded = layered.rounded

    def spy(points, *args, hops=None, **kwargs):
        drawn.append(hops)
        return rounded(points, *args, hops=hops, **kwargs)
    monkeypatch.setattr(layered, "rounded", spy)
    build(FANNED.replace("flowchart TB", f"flowchart {direction}"))
    assert any(drawn)


def test_a_source_is_ranked_just_above_what_it_points_at():
    # Longest-path ranking left S on top with an edge spanning three
    # ranks, crossing everything in between.
    chart = mermaid.parse("flowchart TD\n  A --> B --> C --> D\n  S --> D\n")
    mermaid.build_scene(chart, mermaid.ui_font())
    assert chart.nodes["S"].rank == chart.nodes["D"].rank - 1


def test_transpose_swaps_neighbours_that_cross():
    class Cell:
        def __init__(self, key, order):
            self.key, self.order = key, order
    top = [Cell("a", 0), Cell("b", 1)]
    bottom = [Cell("x", 0), Cell("y", 1)]
    up = {"a": [], "b": [], "x": ["b"], "y": ["a"]}
    down = {"a": ["y"], "b": ["x"], "x": [], "y": []}
    layers = [top, bottom]
    assert layered.count_crossings(layers, down) == 1
    layered.transpose(layers, up, down, {})
    assert layered.count_crossings(layers, down) == 0


def test_a_cylinder_has_an_elliptical_top_and_edges_meet_its_rim():
    scene = build("flowchart TD\n  A --> B[(db)]\n")
    [top] = [s for s in scene.shapes if isinstance(s, sc.Ellipse)]
    [line] = [s for s in scene.shapes if isinstance(s, sc.Line)]
    x, y = line.points[-1]
    # A centred edge arrives at the top of the rim, which is the ellipse's
    # top, not the middle of the box.
    assert x == pytest.approx(top.x + top.w / 2)
    assert y == pytest.approx(top.y)


def test_an_off_centre_edge_meets_a_cylinder_on_its_rim():
    cell = flowchart.FlowNode("B", "db", "cylinder")
    cell.x, cell.y, cell.w, cell.h, cell.cap = 0.0, 0.0, 100.0, 60.0, 10.0
    x, y = layered.boundary_point(cell, (40.0, -300.0))
    # On the top ellipse: centred 10 below the box's top, 50 by 10.
    assert (x / 50) ** 2 + ((y - (-30 + 10)) / 10) ** 2 == pytest.approx(1.0)
    assert y > -30


# -- sequence diagrams -----------------------------------------------------

SEQUENCE = """sequenceDiagram
    participant W as Window
    participant D as Document
    W->>D: load()
    activate D
    D-->>W: tree
    deactivate D
    Note over W,D: reload watches the file
    loop every save
        W->>W: re-render
    end
"""


def test_sequence_participants_keep_declaration_order_and_labels():
    diagram = mermaid.parse(SEQUENCE)
    assert list(diagram.participants) == ["W", "D"]
    assert diagram.participants["W"].label == "Window"


def test_autonumber_counts_every_message_even_while_hidden():
    # As mermaid's renderer does: `off` hides the numbers, the count goes on.
    diagram = mermaid.parse(
        "sequenceDiagram\n  autonumber 10 5\n  A->>B: one\n  autonumber off\n"
        "  B->>A: two\n  autonumber\n  A->>B: three\n")
    messages = [e for e in diagram.events if isinstance(e, sequence.Message)]
    assert [m.number for m in messages] == ["10", None, "20"]


def test_autonumber_draws_each_number_in_a_disc():
    scene = build("sequenceDiagram\n  autonumber\n  A->>B: one\n  B->>A: two\n")
    assert {"1", "2"} <= set(texts(scene))
    assert sum(isinstance(s, sc.Ellipse) for s in scene.shapes) == 2


def test_sequence_alias_breaks_lines_like_any_other_label():
    diagram = mermaid.parse(
        'sequenceDiagram\n  actor D as Developer<br/>(Slack)\n  participant G as "GitHub"\n'
        "  D->>G: push\n")
    assert diagram.participants["D"].label == "Developer\n(Slack)"
    # mermaid keeps a sequence label's quotes; only flowcharts unquote.
    assert diagram.participants["G"].label == '"GitHub"'


def test_sequence_participant_is_declared_by_first_use():
    diagram = mermaid.parse("sequenceDiagram\n  A->>B: hi\n")
    assert list(diagram.participants) == ["A", "B"]
    assert diagram.participants["A"].label == "A"


@pytest.mark.parametrize("arrow, dotted, head", [
    ("->", False, None),
    ("-->", True, None),
    ("->>", False, "arrow"),
    ("-->>", True, "arrow"),
    ("-x", False, "cross"),
    ("--x", True, "cross"),
    ("-)", False, "open"),
    ("--)", True, "open"),
])
def test_sequence_arrow_forms(arrow, dotted, head):
    diagram = mermaid.parse(f"sequenceDiagram\n  A{arrow}B: hi\n")
    message = diagram.events[0]
    assert (message.dotted, message.head, message.text) == (dotted, head, "hi")


def test_sequence_activation_shorthand_is_an_activation():
    diagram = mermaid.parse("sequenceDiagram\n  A->>+B: go\n  B-->>-A: done\n")
    assert [event.activation for event in diagram.events] == ["+", "-"]


def test_sequence_blocks_must_be_closed():
    with pytest.raises(mermaid.Unsupported):
        mermaid.parse("sequenceDiagram\n  loop forever\n    A->>B: hi\n")
    with pytest.raises(mermaid.Unsupported):
        mermaid.parse("sequenceDiagram\n  A->>B: hi\n  end\n")


def test_sequence_scene_has_two_boxes_per_participant():
    # Participants are repeated at the foot of the diagram, which is what
    # keeps a tall one readable without scrolling back to the top.
    scene = build(SEQUENCE)
    labels = [shape.text for shape in scene.shapes if isinstance(shape, sc.Text)]
    assert labels.count("Window") == 2
    assert labels.count("Document") == 2


def test_sequence_rows_run_down_the_page_in_source_order():
    scene = build(SEQUENCE)
    lines = [shape for shape in scene.shapes if isinstance(shape, sc.Line) and not shape.dashed]
    # The solid message lines (load(), and the self-call's box) come in
    # the order they were written.
    assert lines == sorted(lines, key=lambda line: min(y for _x, y in line.points))


def test_sequence_lifelines_span_between_the_two_boxes():
    scene = build("sequenceDiagram\n  A->>B: hi\n")
    dashed = [shape for shape in scene.shapes
              if isinstance(shape, sc.Line) and shape.dashed]
    assert len(dashed) == 2                       # one lifeline per participant
    for line in dashed:
        (x0, y0), (x1, y1) = line.points
        assert x0 == x1                           # vertical
        assert y1 > y0


def test_sequence_note_over_two_participants_spans_them():
    scene = build("sequenceDiagram\n  A->>B: hi\n  Note over A,B: shared\n")
    note = next(s for s in scene.shapes if isinstance(s, sc.Rect) and s.fill == sc.NOTE)
    label = next(s for s in scene.shapes if isinstance(s, sc.Text) and s.text == "shared")
    assert note.x <= label.x and note.x + note.w >= label.x + label.w


def test_sequence_frame_encloses_the_rows_inside_it():
    scene = build(SEQUENCE)
    frame = next(s for s in scene.shapes
                 if isinstance(s, sc.Rect) and s.dashed and s.stroke == sc.EDGE)
    inner = next(s for s in scene.shapes
                 if isinstance(s, sc.Text) and s.text == "re-render")
    assert frame.y < inner.y
    assert frame.y + frame.h > inner.y + inner.h


# -- class and ER diagrams -------------------------------------------------

CLASSES = """classDiagram
    class Renderer {
        <<walker>>
        +render(tree, buffer)
        -_emit_table()
    }
    class PrintItem
    PrintItem : +str kind
    Renderer --> "many" PrintItem : appends
    Scene <|-- FlowScene
    Scene *-- Shape
    Scene o-- Palette
    Renderer ..> Scene : creates
"""


def shown(members):
    return [member.text for member in members]


def test_class_members_come_from_both_spellings():
    diagram = mermaid.parse(CLASSES)
    assert shown(diagram.entities["Renderer"].members) == ["+render(tree, buffer)", "-_emit_table()"]
    assert diagram.entities["Renderer"].stereotype == "walker"
    # `PrintItem : +str kind` declares one member at a time.
    assert shown(diagram.entities["PrintItem"].members) == ["+str kind"]


@pytest.mark.parametrize("written, text, italic, underline", [
    ("+run(int n) bool", "+run(int n) : bool", False, False),
    ("-go()$", "-go()", False, True),
    ("count()* int", "count() : int", True, False),
    ("+int total$", "+int total", False, True),
    # mermaid converts each comma-separated piece on its own, so this is
    # what it shows (recorded: lectern:probe-nested-generics).
    ("Map~String, List~Int~~ index", "Map~String, List<Int~> index", False, False),
    ("+put(K~T~ k) V~T~", "+put(K<T> k) : V<T>", False, False),
])
def test_a_class_member_is_shown_as_mermaid_shows_it(written, text, italic, underline):
    # Return types after ` : `, generics as <T>, and the abstract `*` and
    # static `$` drawn as italics and underlining rather than as text.
    [member] = mermaid.parse(f"classDiagram\n  class A {{\n    {written}\n  }}\n").entities["A"].members
    assert (member.text, member.italic, member.underline) == (text, italic, underline)


def test_a_static_member_is_drawn_underlined():
    scene = build("classDiagram\n  class A {\n    +int total$\n  }\n")
    member = next(s for s in scene.shapes if isinstance(s, sc.Text) and s.text == "+int total")
    assert member.underline and not member.italic


def test_class_declaration_forms():
    diagram = mermaid.parse(
        "classDiagram\n"
        '  class Recipe["Fineart / Tools::Deploy"]\n'
        "  class Recipe {\n    call()\n  }\n"            # keeps the label
        "  class Container { definition_hash() }\n"      # mermaid: one member
        "  class Port { <<interface>> }\n"
        "  class Box~Shape~\n"
        "  class Styled:::hot\n"
        "  Recipe --> Container\n")
    entities = diagram.entities
    assert set(entities) == {"Recipe", "Container", "Port", "Box", "Styled"}
    assert (entities["Recipe"].title, shown(entities["Recipe"].members)) == (
        "Fineart / Tools::Deploy", ["call()"])
    assert shown(entities["Container"].members) == ["definition_hash()"]
    assert (entities["Port"].stereotype, entities["Port"].members) == ("interface", [])
    assert entities["Box"].title == "Box<Shape>"


@pytest.mark.parametrize("line", ['class A["unterminated', "class A extra words", "class A { x"])
def test_an_unparsed_class_declaration_falls_back(line):
    """These used to keep the first word and quietly drop the rest."""
    with pytest.raises(mermaid.Unsupported):
        mermaid.parse(f"classDiagram\n  {line}\n")


@pytest.mark.parametrize("relation, tail, head, dashed", [
    ("<|--", "triangle", None, False),
    ("*--", "diamond", None, False),
    ("o--", "diamond-open", None, False),
    ("-->", None, "arrow", False),
    ("..>", None, "arrow", True),
    ("..|>", None, "triangle", True),
    ("--", None, None, False),
])
def test_uml_relation_glyphs(relation, tail, head, dashed):
    diagram = mermaid.parse(f"classDiagram\n  A {relation} B\n")
    edge = diagram.relations[0]
    assert (edge.tail, edge.head, edge.dashed) == (tail, head, dashed)


def test_class_cardinality_is_kept_per_end():
    diagram = mermaid.parse('classDiagram\n  A "1" --> "many" B : has\n')
    edge = diagram.relations[0]
    assert (edge.src_card, edge.dst_card, edge.label) == ("1", "many", "has")


def test_class_scene_draws_a_divider_between_compartments():
    scene = build(CLASSES)
    # The Renderer box has members, so it gets a rule under its title; a
    # bare class (DiagramView-style) does not.
    solid_rules = [s for s in scene.shapes
                   if isinstance(s, sc.Line) and len(s.points) == 2
                   and s.points[0][1] == s.points[1][1] and s.head is None and s.tail is None]
    assert solid_rules


@pytest.mark.parametrize("relation, tail, head", [
    ("||--o{", "one", "zero-many"),
    ("||--|{", "one", "one-many"),
    ("}o--||", "zero-many", "one"),
    ("|o..o|", "zero-one", "zero-one"),
])
def test_er_crow_foot_cardinalities(relation, tail, head):
    diagram = mermaid.parse(f"erDiagram\n  A {relation} B : rel\n")
    edge = diagram.relations[0]
    assert (edge.tail, edge.head) == (tail, head)
    assert edge.dashed == (".." in relation)


def test_er_attribute_block_becomes_members():
    diagram = mermaid.parse(
        "erDiagram\n"
        "  DOCUMENT ||--o{ BLOCK : contains\n"
        "  DOCUMENT {\n"
        "    string path\n"
        "    string title\n"
        "  }\n"
    )
    assert shown(diagram.entities["DOCUMENT"].members) == ["string path", "string title"]
    assert diagram.entities["BLOCK"].members == []


def test_an_er_entity_may_have_an_alias_and_a_style():
    diagram = mermaid.parse("erDiagram\n  p[Person]:::warm {\n    `my.type` name\n  }\n")
    assert diagram.entities["p"].title == "Person"
    assert shown(diagram.entities["p"].members) == ["my.type name"]


@pytest.mark.parametrize("opening", ["P extra {", "class P {"])
def test_an_er_entity_opening_with_more_on_the_line_falls_back(opening):
    # Taking the first word and dropping the rest would be a silent
    # mis-parse; mermaid reads neither.
    with pytest.raises(Unsupported):
        mermaid.parse(f"erDiagram\n  {opening}\n    string name\n  }}\n")


def test_er_unclosed_block_is_refused():
    with pytest.raises(mermaid.Unsupported):
        mermaid.parse("erDiagram\n  A ||--|| B : rel\n  A {\n    string x\n")


def test_entity_boxes_do_not_overlap():
    diagram = mermaid.parse(CLASSES)
    mermaid.build_scene(diagram, mermaid.ui_font())
    entities = list(diagram.entities.values())
    for index, a in enumerate(entities):
        for b in entities[index + 1:]:
            gap_x = abs(a.x - b.x) - (a.w + b.w) / 2
            gap_y = abs(a.y - b.y) - (a.h + b.h) / 2
            assert gap_x > 0 or gap_y > 0, f"{a.key} overlaps {b.key}"


def test_class_direction_statement_is_honoured():
    source = "classDiagram\n  direction {}\n  A --> B\n"
    left_right = mermaid.parse(source.format("LR"))
    mermaid.build_scene(left_right, mermaid.ui_font())
    assert left_right.entities["A"].x < left_right.entities["B"].x

    top_down = mermaid.parse(source.format("TB"))
    mermaid.build_scene(top_down, mermaid.ui_font())
    assert top_down.entities["A"].y < top_down.entities["B"].y


def test_class_diagram_v2_is_the_same_type():
    assert mermaid.parse("classDiagram-v2\n  A --> B\n").entities.keys() == {"A", "B"}


# -- renderer integration --------------------------------------------------

def render(markdown_text):
    tree = SyntaxTreeNode(_PARSER.parse(markdown_text))
    buffer = Gtk.TextBuffer(tag_table=create_tag_table(dark=False))
    renderer = MarkdownRenderer()
    renderer.render(tree, buffer)
    return renderer, buffer


def test_mermaid_fence_becomes_a_diagram_widget():
    renderer, _buffer = render("```mermaid\nflowchart LR\n  A --> B\n```\n")
    assert len(renderer.diagrams) == 1
    assert [item.kind for item in renderer.print_model] == ["diagram"]
    # Print keeps the parsed model, not the screen's scene: paper lays it
    # out again in the print font.
    assert renderer.print_model[0].diagram is not None
    assert len(renderer._pending_anchors) == 1


def test_unsupported_mermaid_falls_back_to_a_code_block():
    renderer, buffer = render("```mermaid\ngantt\n  title Nope\n```\n")
    assert renderer.diagrams == []
    assert [item.kind for item in renderer.print_model] == ["code-block"]
    start, end = buffer.get_bounds()
    assert "gantt" in buffer.get_text(start, end, False)


def test_other_fences_are_untouched():
    renderer, _buffer = render("```python\nx = 1\n```\n")
    assert renderer.diagrams == []
    assert [item.kind for item in renderer.print_model] == ["code-block"]


# -- links -----------------------------------------------------------------

from lectern.mermaid.common import parse_click


@pytest.mark.parametrize("statement, expected", [
    ('click A href "a.md"', ("A", "a.md")),
    ('click A "a.md"', ("A", "a.md")),
    ('click A href "https://x.org" "a tooltip" _blank', ("A", "https://x.org")),
    ('link A "a.md"', ("A", "a.md")),
    ("click A callback", None),
    ('click A call callback("x")', None),
    ('click A href ""', None),
])
def test_click_statements(statement, expected):
    assert parse_click(statement) == expected


def links(scene):
    return [shape for shape in scene.shapes if isinstance(shape, sc.Link)]


def test_flowchart_click_links_its_node():
    chart = mermaid.parse('flowchart LR\n  A --> B\n  click B href "b.md"\n')
    assert chart.nodes["A"].href is None
    assert chart.nodes["B"].href == "b.md"


def test_a_click_before_the_node_it_names_is_dropped():
    # mermaid links only a node that exists by the time the click is read.
    chart = mermaid.parse('flowchart LR\n  click B "b.md"\n  A --> B\n')
    assert chart.nodes["B"].href is None


def test_a_click_on_an_undeclared_id_conjures_no_node():
    chart = mermaid.parse('flowchart LR\n  A --> B\n  click C "c.md"\n')
    assert set(chart.nodes) == {"A", "B"}


def test_a_link_covers_exactly_its_nodes_box():
    scene = build('flowchart LR\n  A[Start] --> B[Linked]\n  click B "b.md"\n')
    [link] = links(scene)
    [box] = [b for b in boxes(scene)
             if any(t.text == "Linked" and b.x <= t.x and t.x + t.w <= b.x + b.w
                    for t in scene.shapes if isinstance(t, sc.Text))]
    assert (link.x, link.y, link.w, link.h) == (box.x, box.y, box.w, box.h)
    assert scene.link_at(link.x + link.w / 2, link.y + link.h / 2) == "b.md"
    assert scene.link_at(link.x - 1, link.y - 1) is None


def test_unlinked_diagram_has_no_links():
    assert not build(SIMPLE).has_links


def test_class_diagram_click_and_link_both_link_a_class():
    diagram = mermaid.parse(
        'classDiagram\n  A --> B\n  A --> C\n  click A href "a.md"\n  link B "b.md"\n')
    assert diagram.entities["A"].href == "a.md"
    assert diagram.entities["B"].href == "b.md"
    assert diagram.entities["C"].href is None
    scene = build('classDiagram\n  class A {\n    int id\n  }\n  click A "a.md"\n')
    [link] = links(scene)
    assert scene.link_at(link.x + 1, link.y + link.h - 1) == "a.md"


def test_er_diagram_click_is_still_refused():
    # mermaid's erDiagram has no `click`: it reads the words as entity
    # names, so drawing it as a link would show what mermaid doesn't.
    with pytest.raises(mermaid.Unsupported):
        mermaid.parse('erDiagram\n  A ||--o{ B : has\n  click A "a.md"\n')


def test_links_are_not_painted():
    import cairo
    from lectern.mermaid import draw as drawlib
    from lectern.tags import diagram_palette
    scene = build('flowchart LR\n  A --> B\n  click A "a.md"\n')
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 10, 10)
    drawlib.draw_scene(cairo.Context(surface), scene, diagram_palette(False), mermaid.ui_font())


def test_diagram_widget_hit_tests_through_its_scale():
    from lectern.mermaid.view import DiagramView
    scene = build('flowchart LR\n  A --> B\n  click B "b.md"\n')
    view = DiagramView(scene, mermaid.ui_font())
    [link] = links(scene)
    cx, cy = link.x + link.w / 2, link.y + link.h / 2
    assert view.link_at(cx, cy) == "b.md"
    view.set_available_width(scene.width / 2)   # drawn at half size
    assert view.link_at(cx / 2, cy / 2) == "b.md"


# -- label placement -------------------------------------------------------

from lectern.mermaid import layered


def plates(scene):
    return [(s.x, s.y, s.w, s.h) for s in scene.shapes
            if isinstance(s, sc.Rect) and s.fill == sc.LABEL_BG]


def overlap(a, b):
    return layered._overlap_area(a, b) > 0


# A table with five references out, two of them to the same layer right
# next to each other: their labels' midpoints sat on top of each other.
FAN_OUT = """classDiagram
    class Penalty {
        int ID PK
        int Haendelse
        int Spiller FK
        int Forseelse FK
        int Kamp FK
        int Hold FK
    }
    class Forseelser {
        int ID PK
    }
    class Haendelser {
        int ID PK
    }
    class Hold {
        int ID PK
    }
    class kamp {
        int ID PK
    }
    class Person {
        int ID PK
    }
    class Indberetning {
        int ID PK
        int PenaltyID FK
    }
    Haendelser <.. Penalty : Haendelse (implied)
    Penalty <-- Indberetning : PenaltyID
    Forseelser <-- Penalty : Forseelse
    Hold <-- Penalty : Hold
    kamp <-- Penalty : Kamp
    Person <-- Penalty : Spiller
"""


def test_class_diagram_labels_do_not_overlap_each_other_or_boxes():
    scene = build(FAN_OUT)
    labels = plates(scene)
    assert len(labels) == 6
    for i, a in enumerate(labels):
        assert not any(overlap(a, b) for b in labels[i + 1:])
    entity_boxes = [(b.x, b.y, b.w, b.h) for b in boxes(scene) if b.fill == sc.NODE]
    for plate in labels:
        assert not any(overlap(plate, box) for box in entity_boxes)


def test_flowchart_labels_fanning_out_do_not_overlap():
    scene = build("flowchart TD\n"
                  "  A -->|first label| B\n  A -->|second label| C\n  A -->|third label| D\n")
    labels = plates(scene)
    assert len(labels) == 3
    for i, a in enumerate(labels):
        assert not any(overlap(a, b) for b in labels[i + 1:])


def test_an_unobstructed_label_keeps_its_midpoint():
    points = [(0.0, 0.0), (0.0, 100.0)]
    assert layered.place_labels([(points, 20.0, 10.0)]) == [layered.label_position(points)]


def test_a_blocked_label_slides_along_its_own_edge():
    points = [(0.0, 0.0), (0.0, 100.0)]
    [(x, y)] = layered.place_labels([(points, 20.0, 10.0)], obstacles=[(-15.0, 40.0, 30.0, 20.0)])
    assert x == 0.0                 # still on the edge
    assert not 40.0 - 7.0 < y < 60.0 + 7.0


def test_the_first_diagram_in_a_process_measures_like_every_later_one(monkeypatch):
    # The shared measuring context used to be unhinted only by the first
    # layout made on it, after Measurer had already read the line height
    # off it hinted -- so whichever diagram came first had taller lines,
    # and a test passed or failed depending on what ran before it.
    from lectern.mermaid import text
    monkeypatch.setattr(text, "_scratch_context", None)
    first = plates(build(FAN_OUT))
    assert plates(build(FAN_OUT)) == first


@pytest.mark.parametrize("statement", [
    'A["player & team stats"] --> B',
    'A["review &amp; merge"] --> B',
    "A(Tom & Jerry) --> B{yes & no}",
    "A -->|this & that| B",
    "A -- this & that --> B",
])
def test_an_ampersand_in_a_label_is_just_text(statement):
    chart = mermaid.parse(f"flowchart LR\n  {statement}\n")
    assert set(chart.nodes) == {"A", "B"}
    assert len(chart.edges) == 1


@pytest.mark.parametrize("statement, expected", [
    ("A & B --> C", [("A", "C"), ("B", "C")]),
    ("A --> B & C", [("A", "B"), ("A", "C")]),
    ("A & B --> C & D", [("A", "C"), ("A", "D"), ("B", "C"), ("B", "D")]),
    ("A --> B & C --> D", [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")]),
    ('A["x"] & B', []),
])
def test_a_node_list_links_every_node_on_each_side(statement, expected):
    chart = mermaid.parse(f"flowchart LR\n  {statement}\n")
    assert [(e.src, e.dst) for e in chart.edges] == expected


def test_a_node_list_keeps_shapes_and_the_link_label():
    chart = mermaid.parse('flowchart LR\n  A[("db")] & B{{hex}} -. "reads" .-> C\n')
    assert chart.nodes["A"].shape == "cylinder"
    assert chart.nodes["B"].shape == "hexagon"
    assert {e.label for e in chart.edges} == {"reads"}
    assert {e.style for e in chart.edges} == {"dotted"}

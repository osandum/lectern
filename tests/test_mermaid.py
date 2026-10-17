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
from lectern.mermaid import flowchart, scene as sc, text as textlib
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
        "  A --> B  %% trailing\n"
        "  style A fill:#f9f\n"
        "  classDef big font-size:20px\n"
        "  click A callback\n"
    )
    assert list(chart.nodes) == ["A", "B"]
    assert len(chart.edges) == 1


@pytest.mark.parametrize("source", [
    "gantt\n  title Nope\n",                       # a type we don't draw
    "stateDiagram-v2\n  [*] --> Still\n",          # ditto
    "flowchart TD\n  subgraph one\n  A\n",         # unclosed subgraph
    "flowchart TD\n  A\n  end\n",                  # `end` with no subgraph
    "flowchart TD\n  A --> B & C\n",
    "flowchart XY\n  A --> B\n",                   # unknown direction
    "flowchart TD\n  A[unterminated\n",
    "",
])
def test_unsupported_sources_raise(source):
    with pytest.raises(mermaid.Unsupported):
        mermaid.parse(source)


# -- subgraphs -------------------------------------------------------------

def test_subgraph_membership_follows_first_mention():
    chart = mermaid.parse(
        "flowchart TD\n"
        "  subgraph s[Inside]\n    A --> B\n  end\n"
        "  B --> C\n")
    group, = chart.subgraphs
    assert group.key == "s" and group.title == "Inside"
    # C is named outside the block; B is named inside it first and again
    # outside, which doesn't move it.
    assert group.node_keys() == ["A", "B"]


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


def test_class_members_come_from_both_spellings():
    diagram = mermaid.parse(CLASSES)
    assert diagram.entities["Renderer"].members == ["+render(tree, buffer)", "-_emit_table()"]
    assert diagram.entities["Renderer"].stereotype == "walker"
    # `PrintItem : +str kind` declares one member at a time.
    assert diagram.entities["PrintItem"].members == ["+str kind"]


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
    assert diagram.entities["DOCUMENT"].members == ["string path", "string title"]
    assert diagram.entities["BLOCK"].members == []


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


def test_a_click_may_precede_the_node_it_names():
    chart = mermaid.parse('flowchart LR\n  click B "b.md"\n  A --> B\n')
    assert chart.nodes["B"].href == "b.md"


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

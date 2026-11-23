"""State diagrams: what mermaid's state parser makes of a source, and how
Lectern draws it.

What a source *means* is held to mermaid's own reading by the
conformance corpus (mermaid_corpus/, family `state`); these pin down the
parts of that reading worth stating outright, and the drawing. As in
test_mermaid.py, geometry is asserted as relations, not coordinates.
"""
import pytest

from lectern import mermaid
from lectern.mermaid import layered, scene as sc, state

from test_mermaid import boxes, build, render, texts


def nodes(source):
    return {node["id"]: node for node in mermaid.parse(source).nodes}


def frames(scene):
    """The frames, not their title plates, which are filled the same."""
    return [shape for shape in scene.shapes
            if isinstance(shape, sc.Rect) and shape.fill == sc.CLUSTER and shape.stroke]


def lines(scene):
    return [shape for shape in scene.shapes if isinstance(shape, sc.Line)]


# -- reading ---------------------------------------------------------------

def test_both_headers_are_state_diagrams():
    for header in ("stateDiagram", "stateDiagram-v2"):
        assert isinstance(mermaid.parse(f"{header}\n  A --> B\n"), state.Model)


def test_star_is_the_start_or_end_of_the_block_it_is_in():
    found = nodes("stateDiagram-v2\n  [*] --> A\n  state A {\n    [*] --> a\n    a --> [*]\n  }\n"
                  "  A --> [*]\n")
    assert found["root_start"]["shape"] == "stateStart"
    assert found["root_end"]["shape"] == "stateEnd"
    assert found["A_start"]["parent"] == found["A_end"]["parent"] == "A"


def test_every_end_in_a_block_is_one_end():
    model = mermaid.parse("stateDiagram-v2\n  A --> [*]\n  B --> [*]\n")
    assert [(start, end) for start, end, _label in model.edges] == [
        ("A", "root_end"), ("B", "root_end")]


def test_a_second_description_turns_the_first_into_a_title():
    # mermaid's dataFetcher: the id is dropped, not shown as the title.
    busy = nodes("stateDiagram-v2\n  Busy : working hard\n  Busy : still\n")["Busy"]
    assert (busy["shape"], busy["label"], busy["description"]) == (
        "rectWithTitle", "working hard", ["still"])


def test_a_transition_keeps_its_label_without_the_colon():
    model = mermaid.parse("stateDiagram-v2\n  A --> B : go now\n")
    assert model.edges == [("A", "B", "go now")]


def test_a_comment_glued_to_an_id_is_still_a_comment():
    # The state lexer's processId hands a `%%` back to be lexed apart.
    assert set(nodes("stateDiagram-v2\n  A --> B%%gone\n")) == {"A", "B"}


def test_keywords_are_case_insensitive_as_in_mermaid():
    assert nodes("stateDiagram-v2\n  STATE c <<CHOICE>>\n  A --> c\n")["c"]["shape"] == "choice"


@pytest.mark.parametrize("source", [
    "stateDiagram-v2\n  state Two words {\n    a --> b\n  }\n",     # thrown by its lexer
    "stateDiagram-v2\n  classDef default fill:#f00\n  A --> B\n",   # no grammar rule takes it
    "stateDiagram-v2\n  A --> B\n  click A \"https://example.org\"\n",
    "stateDiagram-v2\n  state C {\n    a\n  }\n  C : one\n  C : two\n",
])
def test_what_mermaid_refuses_is_worded_as_mermaids(source):
    with pytest.raises(mermaid.Invalid):
        mermaid.parse(source)


@pytest.mark.parametrize("source", [
    "stateDiagram-v2\n  A --> B\n  note right of A : a note\n",
    "stateDiagram-v2\n  state A {\n    a --> b\n    --\n    c --> d\n  }\n",
    "stateDiagram-v2\n  direction LR\n  state A {\n    a --> b\n  }\n",
    "stateDiagram-v2\n  A --> B : **bold**\n",
    "stateDiagram-v2\n  state A {\n    a\n  }\n  A --> A\n",
])
def test_what_lectern_does_not_draw_falls_back(source):
    with pytest.raises(mermaid.Unsupported) as raised:
        mermaid.parse(source)
    assert not isinstance(raised.value, mermaid.Invalid)


def test_a_composite_state_given_the_diagrams_direction_is_drawn():
    mermaid.parse("stateDiagram-v2\n  direction LR\n  state A {\n    direction LR\n"
                  "    a --> b\n  }\n")


# -- drawing ---------------------------------------------------------------

def test_pseudo_states_draw_no_label():
    scene = build("stateDiagram-v2\n  state c <<choice>>\n  state f <<fork>>\n"
                  "  [*] --> A\n  A --> c\n  c --> f\n  f --> [*]\n")
    assert texts(scene) == ["A"]


def test_start_is_a_dot_and_end_a_bullseye():
    scene = build("stateDiagram-v2\n  [*] --> A\n  A --> [*]\n")
    dots = [shape for shape in scene.shapes if isinstance(shape, sc.Ellipse)]
    filled = [dot for dot in dots if dot.fill == sc.EDGE]
    # The start's dot, and the end's: a ring round a dot.
    assert len(dots) == 3 and len(filled) == 2


@pytest.mark.parametrize("direction, wide", [("TB", True), ("LR", False)])
def test_a_fork_lies_across_the_way_its_transitions_run(direction, wide):
    scene = build(f"stateDiagram-v2\n  direction {direction}\n  state f <<fork>>\n"
                  "  A --> f\n  f --> B\n  f --> C\n")
    bar, = [shape for shape in boxes(scene) if isinstance(shape, sc.Rect) and shape.fill == sc.EDGE]
    assert (bar.w > bar.h) == wide


def test_a_titled_state_draws_a_rule_between_title_and_description():
    scene = build("stateDiagram-v2\n  Busy : working hard\n  Busy : still\n")
    title, body = [t for t in scene.shapes if isinstance(t, sc.Text)]
    rule, = lines(scene)
    assert (title.text, body.text) == ("working hard", "still")
    assert title.y + title.h <= rule.points[0][1] <= body.y


def test_a_composite_state_is_a_solid_frame_round_its_own_states():
    scene = build("stateDiagram-v2\n  [*] --> Outer\n  state Outer {\n    [*] --> inner\n"
                  "    inner --> [*]\n  }\n  Outer --> Done\n")
    frame, = frames(scene)
    assert not frame.dashed
    title = next(t for t in scene.shapes if isinstance(t, sc.Text) and t.text == "Outer")
    inner = next(t for t in scene.shapes if isinstance(t, sc.Text) and t.text == "inner")
    done = next(t for t in scene.shapes if isinstance(t, sc.Text) and t.text == "Done")
    assert frame.y <= title.y and frame.y + frame.h >= inner.y + inner.h
    assert done.y > frame.y + frame.h


def test_a_transition_to_a_composite_state_ends_at_its_frame():
    model = mermaid.parse("stateDiagram-v2\n  A --> Outer\n  state Outer {\n    a --> b\n  }\n")
    edge, = [e for e in model.chart().edges if e.src == "A"]
    assert edge.dst_cluster == "Outer"


def test_transitions_are_right_angled_unless_the_flowchart_curve_says_otherwise():
    # mermaid draws a state diagram's edges with the flowchart's curve.
    assert mermaid.parse("stateDiagram-v2\n  A --> B\n").curve == layered.ORTHOGONAL
    curved = mermaid.parse("%%{init: {'flowchart': {'curve': 'basis'}}}%%\n"
                           "stateDiagram-v2\n  A --> B\n")
    assert curved.curve == "basis"


def test_a_click_links_its_state():
    scene = build('stateDiagram-v2\n  A --> B\n  click A "https://example.org" "tip"\n')
    link, = [shape for shape in scene.shapes if isinstance(shape, sc.Link)]
    assert link.href == "https://example.org"


def test_class_and_style_paint_a_state():
    scene = build("stateDiagram-v2\n  classDef bad fill:#ff0000\n  A --> B\n  class B bad\n")
    filled = [shape for shape in boxes(scene) if isinstance(shape.fill, tuple)]
    assert len(filled) == 1 and filled[0].fill[:3] == (1.0, 0.0, 0.0)


def test_a_state_diagram_fence_becomes_a_diagram_widget():
    renderer, _buffer = render("```mermaid\nstateDiagram-v2\n  [*] --> A\n  A --> [*]\n```\n")
    assert len(renderer.diagrams) == 1
    assert [item.kind for item in renderer.print_model] == ["diagram"]

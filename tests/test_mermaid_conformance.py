"""Lectern's mermaid parsers checked against mermaid's own, case by case.

The corpus is in mermaid_corpus/ (see its README): diagram sources from
mermaid's parser specs and from Lectern's own regressions, with what
mermaid's parser made of each recorded at dev time, so this needs no Node.

A case passes when Lectern reads it the way mermaid does, or refuses it
(Unsupported) -- drawing a subset is the design, and refusing falls back
to the source. What may not happen is drawing a diagram mermaid wouldn't,
or reading one differently. The ones that do today are listed, with the
reason, in deviations.json, and are expected to fail; fixing one makes
its test fail too, which is the signal to take it off the list.
"""
import pytest

from mermaid_corpus import conformance

KNOWN = conformance.deviations()


def corpus():
    for family in conformance.COMPARE:
        results = conformance.recorded(family)
        for case in conformance.cases(family):
            marks = [pytest.mark.xfail(strict=True, reason=KNOWN[case["id"]])] \
                if case["id"] in KNOWN else []
            yield pytest.param(family, case, results.get(case["id"]), id=case["id"], marks=marks)


@pytest.mark.parametrize("family, case, result", corpus())
def test_lectern_reads_the_corpus_the_way_mermaid_does(family, case, result):
    assert result is not None, "no recording: run mermaid_corpus/record/record.mjs"
    kind, theirs, ours = conformance.outcome(family, case, result)
    assert kind in ("agree", "unsupported"), (
        f"{kind}: mermaid {'refused it' if theirs is None else theirs}, Lectern read {ours}")


@pytest.mark.parametrize("family", conformance.FAMILIES)
def test_every_case_is_recorded_and_every_recording_has_a_case(family):
    ids = [case["id"] for case in conformance.cases(family)]
    assert len(ids) == len(set(ids))
    assert set(conformance.recorded(family)) == set(ids)


def test_every_listed_deviation_is_a_case():
    ids = {case["id"] for family in conformance.FAMILIES for case in conformance.cases(family)}
    assert set(KNOWN) <= ids

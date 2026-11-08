"""Fixture adapters must not rewrite the reference failures under test."""

import pytest

from hindsight_api.engine.consolidation.consolidator import _CreateAction, _UpdateAction
from tests.consolidation_answers import fact_answer, post_conversion_answer, wire_fact_answer


def actions(answer):
    return answer.to_actions({}, {}, lambda *_: False, lambda *_: False)


def test_fact_answer_round_trips_shared_create():
    creates = [_CreateAction(text="Shared", source_fact_ids=["A", "B"])]
    assert [(c.text, c.source_fact_ids) for c in actions(fact_answer(creates=creates)).creates] == [
        ("Shared", ["A", "B"])
    ]


@pytest.mark.parametrize(
    "creates",
    [
        [
            _CreateAction(text="Valid", source_fact_ids=["A"]),
            _CreateAction(text="Invalid", source_fact_ids=["A", "outsider"]),
        ],
        [
            _CreateAction(text="Shared", source_fact_ids=["A", "B"]),
            _CreateAction(text="Different", source_fact_ids=["B"]),
        ],
        [
            _CreateAction(text="First", source_fact_ids=["A", "B"]),
            _CreateAction(text="Second", source_fact_ids=["A", "C"]),
        ],
    ],
)
def test_non_lossless_verdict_fixture_is_rejected(creates):
    with pytest.raises(ValueError, match="not losslessly representable"):
        fact_answer(creates=creates)
    with pytest.raises(ValueError, match="not losslessly representable"):
        wire_fact_answer({"creates": [action.model_dump() for action in creates]})
    answer = post_conversion_answer(creates=creates)
    converted = actions(answer)
    assert converted.creates == creates
    # Every conversion gets fresh actions; filtering/apply may mutate its copy.
    converted.creates[0].source_fact_ids.append("mutation")
    assert actions(answer).creates == creates


def test_post_conversion_answer_preserves_duplicate_update_proposals_and_citations():
    updates = [
        _UpdateAction(observation_id="O", text="Valid", source_fact_ids=["A"]),
        _UpdateAction(observation_id="O", text="Invalid", source_fact_ids=["B", "outsider"]),
    ]
    with pytest.raises(ValueError, match="not losslessly representable"):
        fact_answer(updates=updates)
    answer = post_conversion_answer(updates=updates)
    converted = actions(answer)
    assert converted.updates == updates
    converted.updates[0].source_fact_ids.append("mutation")
    assert actions(answer).updates == updates


def test_post_conversion_short_alias_resolution_keeps_outsiders_and_siblings():
    answer = post_conversion_answer(
        creates=[_CreateAction(text="Invalid", source_fact_ids=["F1", "outsider"])],
        updates=[_UpdateAction(observation_id="O1", text="Valid", source_fact_ids=["F2"])],
    )
    converted = actions(answer.with_real_ids({"F1": "real-A", "F2": "real-B", "O1": "real-O"}))
    assert converted.creates[0].source_fact_ids == ["real-A", "outsider"]
    assert converted.updates[0].observation_id == "real-O"
    assert converted.updates[0].source_fact_ids == ["real-B"]

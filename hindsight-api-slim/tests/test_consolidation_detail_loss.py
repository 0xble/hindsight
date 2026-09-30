"""Recorded outputs test a deterministic gate, not a model's ability to repair.

Fixtures quote the 2026-09-30 offline audit's before/after and source facts. No
provider or live database is contacted by these tests. The OpenAI SDK stub still
exercises real parsing and the single-completion attempt boundary.
"""

import json
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

import pytest

from hindsight_api.engine.consolidation import consolidator as c
from hindsight_api.engine.consolidation.detail_loss import (
    Anchor,
    Evidence,
    dropped_merge_anchors,
    dropped_supported_anchors,
    without_temporal_suffix,
)
from hindsight_api.engine.response_models import MemoryFact
from tests.test_consolidation_schema_correction import config, install, provider  # noqa: F401

CASES = json.loads((Path(__file__).parent / "fixtures/consolidation_detail_loss_audit.json").read_text())
REGRESSIONS = [case for case in CASES if case["classification"] == "REGRESSION"]
LEGITIMATE = [case for case in CASES if case["classification"] == "OK"]
OBS_ID = "33333333-3333-4333-8333-333333333333"


def inputs(case):
    old = [MemoryFact(**fact, fact_type="world") for fact in case["prior_source_facts"]]
    new = case["new_source_facts"]
    observation = SimpleNamespace(
        id=OBS_ID,
        text=case["before"],
        proof_count=1,
        occurred_start=None,
        occurred_end=None,
        mentioned_at=None,
        tags=[],
        source_fact_ids=[fact.id for fact in old],
    )
    return SimpleNamespace(observation=observation, old={fact.id: fact for fact in old}, new=new)


def response(case, text=None):
    return {
        "updates": [
            {
                "text": case["after"] if text is None else text,
                "observation_id": OBS_ID,
                "source_fact_ids": [fact["id"] for fact in case["new_source_facts"]],
            }
        ]
    }


async def run_case(provider, config, case, **kwargs):
    data = inputs(case)
    return await c._consolidate_batch_with_llm(provider, data.new, [data.observation], data.old, config, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", REGRESSIONS, ids=lambda case: str(case["sample_index"]))
async def test_recorded_detail_loss_preserves_original_and_creates_separately(provider, config, case, caplog):
    stub = install(provider, [response(case), response(case), response(case, "third must never be requested")])
    budget = c._SchemaCorrectionBudget()
    result = await run_case(provider, config, case, schema_correction_budget=budget)
    assert not result.failed
    assert not result.updates
    assert result.creates[0].text == case["after"]
    assert result.creates[0]._preserve_separate
    assert len(stub.requests) == 2
    assert budget.result_stats()["detail_loss_flagged"] == 1
    assert budget.result_stats()["detail_loss_fallback"] == 1
    assert "detail_loss outcome=flagged" in caplog.text
    assert inputs(case).observation.text == case["before"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", LEGITIMATE, ids=lambda case: str(case["sample_index"]))
async def test_recorded_legitimate_replacements_pass_unchanged(provider, config, case):
    stub = install(provider, [response(case)])
    result = await run_case(provider, config, case)
    assert not result.failed and not result.creates
    assert result.updates[0].text == case["after"]
    assert len(stub.requests) == 1


@pytest.mark.asyncio
async def test_corrected_text_only_keeps_original_sources_and_siblings(provider, config):
    case = REGRESSIONS[0]
    first = response(case)
    first["creates"] = [{"text": "A separate additive note", "source_fact_ids": first["updates"][0]["source_fact_ids"]}]
    repaired = response(case, case["before"])
    repaired["deletes"] = [{"observation_id": OBS_ID}]
    stub = install(provider, [first, repaired])
    budget = c._SchemaCorrectionBudget()
    result = await run_case(provider, config, case, schema_correction_budget=budget)
    assert result.updates[0].text == case["before"]
    assert result.updates[0].source_fact_ids == first["updates"][0]["source_fact_ids"]
    assert result.creates[0].text == "A separate additive note"
    assert not result.deletes
    assert budget.result_stats()["detail_loss_corrected"] == 1
    assert len(stub.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("private completion text"), {"updates": [{}]}])
async def test_failed_correction_falls_back_without_fact_failure(provider, config, failure, caplog):
    case = REGRESSIONS[0]
    stub = install(provider, [response(case), failure])
    result = await run_case(provider, config, case)
    assert not result.failed and not result.updates and result.creates
    assert len(stub.requests) == 2
    assert "private completion text" not in caplog.text


@pytest.mark.asyncio
async def test_budget_or_context_exhaustion_still_preserves_information(provider, config, monkeypatch):
    case = REGRESSIONS[0]
    stub = install(provider, [response(case)])
    result = await run_case(provider, config, case, schema_correction_budget=c._SchemaCorrectionBudget(0))
    assert not result.failed and result.creates and len(stub.requests) == 1
    stub = install(provider, [response(case)])
    monkeypatch.setattr(c, "count_tokens", lambda text: 100 if "Dropped anchors" in text else 1)
    config.consolidation_max_context_tokens = 10
    result = await run_case(provider, config, case)
    assert not result.failed and result.creates and len(stub.requests) == 1


@pytest.mark.asyncio
async def test_schema_repair_does_not_get_third_completion_for_detail_loss(provider, config):
    case = REGRESSIONS[0]
    stub = install(provider, [{"updates": [{}]}, response(case), response(case, case["before"])])
    result = await run_case(provider, config, case)
    assert not result.failed and not result.updates and result.creates
    assert len(stub.requests) == 2


@pytest.mark.asyncio
async def test_fallback_suppresses_delete_of_preserved_target_and_survives_zero_capacity(provider, config):
    case = REGRESSIONS[0]
    reply = response(case)
    reply["deletes"] = [{"observation_id": OBS_ID}]
    stub = install(provider, [reply, reply])
    result = await run_case(provider, config, case, remaining_observation_slots=0, max_observations_per_scope=1)
    assert not result.failed and not result.deletes and result.creates
    assert len(stub.requests) == 2


def test_missing_evidence_is_not_invented_support():
    assert not dropped_supported_anchors("Due 2026-10-02", "Task remains open", [], [])


@pytest.mark.parametrize("newer", [None, "2026-09-30T19:19:42Z", "2026-09-30T19:46:09Z"])
def test_older_advertised_price_cannot_supersede_supported_purchase(newer):
    # Minimal factual reconstruction from semantic-root-cause-20260930.json;
    # unlike the audit fixtures these are not asserted to be verbatim stored text.
    before = "Brian bought the Nuropod for $900."
    old = [Evidence(before, "2026-09-30T19:46:09Z")]
    new = [Evidence("Assistant stated the advertised Nuropod price is $855.", newer)]
    assert any(
        a.value == "$900" for a in dropped_supported_anchors(before, "Brian bought the Nuropod for $855.", old, new)
    )


def test_explicit_later_same_price_slot_can_replace():
    old = [Evidence("Brian paid a Nuropod purchase price of $900.", "2026-09-30T19:46:09Z")]
    new = [Evidence("Brian corrected the Nuropod purchase price to $855.", "2026-10-01T10:00:00Z")]
    assert not dropped_supported_anchors(old[0].text, new[0].text, old, new)


def test_unsupported_additions_remain_an_explicit_non_goal():
    for case in CASES:
        if case["classification"] == "UNSUPPORTED_ADDITION":
            data = inputs(case)
            assert not dropped_supported_anchors(
                case["before"],
                case["after"],
                [Evidence(f.text, f.mentioned_at) for f in data.old.values()],
                [Evidence(f["text"], f["mentioned_at"]) for f in data.new],
            )


def test_rephrasing_literals_and_unicode_dates_retains_anchors():
    old = 'As of August 15, 2026, use `update_goal` with status "complete", only after checks.'
    new = "As of 2026‑08‑15, use update_goal with status complete, only after checks."
    assert not dropped_supported_anchors(old, new, [Evidence(old)], [])
    assert (
        without_temporal_suffix(
            "A fact. (occurred_start=2026-08-15 00:00:00+00:00, mentioned_at=2026-08-16 00:00:00+00:00)"
        )
        == "A fact."
    )


@pytest.mark.parametrize("guard", ["update", "merge"])
@pytest.mark.parametrize(
    "kind,first,second,value",
    [
        ("number", "The timeout is 5 seconds", "the retry limit is 5 attempts", "5"),
        ("date", "Task alpha is due 2026-10-02", "task beta is due 2026-10-02", "2026-10-02"),
        ("money", "The deposit is $500", "the fee is $500", "$500"),
        ("version", "Alpha runs 1.2.3", "beta runs 1.2.3", "1.2.3"),
        ("identifier", "Alpha uses abc1234", "beta uses abc1234", "abc1234"),
        ("literal", 'Alpha has status "ready"', 'beta has status "ready"', "ready"),
        ("marker", "Only alpha can run", "only beta can stop", "only"),
    ],
)
def test_repeated_anchor_loss_is_rejected(guard, kind, first, second, value):
    before, after = first + " and " + second + ".", first + "."
    dropped = (
        dropped_merge_anchors(before, after)
        if guard == "merge"
        else dropped_supported_anchors(before, after, [Evidence(before)], [])
    )
    assert Anchor(kind, value) in dropped


@pytest.mark.parametrize("keep_retry", [True, False])
def test_newer_same_slot_replacement_exempts_only_one_occurrence(keep_retry):
    before = "The server timeout is 5 seconds and the retry limit is 5 attempts."
    after = "The server timeout is 10 seconds" + (" and the retry limit is 5 attempts." if keep_retry else ".")
    old = [Evidence(before, "2026-09-29T10:00:00Z")]
    new = [Evidence("The server timeout is 10 seconds.", "2026-09-30T10:00:00Z")]
    dropped = dropped_supported_anchors(before, after, old, new)
    assert (Anchor("number", "5") in dropped) is not keep_retry


@pytest.mark.parametrize("dense_citation", [False, True])
def test_anchor_dense_update_finishes_under_one_second(dense_citation):
    before = " ".join(f"Metric value {i}." for i in range(1000, 4000)).ljust(60000)
    assert len(before) == 60000
    start = perf_counter()
    dropped = dropped_supported_anchors(
        before,
        "Metric values remain recorded.",
        [Evidence(before, "2026-09-29T10:00:00Z")],
        [Evidence(before if dense_citation else "Metric value 4000.", "2026-09-30T10:00:00Z")],
    )
    elapsed = perf_counter() - start
    assert len([a for a in dropped if a.kind == "number"]) == 3000
    assert elapsed < 1.0, f"anchor-dense update took {elapsed:.3f}s"
    print(f"anchor-dense update: {len(before)} chars, 3000 anchors, dense_citation={dense_citation}, {elapsed:.3f}s")


@pytest.mark.parametrize("guard", ["update", "merge"])
def test_oversized_guard_fails_closed_even_without_lexical_anchors(guard):
    before = "ordinary prose " * 100000
    dropped = (
        dropped_merge_anchors(before, "Short prose.")
        if guard == "merge"
        else dropped_supported_anchors(before, "Short prose.", [Evidence(before)], [])
    )
    assert dropped, "exhausting the work cap must preserve the original rather than authorize the rewrite"


def test_slot_comparison_work_cap_fails_closed_below_input_size_cap():
    before = " ".join(f"Alpha timeout {i}." for i in range(1000, 2000))
    after = " ".join(f"Beta price {i}." for i in range(3000, 4000))
    assert 2 * (len(before) + len(after)) < 262144
    start = perf_counter()
    dropped = dropped_supported_anchors(
        before,
        after,
        [Evidence(before, "2026-09-29T10:00:00Z")],
        [Evidence(after, "2026-09-30T10:00:00Z")],
    )
    assert dropped == [Anchor("budget", "detail-loss analysis limit exceeded")]
    assert perf_counter() - start < 1.0


def test_preprocessing_many_consumption_words_stays_bounded():
    before = "used " * 20000 + "5"
    start = perf_counter()
    assert not dropped_supported_anchors(before, "5", [Evidence(before)], [])
    assert perf_counter() - start < 1.0


def test_excessive_source_count_fails_closed():
    assert dropped_supported_anchors("Plain prose.", "Short prose.", [Evidence("Plain prose.")] * 257, []) == [
        Anchor("budget", "detail-loss analysis limit exceeded")
    ]

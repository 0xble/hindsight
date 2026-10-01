"""Round-five offline regressions through the real consolidation UPDATE boundary.

M01-M16 and L01-L20 preserve exact before/after/cited strings from the
3622d98649d393a8fcded5969932563617b92fe7 clause audit. Audit classification
is retained separately from the conservative lexical contract: ambiguous
ownership, multiple independently anchored predicates, and cross-kind status
changes deliberately preserve the observation and CREATE separately.
No xfails, live provider calls, or database access.
"""

import socket
from dataclasses import dataclass
from itertools import permutations
from types import SimpleNamespace
from typing import Any

import pytest

from hindsight_api.engine.consolidation import consolidator as c
from hindsight_api.engine.consolidation.detail_loss import Evidence, dropped_merge_anchors, dropped_supported_anchors
from hindsight_api.engine.llm_wrapper import LLMProvider
from tests.test_consolidation_detail_loss import response, run_case, transition_case
from tests.test_consolidation_schema_correction import config, install, provider  # noqa: F401


@dataclass(frozen=True)
class AuditCase:
    id: str
    family: str
    before: str
    after: str
    audit_expected: str
    cited: str | None = None


# Durable inline fixtures; scratch artifacts are not needed at test runtime.
AUDIT_CASES = [
    AuditCase(
        "M01-snapshot-comma-list",
        "missed-loss",
        "Current deployment commit: abc1234, server timeout is 5 seconds.",
        "Current deployment commit: def5678.",
        "fallback",
    ),
    AuditCase(
        "M02-snapshot-bullet-lines",
        "missed-loss",
        "Current deployment commit: abc1234\n- Server timeout is 5 seconds.",
        "Current deployment commit: def5678.",
        "fallback",
    ),
    AuditCase(
        "M03-credit-comma-list",
        "missed-loss",
        "A reset credit is available until 2026-10-02, server timeout is 5 seconds.",
        "Brian used the last reset credit.",
        "fallback",
    ),
    AuditCase(
        "M04-credit-bullet-lines",
        "missed-loss",
        "A reset credit is available until 2026-10-02\n- Server timeout is 5 seconds.",
        "Brian used the last reset credit.",
        "fallback",
    ),
    AuditCase(
        "M05-snapshot-repeated-list",
        "missed-loss",
        "Current deployment count: 5, server timeout is 5 seconds.",
        "Current deployment count: 10.",
        "fallback",
    ),
    AuditCase(
        "M06-credit-repeated-bullets",
        "missed-loss",
        "A reset credit is available for 5 days\n- Server timeout is 5 seconds.",
        "Brian used the last reset credit.",
        "fallback",
    ),
    AuditCase(
        "M07-credit-wrong-owner",
        "missed-loss",
        "Alpha reset credit is available until 2026-10-02.",
        "Beta used the last reset credit.",
        "fallback",
    ),
    AuditCase(
        "M08-snapshot-multiple-list-slots",
        "missed-loss",
        "Current deployment commit: abc1234, timeout: 5 seconds, retry limit: 9 attempts.",
        "Current deployment commit: def5678.",
        "fallback",
    ),
    AuditCase(
        "M09-shared-parent-protected",
        "missed-loss",
        "Current deployment commit: abc1234.",
        "Current deployment timeout: 10 seconds.",
        "fallback",
    ),
    AuditCase(
        "M10-snapshot-semicolon-protected",
        "missed-loss",
        "Current deployment count: 5; server timeout is 5 seconds.",
        "Current deployment count: 10.",
        "fallback",
    ),
    AuditCase(
        "M11-credit-semicolon-protected",
        "missed-loss",
        "A reset credit is available for 5 days; server timeout is 5 seconds.",
        "Brian used the last reset credit.",
        "fallback",
    ),
    AuditCase(
        "M12-explicit-comma-protected",
        "missed-loss",
        "Current deployment count: 5, the server timeout is 5 seconds.",
        "Current deployment count: 10.",
        "fallback",
    ),
    AuditCase(
        "M13-snapshot-generic-citation",
        "missed-loss",
        "Current primary deployment count: 5. Current backup deployment count: 5.",
        "Current deployment count: 10.",
        "fallback",
    ),
    AuditCase(
        "M14-credit-wrong-resource",
        "missed-loss",
        "An Alpha reset credit is available until 2026-10-02.",
        "Brian used the last Beta reset credit.",
        "fallback",
    ),
    AuditCase(
        "L01-snapshot-basic",
        "legitimate-update",
        "Current deployment commit: abc1234.",
        "Current deployment commit: def5678.",
        "update",
    ),
    AuditCase(
        "L02-credit-basic",
        "legitimate-update",
        "A reset credit is available until 2026-10-02.",
        "Brian used the last reset credit.",
        "update",
    ),
    AuditCase(
        "L03-snapshot-semicolon-keep-timeout",
        "legitimate-update",
        "Current deployment count: 5; server timeout is 5 seconds.",
        "Current deployment count: 10; server timeout is 5 seconds.",
        "update",
        "Current deployment count: 10.",
    ),
    AuditCase(
        "L04-credit-semicolon-keep-timeout",
        "legitimate-update",
        "A reset credit is available for 5 days; server timeout is 5 seconds.",
        "Brian used the last reset credit; server timeout is 5 seconds.",
        "update",
        "Brian used the last reset credit.",
    ),
    AuditCase(
        "L05-snapshot-range",
        "legitimate-update",
        "Current backlog: between 5 and 10 tasks.",
        "Current backlog: between 15 and 20 tasks.",
        "update",
    ),
    AuditCase(
        "L06-snapshot-repeated-total",
        "legitimate-update",
        "Current backlog: 5 tasks (5 total).",
        "Current backlog: 10 tasks (10 total).",
        "update",
    ),
    AuditCase(
        "L07-bullet-repeated-total",
        "legitimate-update",
        "- Current backlog: 5 tasks (5 total).",
        "- Current backlog: 10 tasks (10 total).",
        "update",
    ),
    AuditCase(
        "L08-credit-distinct-owner-semicolon",
        "legitimate-update",
        "Alpha reset credit is available until 2026-10-02; Beta reset credit is available until 2026-10-09.",
        "Alpha used the last reset credit; Beta reset credit is available until 2026-10-09.",
        "update",
        "Alpha used the last reset credit.",
    ),
    AuditCase(
        "L09-credit-distinct-owner-repeated-date",
        "legitimate-update",
        "Alpha reset credit is available until 2026-10-02. Beta reset credit is available until 2026-10-02.",
        "Alpha used the last reset credit. Beta reset credit is available until 2026-10-02.",
        "update",
        "Alpha used the last reset credit.",
    ),
    AuditCase(
        "L10-two-specific-snapshots",
        "legitimate-update",
        "Current primary deployment count: 5; Current backup deployment count: 5.",
        "Current primary deployment count: 10; Current backup deployment count: 5.",
        "update",
        "Current primary deployment count: 10.",
    ),
    AuditCase(
        "L11-snapshot-list-and",
        "legitimate-update",
        "Current backlog: 5 tasks and 10 blocked tasks.",
        "Current backlog: 15 tasks and 20 blocked tasks.",
        "update",
    ),
    AuditCase(
        "L12-no-status-bullet",
        "legitimate-update",
        "- Current deployment status: no outages.",
        "- Current deployment status: 2 outages.",
        "update",
    ),
    AuditCase(
        "L13-no-status-plain",
        "legitimate-update",
        "Current deployment status: no outages.",
        "Current deployment status: 2 outages.",
        "update",
    ),
    AuditCase(
        "M15-credit-wrong-primary-token",
        "missed-loss",
        "The primary reset token is available for 5 days.",
        "Brian consumed the last backup reset token.",
        "fallback",
    ),
    AuditCase(
        "M16-snapshot-newline-unbulleted",
        "missed-loss",
        "Current deployment count: 5\nServer timeout is 5 seconds.",
        "Current deployment count: 10.",
        "fallback",
    ),
    AuditCase(
        "L15-credit-distinct-primary-token",
        "legitimate-update",
        "The primary reset token is available for 5 days; the backup reset token is available for 9 days.",
        "Brian consumed the last primary reset token; the backup reset token is available for 9 days.",
        "update",
        "Brian consumed the last primary reset token.",
    ),
    AuditCase(
        "L16-numbered-snapshot-no-status",
        "legitimate-update",
        "1. Current deployment status: no outages.",
        "1. Current deployment status: 2 outages.",
        "update",
    ),
    AuditCase(
        "L17-two-bulleted-snapshots",
        "legitimate-update",
        "- Current primary deployment status: no outages; - Current backup deployment status: no outages.",
        "- Current primary deployment status: 2 outages; - Current backup deployment status: no outages.",
        "update",
        "- Current primary deployment status: 2 outages.",
    ),
    AuditCase(
        "L18-compound-header-conjunction",
        "legitimate-update",
        "Current build and deployment status: no outages.",
        "Current build and deployment status: 2 outages.",
        "update",
    ),
    AuditCase(
        "L19-snapshot-bullet-keep-timeout",
        "legitimate-update",
        "Current deployment count: 5\n- Server timeout is 5 seconds.",
        "Current deployment count: 10\n- Server timeout is 5 seconds.",
        "update",
        "Current deployment count: 10.",
    ),
    AuditCase(
        "L20-credit-bullet-keep-timeout",
        "legitimate-update",
        "A reset credit is available for 5 days\n- Server timeout is 5 seconds.",
        "Brian used the last reset credit\n- Server timeout is 5 seconds.",
        "update",
        "Brian used the last reset credit.",
    ),
    AuditCase(
        "L14-credit-new-subject",
        "legitimate-update",
        "A reset credit is available until 2026-10-02.",
        "Brian consumed the remaining reset credit.",
        "update",
    ),
]

# Complete intentional fail-closed subset of the audit's legitimate list.
# A consumer named Alpha does not prove identity with a resource named Alpha;
# a header cannot waive independent predicates; no -> 2 is cross-kind.
DELIBERATE_FAIL_CLOSED = {
    "L08-credit-distinct-owner-semicolon": "consumer/resource ownership ambiguity",
    "L09-credit-distinct-owner-repeated-date": "consumer/resource ownership ambiguity",
    "L11-snapshot-list-and": "multiple independently anchor-bearing predicates",
    "L12-no-status-bullet": "marker-to-number replacement lacks same-kind authority",
    "L13-no-status-plain": "marker-to-number replacement lacks same-kind authority",
    "L16-numbered-snapshot-no-status": "marker-to-number replacement lacks same-kind authority",
    "L17-two-bulleted-snapshots": "marker-to-number replacement lacks same-kind authority",
    "L18-compound-header-conjunction": "marker-to-number replacement lacks same-kind authority",
}


def forbidden_network(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("Round-five tests must not contact a network or database")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden_network)


def expected_action(case: AuditCase) -> str:
    return "fallback" if case.id in DELIBERATE_FAIL_CLOSED else case.audit_expected


async def assert_batch_action(
    provider: LLMProvider,
    config: SimpleNamespace,
    before: str,
    after: str,
    expected: str,
    cited: str | None = None,
    correction: bool = False,
) -> None:
    case = transition_case(before, after)
    if cited is not None:
        case["new_source_facts"][0]["text"] = cited
    stub = install(provider, [response(case), response(case), response(case, "third must never be requested")])
    budget = c._SchemaCorrectionBudget() if correction else c._SchemaCorrectionBudget(0)
    result = await run_case(provider, config, case, schema_correction_budget=budget)
    assert not result.failed
    assert not result.deletes
    assert len(stub.requests) == (2 if correction and expected == "fallback" else 1)
    if expected == "fallback":
        assert not result.updates, "supported original details must not be overwritten"
        assert len(result.creates) == 1
        action = result.creates[0]
        assert action._preserve_separate
        assert budget.result_stats()["detail_loss_flagged"] == 1
        assert budget.result_stats()["detail_loss_fallback"] == 1
    else:
        assert not result.creates
        assert len(result.updates) == 1
        action = result.updates[0]
        assert budget.result_stats().get("detail_loss_flagged", 0) == 0
    assert action.text == after
    assert action.source_fact_ids == [fact["id"] for fact in case["new_source_facts"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", AUDIT_CASES, ids=lambda case: case.id)
async def test_exact_clause_audit_contract(provider: LLMProvider, config: SimpleNamespace, case: AuditCase) -> None:
    await assert_batch_action(provider, config, case.before, case.after, expected_action(case), case.cited)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [case for case in AUDIT_CASES if case.id[:3] in {"M01", "M07", "L07", "L08"}],
    ids=lambda case: case.id,
)
async def test_normal_correction_budget_cannot_hide_clause_audit_loss(
    provider: LLMProvider, config: SimpleNamespace, case: AuditCase
) -> None:
    await assert_batch_action(
        provider, config, case.before, case.after, expected_action(case), case.cited, correction=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["\n", "\n- ", ", ", ". ", "; ", " — ", " – "])
@pytest.mark.parametrize("transition", ["snapshot", "credit"])
async def test_statement_boundaries_keep_repeated_unrelated_timeout(
    provider: LLMProvider, config: SimpleNamespace, separator: str, transition: str
) -> None:
    state = "Current deployment count: 5" if transition == "snapshot" else "A reset credit is available for 5 days"
    after = "Current deployment count: 10." if transition == "snapshot" else "Brian used the last reset credit."
    await assert_batch_action(provider, config, state + separator + "server timeout is 5 seconds.", after, "fallback")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before,after",
    [
        ("Current backlog: 5 tasks with server timeout set to 9 seconds.", "Current backlog: 10 tasks."),
        (
            "A reset credit is available for 5 days with server timeout set to 9 seconds.",
            "Brian used the last reset credit.",
        ),
    ],
    ids=["snapshot-extra-predicate", "credit-extra-predicate"],
)
async def test_old_state_with_extra_anchor_bearing_predicate_fails_closed(
    provider: LLMProvider, config: SimpleNamespace, before: str, after: str
) -> None:
    await assert_batch_action(provider, config, before, after, "fallback")


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "- ", "* ", "• ", "1. ", "1) "])
@pytest.mark.parametrize("header", ["Current deployment count", "Current build and deployment count"])
async def test_snapshot_header_normalizes_list_prefix_but_keeps_header_conjunction(
    provider: LLMProvider, config: SimpleNamespace, prefix: str, header: str
) -> None:
    # Repeated values prevent the ordinary one-occurrence replacement path
    # from masking a failure to recognize the whole snapshot header.
    await assert_batch_action(
        provider,
        config,
        f"{prefix}{header}: 5 tasks (5 total).",
        f"{prefix}{header}: 10 tasks (10 total).",
        "update",
    )


@pytest.mark.asyncio
async def test_correct_citation_in_wrong_output_snapshot_slot_fails_closed(
    provider: LLMProvider, config: SimpleNamespace
) -> None:
    await assert_batch_action(
        provider,
        config,
        "Current deployment count: 5.",
        "Current deployment timeout: 10 seconds.",
        "fallback",
        "Current deployment count: 10.",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before,after,cited",
    [
        (
            "An Alpha reset credit is available until 2026-10-02.",
            "Brian used the last Alpha reset credit.",
            "Brian used the last Alpha reset credit.",
        ),
        (
            "Alpha reset credit is available until 2026-10-02; Beta reset credit is available until 2026-10-09.",
            "Brian used the last Alpha reset credit; Beta reset credit is available until 2026-10-09.",
            "Brian used the last Alpha reset credit.",
        ),
        (
            "Alpha reset credit is available until 2026-10-02. Beta reset credit is available until 2026-10-02.",
            "Brian used the last Alpha reset credit. Beta reset credit is available until 2026-10-02.",
            "Brian used the last Alpha reset credit.",
        ),
    ],
    ids=["one-exact-qualified-resource", "distinct-qualified-resources", "qualified-resources-same-deadline"],
)
async def test_full_qualified_credit_identity_allows_only_named_consumption(
    provider: LLMProvider, config: SimpleNamespace, before: str, after: str, cited: str
) -> None:
    await assert_batch_action(provider, config, before, after, "update", cited)


@pytest.mark.asyncio
async def test_duplicate_full_credit_identity_is_not_unique_authority(
    provider: LLMProvider, config: SimpleNamespace
) -> None:
    await assert_batch_action(
        provider,
        config,
        "An Alpha reset credit is available for 5 days; an Alpha reset credit is available for 9 days.",
        "Brian used the last Alpha reset credit.",
        "fallback",
    )


ADDITIVE_CITATION = "Brian added an unrelated workshop note."
AMOUNT_LOSSES = [
    ("$500 thousand", "$500"),
    ("$500 thousand", "$500 million"),
    ("500k", "500"),
    ("500K", "500"),
    ("500 thousand", "500"),
    ("US$500", "C$500"),
    ("$500USD", "$500CAD"),
    ("$500K", "$500"),
    ("$1.5M", "$1.5B"),
]
EQUIVALENT_AMOUNTS = (
    tuple(permutations(("$500k", "$500K", "$500 thousand", "$500,000"), 2))
    + tuple(permutations(("$1.5M", "$1,500,000"), 2))
    + tuple(permutations(("US$500", "$500 USD", "USD 500"), 2))
    + tuple(permutations(("500k", "500K", "500 thousand", "500,000"), 2))
    + tuple(permutations(("1.5M", "1,500,000"), 2))
)


@pytest.mark.asyncio
@pytest.mark.parametrize("old,new", AMOUNT_LOSSES)
async def test_magnitude_and_currency_loss_preserves_separate_at_update_boundary(
    provider: LLMProvider, config: SimpleNamespace, old: str, new: str
) -> None:
    await assert_batch_action(
        provider,
        config,
        f"The purchase amount is {old}.",
        f"The purchase amount is {new}.",
        "fallback",
        ADDITIVE_CITATION,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("old,new", EQUIVALENT_AMOUNTS)
async def test_equivalent_amounts_pass_without_temporal_replacement_authority(
    provider: LLMProvider, config: SimpleNamespace, old: str, new: str
) -> None:
    await assert_batch_action(
        provider,
        config,
        f"The purchase amount is {old}.",
        f"The purchase amount is {new}.",
        "update",
        ADDITIVE_CITATION,
    )


@pytest.mark.parametrize("guard", ["update", "merge"])
@pytest.mark.parametrize("old,new", AMOUNT_LOSSES)
def test_both_guards_reject_magnitude_or_currency_loss(guard: str, old: str, new: str) -> None:
    before, after = f"The purchase amount is {old}.", f"The purchase amount is {new}."
    dropped = (
        dropped_merge_anchors(before, after)
        if guard == "merge"
        else dropped_supported_anchors(before, after, [Evidence(before)], [Evidence(ADDITIVE_CITATION)])
    )
    assert dropped, f"{guard} must retain the complete magnitude and explicit currency of {old!r}"


@pytest.mark.parametrize("guard", ["update", "merge"])
@pytest.mark.parametrize("old,new", EQUIVALENT_AMOUNTS)
def test_both_guards_accept_equivalent_compound_amounts(guard: str, old: str, new: str) -> None:
    before, after = f"The purchase amount is {old}.", f"The purchase amount is {new}."
    dropped = (
        dropped_merge_anchors(before, after)
        if guard == "merge"
        else dropped_supported_anchors(before, after, [Evidence(before)], [Evidence(ADDITIVE_CITATION)])
    )
    assert not dropped, f"{guard} must normalize equivalent amounts {old!r} and {new!r}: {dropped}"


DISTINCT_SLOT_CASES = [
    (
        "Current primary deployment count: 5.",
        "Current backup deployment count: 9.",
        "Current primary deployment count: 10.",
    ),
    (
        "The primary server timeout is 5 seconds.",
        "The backup server timeout is 9 seconds.",
        "The primary server timeout is 10 seconds.",
    ),
    (
        "The primary purchase amount is $500.",
        "The backup purchase amount is $900.",
        "The primary purchase amount is $600.",
    ),
    (
        "The primary deployment commit is abc1234.",
        "The backup deployment commit is fed9876.",
        "The primary deployment commit is def5678.",
    ),
    ("slot0: 1000.", "slot1: 1001.", "slot0: 1002."),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second,replacement", DISTINCT_SLOT_CASES)
@pytest.mark.parametrize("keep_second", [False, True])
async def test_distinct_value_slot_authority_is_not_reused(provider, config, first, second, replacement, keep_second):
    await assert_batch_action(
        provider,
        config,
        first + " " + second,
        replacement + (" " + second if keep_second else ""),
        "update" if keep_second else "fallback",
        replacement,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second,replacement", DISTINCT_SLOT_CASES)
async def test_distinct_value_single_slot_replacement_control(provider, config, first, second, replacement):
    await assert_batch_action(provider, config, first, replacement, "update", replacement)


@pytest.mark.asyncio
@pytest.mark.parametrize("cited", ["Current deployment count: 5.", "Current deployment count: 11."])
async def test_snapshot_requires_changed_same_kind_value_in_citation_and_output(provider, config, cited):
    await assert_batch_action(
        provider, config, "Current deployment count: 5.", "Current deployment count: 10.", "fallback", cited
    )


@pytest.mark.asyncio
async def test_credit_identity_must_be_unique_even_outside_available_slots(provider, config):
    await assert_batch_action(
        provider,
        config,
        "An Alpha reset credit is available for 5 days; an Alpha reset credit was previously cancelled.",
        "Brian used the last Alpha reset credit.",
        "fallback",
    )


@pytest.mark.parametrize(
    "before,after",
    [("$500kUSD", "$500USD"), ("$500 thousand USD", "$500 million USD"), ("Current count: 5,5.", "Current count: 55.")],
)
def test_compound_qualifiers_and_invalid_grouping_do_not_collapse(before, after):
    assert dropped_supported_anchors(before, after, [Evidence(before)], [])
    assert dropped_merge_anchors(before, after)

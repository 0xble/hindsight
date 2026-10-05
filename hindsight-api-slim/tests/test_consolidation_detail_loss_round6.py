"""Round-six exact reviewer cases through the real consolidation UPDATE boundary.

All 60 A cases are copied verbatim from the fa462fc offline reviewer AST.
No scratch artifact is imported or read at test runtime. Only SDK completions
are synthetic; parsing, replacement authority, and preservation are real.
The four semantic-only cases document the retained lexical-only contract.
"""

import copy
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from hindsight_api.engine.consolidation import consolidator as c
from hindsight_api.engine.llm_wrapper import LLMProvider
from tests.test_consolidation_detail_loss import inputs, response, transition_case
from tests.test_consolidation_detail_loss_round5 import assert_batch_action, no_network  # noqa: F401
from tests.test_consolidation_schema_correction import config, install, provider  # noqa: F401

ADDITIVE = "Brian added an unrelated workshop note."


@dataclass(frozen=True)
class Case:
    id: str
    family: str
    before: str
    after: str
    expected: str = "fallback"
    cited: str | None = None
    rationale: str = ""


# Durable exact reviewer input strings and original expectation metadata.
AUDIT_CASES = [
    Case(
        "A01-negative-integer",
        "new-adversarial",
        "The account balance is -500.",
        "The account balance is 500.",
        cited=ADDITIVE,
        rationale="Unsupported loss of negative sign",
    ),
    Case(
        "A02-negative-money",
        "new-adversarial",
        "The account balance is -$500.",
        "The account balance is $500.",
        cited=ADDITIVE,
        rationale="Unsupported loss of negative sign",
    ),
    Case(
        "A03-negative-scale",
        "new-adversarial",
        "The account balance is -1.5k.",
        "The account balance is 1.5k.",
        cited=ADDITIVE,
        rationale="Unsupported loss of negative sign",
    ),
    Case(
        "A04-negative-money-scale",
        "new-adversarial",
        "The account balance is -US$1.5M.",
        "The account balance is US$1.5M.",
        cited=ADDITIVE,
        rationale="Unsupported loss of negative sign",
    ),
    Case(
        "A05-leading-decimal-scale",
        "new-adversarial",
        "The purchase amount is .5k.",
        "The purchase amount is 5k.",
        cited=ADDITIVE,
        rationale="0.5k cannot become 5k without evidence",
    ),
    Case(
        "A06-leading-decimal-number",
        "new-adversarial",
        "The measured mass is .5 grams.",
        "The measured mass is 5 grams.",
        cited=ADDITIVE,
        rationale="0.5 cannot become 5 without evidence",
    ),
    Case(
        "A07-leading-decimal-scaleword",
        "new-adversarial",
        "The purchase amount is .5 million.",
        "The purchase amount is 5 million.",
        cited=ADDITIVE,
        rationale="0.5 million cannot become 5 million",
    ),
    Case(
        "A08-identifier-leading-zero",
        "new-adversarial",
        "The employee identifier is 00123.",
        "The employee identifier is 123.",
        cited=ADDITIVE,
        rationale="Leading zeros in opaque numeric identifier must survive",
    ),
    Case(
        "A09-identifier-hex-case",
        "new-adversarial",
        "The case-sensitive API key is AbC1234.",
        "The case-sensitive API key is abc1234.",
        cited=ADDITIVE,
        rationale="Explicitly case-sensitive opaque identifier altered",
    ),
    Case(
        "A10-label-case-collision",
        "new-adversarial",
        "The case-sensitive pool label is PROD; server timeout is 5 seconds.",
        "The case-sensitive pool label is prod; server timeout is 5 seconds.",
        cited=ADDITIVE,
        rationale="Explicitly case-sensitive unquoted label altered",
    ),
    Case(
        "A11-label-punctuation-authority",
        "new-adversarial",
        "The pool-A server timeout is 5 seconds.",
        "The pool_A server timeout is 10 seconds.",
        cited="The pool_A server timeout is 10 seconds.",
        rationale="New pool_A value must not supersede distinct pool-A slot",
    ),
    Case(
        "A12-label-order-authority",
        "new-adversarial",
        "The Alice-to-Bob transfer limit is $500.",
        "The Bob-to-Alice transfer limit is $600.",
        cited="The Bob-to-Alice transfer limit is $600.",
        rationale="Opposite directional slot must not erase old limit",
    ),
    Case(
        "A13-twice-label-distinct-values",
        "new-adversarial",
        "Current server timeout: 5 seconds. Current server timeout: 9 seconds.",
        "Current server timeout: 10 seconds.",
        cited="Current server timeout: 10 seconds.",
        rationale="Duplicate exact labels are ambiguous authority",
    ),
    Case(
        "A14-twice-label-punctuation",
        "new-adversarial",
        "Server timeout: 5 seconds. Server timeout : 9 seconds.",
        "Server timeout: 10 seconds.",
        cited="Server timeout: 10 seconds.",
        rationale="Duplicate normalized labels must not resolve by value",
    ),
    Case(
        "A15-nested-list-loss",
        "new-adversarial",
        "Current deployment count: 5\n  - Retry policy:\n    - Server timeout is 9 seconds.",
        "Current deployment count: 10.",
        rationale="Nested independently supported timeout disappears",
    ),
    Case(
        "A16-parenthesis-other-total",
        "new-adversarial",
        "Current backlog: 5 tasks (9 total).",
        "Current backlog: 10 tasks.",
        rationale="Distinct independently supported total is not a repeated value",
    ),
    Case(
        "A17-parenthesis-identical-total",
        "new-adversarial",
        "Current backlog: 5 tasks (5 total).",
        "Current backlog: 10 tasks.",
        rationale="Check repeated-total policy against exact dated state",
    ),
    Case(
        "A18-parenthesis-other-unit",
        "new-adversarial",
        "Current backlog: 5 tasks (9 retries).",
        "Current backlog: 10 tasks.",
        rationale="Parenthesized retries independent of task count",
    ),
    Case(
        "A19-k-word-loss",
        "new-adversarial",
        "The purchase amount is five k.",
        "The purchase amount is five.",
        cited=ADDITIVE,
        rationale="Word-normalized k scale must remain",
    ),
    Case(
        "A20-decimal-k-scale-loss",
        "new-adversarial",
        "The purchase amount is 0.125k.",
        "The purchase amount is 0.125.",
        cited=ADDITIVE,
        rationale="Decimal scale must remain",
    ),
    Case(
        "A21-negative-range",
        "new-adversarial",
        "The permitted temperature range is -5 to -1 degrees.",
        "The permitted temperature range is 5 to 1 degrees.",
        cited=ADDITIVE,
        rationale="Signs of range endpoints lost",
    ),
    Case(
        "A22-range-direction",
        "new-adversarial",
        "The operating limit is less than 5 seconds.",
        "The operating limit is greater than 5 seconds.",
        cited=ADDITIVE,
        rationale="Comparison direction reversed",
    ),
    Case(
        "A23-range-delimiter",
        "new-adversarial",
        "The permitted range is 5-10 tasks.",
        "The permitted range is 5, 10 tasks.",
        cited=ADDITIVE,
        rationale="Range converted to two disjoint points",
    ),
    Case(
        "A24-mixed-currencies",
        "new-adversarial",
        "The invoice totals are US$500 and C$900.",
        "The invoice totals are C$500 and US$900.",
        cited=ADDITIVE,
        rationale="Different explicit currencies must remain bound to amounts",
    ),
    Case(
        "A25-mixed-currencies-repeated-value",
        "new-adversarial",
        "The primary invoice is US$500; the backup invoice is C$500.",
        "The primary invoice is C$500; the backup invoice is US$500.",
        cited=ADDITIVE,
        rationale="Currency values swapped between independent single-value slots",
    ),
    Case(
        "A26-label-set-collision-slots",
        "new-adversarial",
        "The Alice-to-Bob transfer limit is $500. The Bob-to-Alice transfer limit is $900.",
        "The Alice-to-Bob transfer limit is $600.",
        rationale="Opposite directional competitor prevents reuse",
    ),
    Case(
        "A27-nested-inline-parenthesis",
        "new-adversarial",
        "Current deployment count: 5 (server timeout: 9 seconds).",
        "Current deployment count: 10.",
        rationale="Nested colon does not authorize independent timeout loss",
    ),
    Case(
        "A28-numeric-label-collapse",
        "new-adversarial",
        "Server 01 timeout is 5 seconds.",
        "Server 1 timeout is 10 seconds.",
        rationale="Numeric label 01 must not collapse into distinct server 1",
    ),
    Case(
        "A29-numeric-identifier-scale",
        "new-adversarial",
        "The opaque purchase code is 500k.",
        "The opaque purchase code is 500000.",
        cited=ADDITIVE,
        rationale="Numeric opaque code is not an interchangeable quantity",
    ),
    Case(
        "A30-letter-case-punct-label",
        "new-adversarial",
        "The pool-A server timeout: 5 seconds.",
        "The pool A server timeout: 10 seconds.",
        rationale="Colon label mismatch must not borrow unordered generic context",
    ),
    Case(
        "A31-parenthetical-negative-money",
        "new-adversarial",
        "The accounting balance is ($500).",
        "The accounting balance is $500.",
        cited=ADDITIVE,
        rationale="Accounting negative parentheses removed",
    ),
    Case(
        "A32-unicode-minus",
        "new-adversarial",
        "The account balance is −500.",
        "The account balance is 500.",
        cited=ADDITIVE,
        rationale="Unicode minus normalized but not protected",
    ),
    Case(
        "A33-twice-label-independent-same-value",
        "new-adversarial",
        "Server timeout: 5 seconds. Server timeout: 5 seconds.",
        "Server timeout: 10 seconds.",
        rationale="Duplicate exact labels must fail closed",
    ),
    Case(
        "A34-punctuation-literal-loss",
        "new-adversarial",
        "The pool label is `pool-A`; the server timeout is 5 seconds.",
        "The pool label is `pool_A`; the server timeout is 10 seconds.",
        rationale="Quoted label control must protect opaque punctuation",
    ),
    Case(
        "A35-snapshot-foreign-currency-number",
        "new-adversarial",
        "Current purchase amount: US$500.",
        "Current purchase amount: C$600.",
        cited="Current purchase amount: US$600.",
        rationale="USD citation must not authorize CAD output replacement",
    ),
    Case(
        "A36-generic-foreign-currency-number",
        "new-adversarial",
        "The purchase amount is US$500.",
        "The purchase amount is C$600.",
        cited="The purchase amount is US$600.",
        rationale="Changed explicit currency must agree in citation and output",
    ),
    Case(
        "A37-distinct-currency-slot-move",
        "new-adversarial",
        "The primary purchase amount is US$500. The backup purchase amount is C$900.",
        "The primary purchase amount is US$600. The backup purchase amount is US$500.",
        cited="The primary purchase amount is US$600.",
        rationale="Primary old value relocation must not stand in for backup amount",
    ),
    Case(
        "A38-generic-wrong-labeled-output",
        "new-adversarial",
        "The primary server timeout is 5 seconds.",
        "The backup server timeout is 10 seconds.",
        cited="The primary server timeout is 10 seconds.",
        rationale="Correct primary citation must not permit wrong output slot",
    ),
    Case(
        "A39-generic-opposite-direction-output",
        "new-adversarial",
        "The Alice-to-Bob transfer limit is $500.",
        "The Bob-to-Alice transfer limit is $600.",
        cited="The Alice-to-Bob transfer limit is $600.",
        rationale="Citation/output directional identity must agree",
    ),
    Case(
        "A40-current-numeric-identifier",
        "new-adversarial",
        "Current employee identifier: 00123.",
        "Current employee identifier: 123.",
        cited=ADDITIVE,
        rationale="Current label cannot excuse opaque identifier normalization",
    ),
    Case(
        "A41-currency-prefix-boundary",
        "new-adversarial",
        "The purchase amount is HK$500.",
        "The purchase amount is HK$5.",
        cited=ADDITIVE,
        rationale="Unsupported currency code must still protect changed amount",
    ),
    Case(
        "A42-currency-symbol-prefix-loss",
        "new-adversarial",
        "The purchase amount is HK$500.",
        "The purchase amount is SG$500.",
        cited=ADDITIVE,
        rationale="Unknown but explicit currency prefixes not interchangeable",
    ),
    Case(
        "A43-mixed-currency-numeric-code",
        "new-adversarial",
        "The purchase amount is USD500.",
        "The purchase amount is CAD500.",
        cited=ADDITIVE,
        rationale="Currency-affixed amount must preserve currency",
    ),
    Case(
        "A44-k-words-uppercase",
        "new-adversarial",
        "The purchase amount is five K.",
        "The purchase amount is five.",
        cited=ADDITIVE,
        rationale="Uppercase word-normalized scale must remain",
    ),
    Case(
        "A45-decimal-scale-trailing-dot",
        "new-adversarial",
        "The purchase amount is 1.k.",
        "The purchase amount is 1.",
        cited=ADDITIVE,
        rationale="Trailing-dot decimal scale should not disappear",
    ),
    Case(
        "A46-negative-money-suffix",
        "new-adversarial",
        "The accounting balance is -$500 CAD.",
        "The accounting balance is $500 CAD.",
        cited=ADDITIVE,
        rationale="Sign remains independent of explicit currency suffix",
    ),
    Case(
        "A47-exact-colon-wrong-output",
        "new-adversarial",
        "Primary server timeout: 5 seconds.",
        "Backup server timeout: 10 seconds.",
        cited="Primary server timeout: 10 seconds.",
        rationale="Exact citation label must agree with output label",
    ),
    Case(
        "A48-colon-punctuation-wrong-citation",
        "new-adversarial",
        "Pool-A server timeout: 5 seconds.",
        "Pool A server timeout: 10 seconds.",
        cited="Pool A server timeout: 10 seconds.",
        rationale="Distinct explicit colon labels cannot borrow generic overlap",
    ),
    Case(
        "A49-wrong-output-with-preserved-competitor",
        "new-adversarial",
        "Primary server timeout: 5 seconds. Backup retry limit: 9 attempts.",
        "Backup server timeout: 10 seconds. Backup retry limit: 9 attempts.",
        cited="Primary server timeout: 10 seconds.",
        rationale="Preserving other slot does not make mismatched timeout identity safe",
    ),
    Case(
        "A50-signed-ordinary-decimal",
        "new-adversarial",
        "The measured offset is -0.125 millimeters.",
        "The measured offset is 0.125 millimeters.",
        cited=ADDITIVE,
        rationale="Negative decimal loses sign",
    ),
    Case(
        "A51-small-decimal-b-scale",
        "new-adversarial",
        "The purchase amount is .125B.",
        "The purchase amount is 125B.",
        cited=ADDITIVE,
        rationale="Leading decimal point lost at billion scale",
    ),
    Case(
        "A52-short-numeric-identifier",
        "new-adversarial",
        "The account identifier is 000042.",
        "The account identifier is 42.",
        cited=ADDITIVE,
        rationale="Opaque numeric account identifiers must retain leading zeros",
    ),
    Case(
        "A53-long-numeric-identifier-control",
        "new-adversarial",
        "The account identifier is 0000042.",
        "The account identifier is 42.",
        cited=ADDITIVE,
        rationale="Seven-character numeric identifier pattern control",
    ),
    Case(
        "A54-twice-label-case-control",
        "new-adversarial",
        "Current PROD timeout: 5 seconds. Current prod timeout: 9 seconds.",
        "Current PROD timeout: 10 seconds.",
        rationale="Casefolded duplicate labels should fail closed",
    ),
    Case(
        "A55-snapshot-leading-zero-label-control",
        "new-adversarial",
        "Current server 01 timeout: 5 seconds.",
        "Current server 1 timeout: 10 seconds.",
        rationale="Exact snapshot label distinguishes padded numeric names",
    ),
    Case(
        "A56-case-sensitive-quoted-identifier",
        "new-adversarial",
        "The case-sensitive pool identifier is `PROD`.",
        "The case-sensitive pool identifier is `prod`.",
        cited=ADDITIVE,
        rationale="Quoted opaque case-sensitive identifier altered",
    ),
    Case(
        "A57-literal-wrong-output-slot",
        "new-adversarial",
        "The primary deployment commit is `abc1234`.",
        "The backup deployment commit is `def5678`.",
        cited="The primary deployment commit is `def5678`.",
        rationale="Recognized identifier and literal cannot relocate to backup",
    ),
    Case(
        "A58-distinct-total-preserving-count",
        "new-adversarial",
        "Current backlog: 5 tasks (9 total).",
        "Current backlog: 5 tasks (10 total).",
        cited="Current backlog: 5 tasks (10 total).",
        rationale="Snapshot grammar accepts total update even without same-kind changed task count",
    ),
    Case(
        "A59-parenthesis-total-preserved-control",
        "new-adversarial",
        "Current backlog: 5 tasks (9 total).",
        "Current backlog: 10 tasks (9 total).",
        "update",
        "Current backlog: 10 tasks.",
        "Preserving independent total is valid",
    ),
    Case(
        "A60-ordinary-snapshot-count-control",
        "new-adversarial",
        "Current backlog: 5 tasks.",
        "Current backlog: 10 tasks.",
        "update",
        "Current backlog: 10 tasks.",
        "Plain single-number snapshot remains supported",
    ),
]

VALID_CONTROLS = {"A17", "A34", "A58", "A59", "A60"}
SEMANTIC_ONLY = {"A10", "A22", "A23", "A29"}
GROUP_IDS = {
    "F1": {"A11", "A12", "A30", "A38", "A39", "A47", "A48", "A49", "A57"},
    "F2": {"A01", "A02", "A03", "A04", "A21", "A31", "A32", "A46", "A50"},
    "F3": {"A05", "A06", "A07", "A51"},
    "F4": {"A08", "A09", "A40", "A52", "A56"},
    "F5": {"A16", "A17", "A58", "A59"},
    "F6": {"A25", "A42"},
}
GROUPED_IDS = set().union(*GROUP_IDS.values())
PROTECTED_CASES = [case for case in AUDIT_CASES if case.id[:3] not in GROUPED_IDS | VALID_CONTROLS | SEMANTIC_ONLY]


def cases_for(group: str) -> list[Case]:
    return [case for case in AUDIT_CASES if case.id[:3] in GROUP_IDS[group]]


def expected_action(case: Case) -> str:
    # The review's valid controls explicitly support the changes; semantic-only
    # limitations are not promoted to general semantic-entailment guarantees.
    return "update" if case.id[:3] in VALID_CONTROLS | SEMANTIC_ONLY else case.expected


@pytest.mark.asyncio
@pytest.mark.parametrize("case", cases_for("F1"), ids=lambda case: case.id)
async def test_f1_citation_and_output_must_name_the_same_slot(
    provider: LLMProvider, config: SimpleNamespace, case: Case
) -> None:
    await assert_batch_action(provider, config, case.before, case.after, expected_action(case), case.cited)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", cases_for("F2"), ids=lambda case: case.id)
async def test_f2_numeric_signs_must_survive(provider: LLMProvider, config: SimpleNamespace, case: Case) -> None:
    await assert_batch_action(provider, config, case.before, case.after, expected_action(case), case.cited)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", cases_for("F3"), ids=lambda case: case.id)
async def test_f3_leading_decimal_magnitude_must_survive(
    provider: LLMProvider, config: SimpleNamespace, case: Case
) -> None:
    await assert_batch_action(provider, config, case.before, case.after, expected_action(case), case.cited)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", cases_for("F4"), ids=lambda case: case.id)
async def test_f4_opaque_identifier_spelling_must_survive(
    provider: LLMProvider, config: SimpleNamespace, case: Case
) -> None:
    await assert_batch_action(provider, config, case.before, case.after, expected_action(case), case.cited)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", cases_for("F5"), ids=lambda case: case.id)
async def test_f5_independent_parenthetical_total_must_survive(
    provider: LLMProvider, config: SimpleNamespace, case: Case
) -> None:
    await assert_batch_action(provider, config, case.before, case.after, expected_action(case), case.cited)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", cases_for("F6"), ids=lambda case: case.id)
async def test_f6_currency_identity_must_remain_in_its_slot(
    provider: LLMProvider, config: SimpleNamespace, case: Case
) -> None:
    await assert_batch_action(provider, config, case.before, case.after, expected_action(case), case.cited)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", PROTECTED_CASES, ids=lambda case: case.id)
async def test_other_protected_a_cases_remain_preserve_separate(provider, config, case):
    await assert_batch_action(provider, config, case.before, case.after, "fallback", case.cited)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", [case for case in AUDIT_CASES if case.id[:3] in {"A34", "A60"}], ids=lambda case: case.id
)
async def test_valid_literal_and_plain_snapshot_controls(provider, config, case):
    await assert_batch_action(provider, config, case.before, case.after, "update", case.cited)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", [case for case in AUDIT_CASES if case.id[:3] in SEMANTIC_ONLY], ids=lambda case: case.id
)
async def test_semantic_only_lexical_contract(provider, config, case):
    # A23's comma now creates an unlabeled second slot, so explicit-label
    # preservation conservatively vetoes it; no semantic range claim is made.
    expected = "fallback" if case.id.startswith("A23") else "update"
    await assert_batch_action(provider, config, case.before, case.after, expected, case.cited)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before,after",
    [
        ("-500", "−500"),
        ("+500", "500"),
        ("-1.5k", "-1500"),
        ("-US$1.5M", "-USD 1500000"),
        ("-$500", "($500)"),
        ("-$500 CAD", "-CAD 500"),
        (".5", "0.5"),
        ("-.5", "-0.5"),
        (".5k", "500"),
        (".5 million", "500000"),
        (".125B", "125000000"),
        ("-.5k", "-500"),
    ],
)
async def test_equivalent_signed_and_leading_decimal_quantities(provider, config, before, after):
    await assert_batch_action(
        provider,
        config,
        f"The purchase amount is {before}.",
        f"The purchase amount is {after}.",
        "update",
        ADDITIVE,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before,after,expected",
    [
        ("The server timeout is 5 seconds.", "THE SERVER TIMEOUT IS 5 SECONDS.", "update"),
        (
            "The case-sensitive pool identifier is `PROD`.",
            "THE CASE-SENSITIVE POOL IDENTIFIER IS `PROD`.",
            "update",
        ),
        (
            "The case-sensitive pool identifier is `PROD`.",
            "The case-sensitive pool identifier is `prod`.",
            "fallback",
        ),
        (
            "The case-sensitive pool identifier is `pool-A`.",
            "The case-sensitive pool identifier is `pool_A`.",
            "fallback",
        ),
    ],
    ids=["ordinary-prose-case", "prose-case-preserves-literal", "literal-case-loss", "literal-punctuation-loss"],
)
async def test_prose_case_is_not_case_sensitive_literal_identity(provider, config, before, after, expected):
    await assert_batch_action(provider, config, before, after, expected, ADDITIVE)


@pytest.mark.asyncio
@pytest.mark.parametrize("correction", [False, True], ids=["zero-correction", "normal-correction"])
@pytest.mark.parametrize("expected", ["update", "fallback"])
async def test_input_rows_and_action_invariants_are_unchanged(provider, config, expected, correction):
    before = "Current backlog: 5 tasks; server timeout is 9 seconds."
    after = before if expected == "update" else "Current backlog: 10 tasks."
    case = transition_case(before, after)
    data = inputs(case)
    original_case = copy.deepcopy(case)
    original_observation = copy.deepcopy(data.observation)
    original_old = copy.deepcopy(data.old)
    original_new = copy.deepcopy(data.new)
    stub = install(provider, [response(case), response(case), response(case, "third must never be requested")])
    budget = c._SchemaCorrectionBudget() if correction else c._SchemaCorrectionBudget(0)
    result = await c._consolidate_batch_with_llm(
        provider, data.new, [data.observation], data.old, config, schema_correction_budget=budget
    )
    assert case == original_case
    assert data.observation == original_observation
    assert data.old == original_old
    assert data.new == original_new
    assert not result.failed and not result.deletes
    assert len(stub.requests) == (2 if correction and expected == "fallback" else 1)
    if expected == "fallback":
        assert not result.updates and len(result.creates) == 1
        action = result.creates[0]
        assert action._preserve_separate
        assert budget.result_stats()["detail_loss_flagged"] == 1
        assert budget.result_stats()["detail_loss_fallback"] == 1
    else:
        assert not result.creates and len(result.updates) == 1
        action = result.updates[0]
        assert budget.result_stats().get("detail_loss_flagged", 0) == 0
    assert action.text == after
    assert action.source_fact_ids == [fact["id"] for fact in data.new]


def test_exact_case_inventory_is_complete_and_partitioned():
    ids = [case.id[:3] for case in AUDIT_CASES]
    assert len(ids) == len(set(ids)) == 60
    assert set(ids) == {f"A{number:02d}" for number in range(1, 61)}
    protected = {case.id[:3] for case in PROTECTED_CASES}
    assert len(protected) == 21
    assert not protected & (GROUPED_IDS | VALID_CONTROLS | SEMANTIC_ONLY)
    assert protected | GROUPED_IDS | VALID_CONTROLS | SEMANTIC_ONLY == set(ids)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [next(c for c in AUDIT_CASES if c.id.startswith(i)) for i in ("A47", "A01", "A05", "A56", "A16", "A25")],
    ids=lambda c: c.id,
)
async def test_each_finding_cannot_bypass_normal_correction(provider, config, case):
    await assert_batch_action(provider, config, case.before, case.after, "fallback", case.cited, correction=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before,after,expected",
    [
        ("The employee identifier is 01.", "The employee identifier is 1.", "fallback"),
        ("The API key is ApiKey.", "The API key is apikey.", "fallback"),
        ("The count is 5 tasks.", "THE COUNT IS FIVE TASKS.", "update"),
        ("The rule is not authorized.", "THE RULE IS NOT AUTHORIZED.", "update"),
        ("The purchase amount is HKD 500.", "The purchase amount is 500 HKD.", "update"),
        ("The purchase amount is HKD 500.", "The purchase amount is SGD 500.", "fallback"),
        ("The purchase amount is HK$500.", "The purchase amount is HK$500.", "update"),
        ("Pool-A server timeout: 5 seconds.", "pool-a server timeout : 10 seconds.", "update"),
        ("Current backlog: 5 tasks (9 total).", "Current backlog: 9 tasks (5 total).", "fallback"),
        ("The deadline is May 5, 2026.", "THE DEADLINE IS MAY 5, 2026.", "update"),
        ("The purchase amount is HKD500.", "The purchase amount is 500HKD.", "update"),
        ("The purchase amount is HKD500.", "The purchase amount is SGD500.", "fallback"),
        ("The purchase amount is 500KHKD.", "The purchase amount is HKD500000.", "update"),
    ],
)
async def test_token_identity_and_normalized_label_controls(provider, config, before, after, expected):
    cited = after if before.startswith("Pool-A") else ADDITIVE
    await assert_batch_action(provider, config, before, after, expected, cited)


@pytest.mark.parametrize("text", ["5-10", "worker-5", "Pool-A", "abc-123"])
def test_word_identifier_and_range_hyphens_are_not_negative_signs(text):
    from hindsight_api.engine.consolidation.detail_loss import anchors

    assert all(not a.value.startswith("-") for a in anchors(text) if a.kind in {"number", "money"})


@pytest.mark.asyncio
async def test_credit_parser_large_repeated_prefix_is_cpu_bounded_at_batch_boundary(provider, config):
    from time import process_time

    before = "Brian used last " * 16000 + "5"
    case = transition_case(before, "Plain prose.")
    # Keep support compact: duplicating the giant observation as a source would
    # hit input admission before exercising the synchronous consumed-credit scan.
    case["prior_source_facts"][0]["text"] = "5"
    data = inputs(case)
    stub = install(provider, [response(case), response(case)])
    budget = c._SchemaCorrectionBudget(0)
    started = process_time()
    result = await c._consolidate_batch_with_llm(
        provider, data.new, [data.observation], data.old, config, schema_correction_budget=budget
    )
    cpu_seconds = process_time() - started
    print(f"CREDIT_BOUNDARY_CPU_SECONDS={cpu_seconds:.6f}")
    assert not result.failed and not result.updates and not result.deletes
    assert len(result.creates) == 1 and result.creates[0]._preserve_separate
    assert budget.result_stats()["detail_loss_flagged"] == 1
    assert len(stub.requests) == 1
    assert cpu_seconds < 5.0, "valid-size consumed-credit input must not block the async caller with quadratic parsing"


@pytest.mark.parametrize(
    "text",
    [
        "brian used the last reset credit",
        "brian consumed the remaining reset token",
        "brian used last reset voucher",
        "brian used last brian used last reset credit",
        "brian 2 used last reset credit",
        "brian used last 2 used last reset credit",
        "brian used last reset 2 credit",
        "used last reset credit",
        "brian used last credit",
        "brian used last a-credit",
        "brian used remaining remaining reset credit",
        "brian used the last reset credit extra",
        "brian used last reset\ncredit",
    ],
)
def test_linear_credit_scan_preserves_legacy_leftmost_identity(text):
    import re

    from hindsight_api.engine.consolidation.detail_loss import _credit_identity

    legacy = re.fullmatch(
        r".+?\b(?:used|consumed) (?:the )?(?:last|remaining)(?: remaining)? ([a-z][a-z -]*?(?:credit|token|voucher))",
        text,
    )
    assert _credit_identity(text, consumed=True) == (legacy[1].strip() if legacy else "")

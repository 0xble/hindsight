"""Final bounded review fixes through the real consolidation UPDATE boundary.

Only SDK completions are synthetic. Normal correction is available for every
loss and remains capped at two requests before preserve-separate.
"""

import pytest

from tests.test_consolidation_detail_loss_round5 import assert_batch_action, no_network  # noqa: F401
from tests.test_consolidation_schema_correction import config, provider  # noqa: F401

ADDITIVE = "Brian added an unrelated workshop note."


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["500 USD", "1.5 million CAD", "5 AUD", "500"])
async def test_a1_accounting_parentheses_preserve_negative_sign(provider, config, amount):
    await assert_batch_action(
        provider,
        config,
        f"The accounting balance is ({amount}).",
        f"The accounting balance is {amount}.",
        "fallback",
        ADDITIVE,
        correction=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before,after",
    [
        ("The accounting balance is (500 USD).", "The accounting balance is -USD 500."),
        ("The accounting balance is (500).", "The accounting balance is -500."),
        ("(5) Retry count: 9 attempts.", "(5) Retry count: 9 attempts."),
        ("(5) The accounting balance is ($500).", "(5) The accounting balance is -$500."),
        ("The count is (500).", "The count is 500."),
    ],
)
async def test_a1_accounting_equivalence_and_list_numbering(provider, config, before, after):
    await assert_batch_action(provider, config, before, after, "update", ADDITIVE, correction=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before,after",
    [
        ("The case-sensitive API key is PROD.", "The case-sensitive API key is prod."),
        ("The case-sensitive API key is Abcd.", "The case-sensitive API key is abcd."),
        ("The case-sensitive API key is Face.", "The case-sensitive API key is face."),
        ("Use the case-sensitive API key PROD for login.", "Use the case-sensitive API key prod for login."),
        ("The identifier is PROD.", "The identifier is prod."),
        ("The case-sensitive identifier is PROD.", "The case-sensitive identifier is prod."),
        ("The ID is Face.", "The ID is face."),
        ("The token is Abcd.", "The token is abcd."),
        ("The secret name is PROD.", "The secret name is prod."),
        ("The env var is PROD.", "The env var is prod."),
        ("The flag is Face.", "The flag is face."),
        ("The key: PROD.", "The key: prod."),
        ("The token=Face.", "The token=face."),
        ("Use case-sensitive PROD.", "Use case-sensitive prod."),
        ('The value is "PROD".', 'The value is "prod".'),
        ("The value is `Face`.", "The value is `face`."),
        ("The token was Face.", "The token was face."),
        ("The token set to Abcd.", "The token set to abcd."),
        ("Use the key Abcd for login.", "Use the key abcd for login."),
        ("Use case-sensitive: PROD.", "Use case-sensitive: prod."),
        ("Use case-sensitive=Face.", "Use case-sensitive=face."),
        ('The key "PROD" is active.', 'The key "prod" is active.'),
        ("The token `Face` is active.", "The token `face` is active."),
        ("The API key Abcd is active.", "The API key abcd is active."),
        ("The token XYZ123 is active.", "The token xyz123 is active."),
        ("The key is prod_id.", "The key is prod_ID."),
        ("The key is prod-id.", "The key is prod-ID."),
        ("The key is prod.id.", "The key is prod.ID."),
        ("The case-sensitive API key is prod.", "The case-sensitive API key is PROD."),
        ('The key is "prod".', 'The key is "PROD".'),
        ("The key is `prod`.", "The key is `PROD`."),
    ],
)
async def test_a2_explicit_identifier_case_is_exact(provider, config, before, after):
    await assert_batch_action(provider, config, before, after, "fallback", ADDITIVE, correction=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("noun", ["token", "key", "ID", "identifier", "flag", "secret name", "env var"])
async def test_a2_ordinary_predicate_is_not_an_identifier(provider, config, noun):
    await assert_batch_action(
        provider,
        config,
        f"The {noun} expires in 7 days.",
        f"The {noun} will expire in 7 days.",
        "update",
        ADDITIVE,
        correction=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cited", [None, ADDITIVE], ids=["same-text-evidence", "unrelated-additive-evidence"])
@pytest.mark.parametrize(
    "before,after,expected",
    [
        pytest.param(
            "The API key Abcd is active.", "The API key abcd is active.", "fallback", id="identifier-apposition"
        ),
        pytest.param(
            "The token was revoked after 7 days.",
            "The token got revoked after 7 days.",
            "update",
            id="was-got-revoked",
        ),
        pytest.param(
            "The token was revoked after 7 days.",
            "The token has been revoked after 7 days.",
            "update",
            id="was-has-been-revoked",
        ),
    ],
)
async def test_a2_identifier_apposition_and_predicate_restatements(provider, config, before, after, expected, cited):
    await assert_batch_action(
        provider, config, before, after, expected, before if cited is None else cited, correction=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cited", [None, ADDITIVE], ids=["same-text-evidence", "unrelated-additive-evidence"])
@pytest.mark.parametrize(
    "before,after",
    [
        ("The API key Abcd is active.", "Abcd is the active API key."),
        ("The API key PROD is active.", "PROD is the active API key."),
        ("The key Face is active.", "Face is the active key."),
        ("The API key Abcd is active.", "The active API key has value Abcd."),
    ],
)
async def test_explicit_identifier_retained_whole_token_anywhere(provider, config, before, after, cited):
    await assert_batch_action(
        provider, config, before, after, "update", before if cited is None else cited, correction=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cited", [None, ADDITIVE], ids=["same-text-evidence", "unrelated-additive-evidence"])
@pytest.mark.parametrize(
    "value,output",
    [
        ("Abcd", "abcd"),
        ("PROD", "prod"),
        ("Abcd", "Abcde"),
        ("Abcd", "xAbcd"),
        ("Abcd", "Abcd_x"),
        ("Abcd", "x_Abcd"),
        ("Abcd", "Abcd.x"),
        ("Abcd", "x.Abcd"),
        ("Abcd", "Abcd-x"),
        ("Abcd", "x-Abcd"),
    ],
)
async def test_explicit_identifier_case_and_longer_tokens_still_veto(provider, config, value, output, cited):
    before = f"The API key {value} is active."
    await assert_batch_action(
        provider,
        config,
        before,
        f"{output} is the active API key.",
        "fallback",
        before if cited is None else cited,
        correction=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cited", [None, ADDITIVE], ids=["same-text-evidence", "unrelated-additive-evidence"])
async def test_generic_identifier_repeated_occurrence_still_veto(provider, config, cited):
    before = "Alpha uses abc1234 and beta uses abc1234."
    await assert_batch_action(
        provider, config, before, "Alpha uses abc1234.", "fallback", before if cited is None else cited, correction=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("predicate", ["is", "are", "was", "were", "equals", "set to", ":", "="])
@pytest.mark.parametrize("state", ["revoked", "active", "expired", "valid", "rotated"])
async def test_a2_lowercase_binding_complement_is_not_an_identifier(provider, config, predicate, state):
    await assert_batch_action(
        provider,
        config,
        f"The token {predicate} {state} after 7 days.",
        f"The token has been {state} after 7 days.",
        "update",
        ADDITIVE,
        correction=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("predicate", ["was", "has been", "got"])
async def test_a2_revoked_predicate_restatement_is_lossless(provider, config, predicate):
    await assert_batch_action(
        provider,
        config,
        f"The token {predicate} revoked after 7 days.",
        "After 7 days, the token got revoked.",
        "update",
        ADDITIVE,
        correction=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "The case-sensitive API key is AbC1234.",
        "The case-sensitive API key is PROD.",
        "The case-sensitive API key is Abcd.",
        "The case-sensitive API key is ApiKey.",
        "Use the case-sensitive API key AbC1234 for login.",
        "Use the case-sensitive API key PROD for login.",
        "The case-sensitive API key is Face.",
        "The case-sensitive identifier is PROD.",
        "The case-sensitive API key is abc1234.",
        "Use the case-sensitive API key abc1234 for login.",
        "The case-sensitive API key is prod.",
        "The API key Abcd is active.",
        "The token XYZ123 is active.",
        "The key is PROD; the token is PROD.",
    ],
)
async def test_a2_unchanged_identifier_contexts_accept(provider, config, text):
    await assert_batch_action(provider, config, text, text, "update", ADDITIVE, correction=True)


@pytest.mark.asyncio
async def test_a2_ordinary_prose_remains_case_insensitive(provider, config):
    await assert_batch_action(
        provider,
        config,
        "The configured pool is PROD.",
        "THE CONFIGURED POOL IS prod.",
        "update",
        ADDITIVE,
        correction=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("predicate", ["equals", "equal to", "is equal to", "="])
async def test_a3_equals_predicate_keeps_primary_and_backup_distinct(provider, config, predicate):
    before = f"Primary timeout {predicate} 5 seconds."
    after = f"Backup timeout {predicate} 5 seconds."
    await assert_batch_action(provider, config, before, after, "fallback", ADDITIVE, correction=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("assignment", ["=", "= ", " ="])
async def test_a3_unspaced_equals_keeps_primary_and_backup_distinct(provider, config, assignment):
    await assert_batch_action(
        provider,
        config,
        f"Primary timeout{assignment}7 seconds.",
        f"Backup timeout{assignment}7 seconds.",
        "fallback",
        ADDITIVE,
        correction=True,
    )


@pytest.mark.parametrize(
    "text",
    ["a=b", "`a=b`", "a=7", "https://example.invalid/?a=b", "Endpoint https://example.invalid/?a=7"],
)
def test_a3_compact_code_and_query_noise_do_not_create_subject_slots(text):
    from hindsight_api.engine.consolidation import detail_loss as d

    index = d._prepare([text], d._Budget())[text]
    assert not any(clause.label for clause in index.clauses)


@pytest.mark.asyncio
async def test_a3_compact_equals_accepts_same_subject_restatement(provider, config):
    await assert_batch_action(
        provider,
        config,
        "Primary timeout=7 seconds.",
        "Primary timeout = 7 seconds.",
        "update",
        ADDITIVE,
        correction=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after,expected",
    [
        ("The invoice amount is EUR $750.", "fallback"),
        ("The invoice amount is US$750.", "update"),
        ("The invoice amount is $750 USD.", "update"),
    ],
)
async def test_b2_spaced_currency_symbol_prefix_is_part_of_money(provider, config, after, expected):
    await assert_batch_action(
        provider, config, "The invoice amount is USD $750.", after, expected, ADDITIVE, correction=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before,after",
    [
        ("The script file job.py was updated.", "The script file `job.py` was updated."),
        ("The timeout is 30 seconds.", "The configured timeout is 30 seconds."),
        ("The suite was updated from v1.2.3 to v1.2.4.", "Suite updated from v1.2.3 to v1.2.4."),
        ("The operation has no errors.", "No errors were reported for the operation."),
        ("The primary timeout is 5 seconds.", "The primary timeout remains 5 seconds."),
        ("The schedule is 09:12:05 UTC.", "The schedule is today 09:12:05 UTC."),
        ("The service uses 3 CPU cores.", "The service uses 3 processor cores."),
        ("The primary timeout was: 5 seconds.", "Primary timeout: 5 seconds."),
        ("The purchase amount is XYZ 500.", "The purchase amount is ABC 500."),
    ],
)
async def test_b1_single_retained_value_allows_restatement(provider, config, before, after):
    await assert_batch_action(provider, config, before, after, "update", ADDITIVE, correction=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before,after",
    [
        ("Primary timeout: 5 seconds.", "Backup timeout: 5 seconds."),
        ("Primary timeout: 5 seconds.", "Primary backup timeout: 5 seconds."),
        ("Primary timeout: 5 seconds.", "Use 5 seconds for service."),
        ("The primary timeout is 5 seconds.", "Use 5 seconds for service."),
        (
            "The primary invoice is US$500; the backup invoice is C$500.",
            "The primary invoice is C$500; the backup invoice is US$500.",
        ),
        (
            "Primary timeout: 5 seconds; backup timeout: 9 seconds.",
            "Backup timeout: 5 seconds; backup timeout: 9 seconds.",
        ),
    ],
)
async def test_b1_explicit_relocation_and_true_multi_slot_still_veto(provider, config, before, after):
    await assert_batch_action(provider, config, before, after, "fallback", ADDITIVE, correction=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("acronym", ["UTC", "CPU", "API", "GPU", "ISO", "XYZ", "ABC"])
async def test_b2_technical_acronyms_are_not_currency_at_update_boundary(provider, config, acronym):
    await assert_batch_action(
        provider,
        config,
        f"The reading is 12 {acronym}.",
        f"The reading is 12 {acronym.lower()}.",
        "update",
        ADDITIVE,
        correction=True,
    )


@pytest.mark.parametrize("acronym", ["UTC", "CPU", "API", "GPU", "ISO", "XYZ", "ABC"])
def test_b2_technical_acronyms_have_no_money_anchor(acronym):
    from hindsight_api.engine.consolidation.detail_loss import anchors

    assert not any(a.kind == "money" for a in anchors(f"12 {acronym}; {acronym} 12."))


@pytest.mark.asyncio
@pytest.mark.parametrize("code", "USD EUR GBP CAD AUD NZD JPY CNY HKD SGD CHF SEK NOK DKK INR KRW MXN BRL ZAR".split())
async def test_b2_real_currency_suffix_and_prefix_remain_equivalent(provider, config, code):
    await assert_batch_action(
        provider,
        config,
        f"The amount is {code} 500.",
        f"The amount is 500 {code}.",
        "update",
        ADDITIVE,
        correction=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["Primary timeout", "`Primary timeout`", '"Primary timeout"'])
async def test_b3_replacement_label_ignores_article_and_quote_delimiters(provider, config, label):
    before = "The Primary timeout: 5 seconds."
    after = f"{label}: 10 seconds."
    await assert_batch_action(provider, config, before, after, "update", after, correction=True)


@pytest.mark.parametrize(
    "dense",
    ["(500 USD) ", "key PROD ", "(500) balance; ", "(111111111111111111111111 ", "word ", " ", "111,", "USD111,"],
)
def test_new_regexes_are_cpu_bounded_on_256k_dense_input(dense):
    from time import process_time

    from hindsight_api.engine.consolidation import detail_loss as d

    text = (dense * (262144 // len(dense) + 1))[:262144]
    times = {}
    for name in (
        "_MONEY",
        "_SUFFIX_MONEY",
        "_SCALED_NUMBER",
        "_NUMERIC_SPANS",
        "_ACCOUNTING_NUMBER",
        "_ACCOUNTING_CONTEXT",
        "_EXPLICIT_IDENTIFIER",
        "_SUBJECT",
        "_HEADER",
        "_OPAQUE",
    ):
        start = process_time()
        if name in {"_SUFFIX_MONEY", "_SCALED_NUMBER"}:
            # These patterns are called only at maximal numeric starts. Raw
            # finditer would resurrect the removed comma-tail restart path.
            starts = [match.start() for match in d._NUMERIC_SPANS.finditer(text)]
            [getattr(d, name).match(text, pos) for pos in starts]
        else:
            list(getattr(d, name).finditer(text))
        times[name] = process_time() - start
    start = process_time()
    d._canonical_label(text)
    times["label_canonicalization"] = process_time() - start
    start = process_time()
    d.anchors(text)
    times["all_extraction"] = process_time() - start
    print(f"DENSE_REGEX_CPU_SECONDS={times}")
    assert max(times.values()) < 2.0


def test_accounting_preprocessing_is_cpu_bounded_on_256k_aggregate_input():
    from time import process_time

    from hindsight_api.engine.consolidation import detail_loss as d

    # Exact reviewer probe: one clause with many amounts, not isolated regexes.
    text = ("balance " + "(1)" * (262144 // 3 + 1))[:262144]
    times = []
    for _ in range(5):
        start = process_time()
        d.dropped_supported_anchors(text[:-12], "Plain prose.", [], [])
        times.append(process_time() - start)
    print(f"ACCOUNTING_256K_AGGREGATE_CPU_SECONDS={times}")
    assert max(times) < 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize("cited", [None, ADDITIVE], ids=["same-text-evidence", "unrelated-additive-evidence"])
@pytest.mark.parametrize(
    "before,after",
    [
        pytest.param(
            "The primary API key is Abcd. The backup API key is Abcd.",
            "The primary API key is Abcd.",
            id="multiplicity-drop",
        ),
        pytest.param(
            "The API key Abcd is active and the backup key Wxyz is revoked.",
            "The API key Wxyz is active; Abcd was rotated out.",
            id="role-swap",
        ),
        *[
            pytest.param(
                "The API key Abcd is active.",
                f"The API key Wxyz is active; {tail}",
                id=name,
            )
            for name, tail in [
                ("url-only", "the archive URL is https://example.invalid/Abcd."),
                ("path-only", "the archive path is /srv/Abcd/config."),
                ("email-only", "contact Abcd@example.invalid."),
                ("call-only", "`cache(Abcd)` returns true."),
            ]
        ],
        *[
            pytest.param("The API key Abcd is active.", f"{value} is the active API key.", id=name)
            for name, value in [
                ("combining-mark", "Abcd\u0301"),
                ("substring", "Abcde"),
                ("prefix", "xAbcd"),
                ("underscore", "Abcd_2"),
                ("hyphen", "Abcd-v2"),
                ("dot", "Abcd.v2"),
                ("case-change", "abcd"),
                ("reorder-url-only", "https://example.invalid/Abcd"),
                ("reorder-path-only", "/srv/Abcd/config"),
                ("reorder-email-only", "Abcd@example.invalid"),
                ("reorder-call-only", "cache(Abcd)"),
            ]
        ],
        pytest.param(
            "The primary API key is Abcd. The backup API key is Abcd.",
            "Abcd is the active API key.",
            id="reorder-multiplicity-drop",
        ),
    ],
)
async def test_pure_reorder_waiver_preserves_identifier_occurrences_and_boundaries(
    provider, config, before, after, cited
):
    await assert_batch_action(
        provider, config, before, after, "fallback", before if cited is None else cited, correction=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cited", [None, ADDITIVE], ids=["same-text-evidence", "unrelated-additive-evidence"])
@pytest.mark.parametrize(
    "value,after",
    [
        ("Abcd", "Abcd is the active API key."),
        ("PROD", "PROD is the active API key."),
        ("Face", "Face is the active API key."),
        ("Abcd", "The active API key has value Abcd."),
        ("Abcd", "Abcd, the active API key, is used."),
    ],
)
async def test_pure_reorder_waiver_accepts_exact_standalone_values(provider, config, value, after, cited):
    before = f"The API key {value} is active."
    await assert_batch_action(
        provider, config, before, after, "update", before if cited is None else cited, correction=True
    )


def test_identifier_retention_filter_is_cpu_bounded_on_256k_aggregate_input():
    from time import process_time

    from hindsight_api.engine.consolidation import detail_loss as d

    before = " ".join(f"The API key is Abcd{i:04d}." for i in range(1000))
    n = 262144 - 2 * len(before) - len(ADDITIVE)
    after = ("plain " * (n // 6 + 1))[: n - 1] + "."
    assert 2 * len(before) + len(after) + len(ADDITIVE) == 262144
    start = process_time()
    drops = d.dropped_supported_anchors(
        before,
        after,
        [d.Evidence(before, "2026-09-29T10:00:00Z")],
        [d.Evidence(ADDITIVE, "2026-09-30T10:00:00Z")],
    )
    cpu = process_time() - start
    print(f"IDENTIFIER_RETENTION_FILTER_256K_CPU_SECONDS={cpu:.6f}")
    assert len(drops) == 1000
    assert all(anchor.kind == "identifier" for anchor in drops)
    assert cpu < 1.0


@pytest.mark.asyncio
async def test_grouped_number_scan_is_cpu_bounded_at_real_update_boundary(provider, config):
    from time import process_time

    text = "1" + ",111" * ((32000 - 1) // 4)
    before = f"The measured number is {text}."
    start = process_time()
    await assert_batch_action(provider, config, before, before, "update", ADDITIVE, correction=True)
    cpu = process_time() - start
    print(f"GROUPED_32K_BOUNDARY_CPU_SECONDS={cpu:.6f}")
    assert cpu < 1.5


@pytest.mark.parametrize("size", [32000, 256000])
def test_grouped_number_extraction_is_cpu_bounded(size):
    from time import process_time

    from hindsight_api.engine.consolidation.detail_loss import anchors

    text = "1" + ",111" * ((size - 1) // 4)
    start = process_time()
    result = anchors(text)
    cpu = process_time() - start
    print(f"GROUPED_{size}_EXTRACTION_CPU_SECONDS={cpu:.6f}")
    assert result
    assert cpu < 1.5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before,after,expected",
    [
        ("The note,500 USD was retained.", "The note,USD 500 was retained.", "update"),
        ("The amount is 1,111 USD.", "The amount is USD 1111.", "update"),
        ("The amount is 1,111k.", "The amount is 1111000.", "update"),
        ("The amount is 1,111k.", "The amount is 1111.", "fallback"),
    ],
)
async def test_grouped_scan_preserves_comma_lists_and_canonical_scales(provider, config, before, after, expected):
    await assert_batch_action(provider, config, before, after, expected, ADDITIVE, correction=True)


@pytest.mark.parametrize(
    "before,after,work,exhausted",
    [
        ("balance (1)(1)", "plain prose.", 50, False),
        ("balance (1)(1)", "plain prose.", 10, True),
        ("5", "5", 4, False),
        ("5", "5", 3, True),
        ('"x y"', "x y", 50, False),
        ('"x y"', "x y", 10, True),
        ("USD 1k", "none", 14, False),
        ("USD 1k", "none", 1, True),
    ],
)
def test_evidence_free_updates_still_fail_closed_on_exhausted_work(monkeypatch, before, after, work, exhausted):
    from hindsight_api.engine.consolidation import detail_loss as d

    budget_type = d._Budget
    monkeypatch.setattr(d, "_MAX_WORK", work)
    monkeypatch.setattr(d, "_Budget", lambda: budget_type(remaining=work))
    expected = [d.Anchor("budget", "detail-loss analysis limit exceeded")] if exhausted else []
    assert d.dropped_supported_anchors(before, after, [], []) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("balance (1)(1)", "balance (1)(1)"),
        ("plain prose.\nnext line.", "plain prose.\nnext line."),
        ("key one.one", "key one.one"),
        ("oneUSD", "oneUSD"),
        ("one\t two", "1 2"),
        ("ＡＢＣ one", "abc 1"),
        ("USD 1k", "USD 1k"),
        ("The timeout is twenty seconds.", "the timeout is 20 seconds."),
    ],
)
def test_normalization_preserves_opaque_values_and_prose_transforms(text, expected):
    from hindsight_api.engine.consolidation import detail_loss as d

    assert d.normalize(text) == expected

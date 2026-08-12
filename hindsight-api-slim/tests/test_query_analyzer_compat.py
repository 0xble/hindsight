"""Characterization suite for temporal extraction.

This is the compatibility oracle for the ``search_dates`` performance work. It
snapshots what ``DateparserQueryAnalyzer.analyze()`` returns for every query in
``query_analyzer_corpus`` across several reference dates, and fails if any
answer changes.

The point is *behavioural equivalence*, not correctness: the golden file records
what the pre-optimisation implementation did, including cases where that answer
is arguably wrong. Optimisation must not change any of it. If a golden value is
genuinely wrong, fix it in a separate change with its own reasoning so the
diff is reviewable.

Regenerate (only from a known-good tree) with::

    HS_REGEN_GOLDEN=1 uv run pytest tests/test_query_analyzer_compat.py

No database is touched: everything here is pure CPU.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from hindsight_api.engine.query_analyzer import DateparserQueryAnalyzer
from hindsight_api.engine.search.temporal_extraction import extract_temporal_constraint
from tests.query_analyzer_corpus import build_corpus

GOLDEN_PATH = Path(__file__).parent / "data" / "query_analyzer_golden.json"

# Several reference dates so relative expressions exercise different weekday,
# month-start and year-boundary branches.
REFERENCE_DATES = [
    datetime(2026, 8, 12, 14, 30, 45, 123456),  # Wednesday, mid-month
    datetime(2026, 1, 1, 0, 0, 0),  # New Year's Day (Thursday)
    datetime(2026, 12, 31, 23, 59, 59),  # year end
    datetime(2024, 2, 29, 12, 0, 0),  # leap day
    datetime(2026, 8, 3, 9, 0, 0),  # a Monday
    datetime(2026, 8, 9, 18, 0, 0),  # a Sunday
]


def _key(query: str, ref: datetime) -> str:
    return f"{ref.isoformat()}\x1f{query}"


def _encode(result) -> list[str] | None:
    if result is None:
        return None
    start, end = result
    return [start.isoformat(), end.isoformat()]


def _compute_all() -> dict[str, list[str] | None]:
    """Run every corpus query against every reference date."""
    analyzer = DateparserQueryAnalyzer()
    out: dict[str, list[str] | None] = {}
    for query, _category in build_corpus():
        for ref in REFERENCE_DATES:
            out[_key(query, ref)] = _encode(extract_temporal_constraint(query, reference_date=ref, analyzer=analyzer))
    return out


def _load_golden() -> dict[str, list[str] | None]:
    if not GOLDEN_PATH.exists():
        pytest.fail(f"Golden file missing: {GOLDEN_PATH}. Regenerate with HS_REGEN_GOLDEN=1 on a known-good tree.")
    with GOLDEN_PATH.open() as fh:
        return json.load(fh)


@pytest.mark.skipif(not os.getenv("HS_REGEN_GOLDEN"), reason="regeneration is opt-in")
def test_regenerate_golden() -> None:
    """Write the golden file. Opt-in; never runs in CI."""
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = _compute_all()
    with GOLDEN_PATH.open("w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    print(f"\nWrote {len(data)} golden cases to {GOLDEN_PATH}")


@pytest.mark.skipif(bool(os.getenv("HS_REGEN_GOLDEN")), reason="regenerating")
def test_golden_corpus_unchanged() -> None:
    """Every corpus query must produce exactly the recorded constraint."""
    golden = _load_golden()
    actual = _compute_all()

    missing = sorted(set(golden) - set(actual))
    added = sorted(set(actual) - set(golden))
    assert not missing, f"{len(missing)} golden cases no longer produced, e.g. {missing[:5]}"
    assert not added, f"{len(added)} new corpus cases have no golden value (regenerate deliberately), e.g. {added[:5]}"

    diffs = []
    for key in sorted(golden):
        if golden[key] != actual[key]:
            ref, query = key.split("\x1f", 1)
            diffs.append(f"  ref={ref} query={query!r}\n    golden={golden[key]}\n    actual={actual[key]}")
    assert not diffs, f"{len(diffs)} behavioural changes:\n" + "\n".join(diffs[:25])


def test_corpus_is_deduplicated() -> None:
    queries = [q for q, _ in build_corpus()]
    assert len(queries) == len(set(queries))


def test_corpus_covers_every_category() -> None:
    categories = {c for _, c in build_corpus()}
    expected = {
        "non_temporal_en",
        "tz_abbrev_trap",
        "false_positive_trap",
        "period_en",
        "period_es",
        "period_it",
        "period_fr",
        "period_de",
        "period_ru",
        "month_year",
        "period_zh",
        "cjk_embedded",
        "explicit_date",
        "long_text",
        "edge_case",
    }
    assert categories == expected


def test_analyzer_and_wrapper_agree() -> None:
    """extract_temporal_constraint must mirror analyze() exactly, minus the guard."""
    analyzer = DateparserQueryAnalyzer()
    ref = REFERENCE_DATES[0]
    for query, _category in build_corpus():
        try:
            analysis = analyzer.analyze(query, ref)
        except Exception:
            # The wrapper degrades to None where analyze() raises (#3217).
            assert extract_temporal_constraint(query, reference_date=ref, analyzer=analyzer) is None
            continue
        expected = None
        if analysis.temporal_constraint:
            expected = (
                analysis.temporal_constraint.start_date,
                analysis.temporal_constraint.end_date,
            )
        assert extract_temporal_constraint(query, reference_date=ref, analyzer=analyzer) == expected, query


def test_language_restricted_analyzer_matches_on_fastpath() -> None:
    """Pinning languages must not change results that never reach dateparser."""
    from tests.query_analyzer_corpus import PERIOD_EN, PERIOD_RU, PERIOD_ZH

    auto = DateparserQueryAnalyzer()
    pinned = DateparserQueryAnalyzer(languages=["en", "es", "it", "fr", "de", "ru", "zh"])
    ref = REFERENCE_DATES[0]
    for query in PERIOD_EN + PERIOD_RU + PERIOD_ZH:
        assert auto.analyze(query, ref) == pinned.analyze(query, ref), query

"""Differential test: our fast detector must agree with dateparser's, always.

``hindsight_api.engine.temporal_language_detection.best_language`` is a hand-
optimised copy of ``FullTextLanguageDetector._best_language``. Hand-optimised
copies of third-party internals rot silently when the dependency is upgraded,
so this suite compares the two implementations directly rather than asserting
fixed expectations: the reference is dateparser itself.

If a dateparser upgrade changes detection semantics, this fails — which is the
point. It is the tripwire that lets us keep the copy.
"""

import random
import string
import sys

import pytest

from hindsight_api.engine.temporal_language_detection import best_language
from tests.query_analyzer_corpus import build_corpus

dateparser = pytest.importorskip("dateparser")

from dateparser.search import _search_with_detection  # noqa: E402
from dateparser.search.text_detection import FullTextLanguageDetector  # noqa: E402

ALL_LOCALES = list(_search_with_detection.available_language_map.values())


def reference(text: str) -> str | None:
    """dateparser's own answer. A fresh detector each call: it mutates itself."""
    return FullTextLanguageDetector(ALL_LOCALES)._best_language(text)


def assert_same(text: str) -> None:
    expected = reference(text)
    actual = best_language(text, ALL_LOCALES)
    assert actual == expected, f"detector disagreement on {text!r}: ours={actual!r} dateparser={expected!r}"


def test_agrees_on_full_corpus() -> None:
    for query, _category in build_corpus():
        assert_same(query)


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "\n",
        "0",
        "2026-06-10",
        "12/31/1999",
        "(1.2.3)",
        "-",
        "a",
        "ab",
        "ЖЖЖ",
        "日本語",
        "한국어",
        "ελληνικά",
        "עברית",
        "العربية",
        "ไทย",
        "हिन्दी",
        "ᏣᎳᎩ",
        "🧠🧠🧠",
        "café naïve",
        "EST",
        "12:00 EST",
        "meeting at 3pm EST on tuesday",
        "GMT+5",
        "UTC",
        "x" * 2000,
    ],
)
def test_agrees_on_edge_cases(text: str) -> None:
    assert_same(text)


def test_agrees_on_symbol_only_strings() -> None:
    """The symbol-set shortcut path (returns the first locale unconditionally)."""
    for text in ["123", "1/2/3", "(1.2)", "12:30", "2026-06-10", "1,2", "-.:", "0 0 0"]:
        assert_same(text)


def test_agrees_on_timezone_bearing_text() -> None:
    """The strip_timezone retry path — the branch we hoisted out of the loop."""
    for text in [
        "3pm EST",
        "meeting EST",
        "connection pooling",  # 'ect' matches the tz guard mid-word
        "what happened",  # 'hat' matches the tz guard mid-word
        "expected CAT WET MET",
        "2026-06-10 12:00 UTC",
        "east of the office",
    ]:
        assert_same(text)


def _random_text(rng: random.Random) -> str:
    alphabets = [
        string.ascii_lowercase,
        string.ascii_letters + string.digits,
        string.printable,
        "абвгдежзийклмнопрстуфхцчшщъыьэюя",
        "一二三四五六七八九十年月日上下周昨今明",
        "あいうえおかきくけこ日本語",
        "αβγδεζηθικλμνξοπρστυφχψω",
        "ابتثجحخدذرزسشصضطظعغ",
        "0123456789:/-. ",
        "áéíóúñàèìòùäöüßçãõ",
    ]
    alphabet = rng.choice(alphabets)
    length = rng.choice([0, 1, 2, 3, 5, 8, 13, 40, 120])
    return "".join(rng.choice(alphabet) for _ in range(length))


@pytest.mark.parametrize("seed", range(8))
def test_agrees_on_random_strings(seed: int) -> None:
    """Fuzz. Fixed seeds so a failure is reproducible."""
    rng = random.Random(seed)
    for _ in range(150):
        assert_same(_random_text(rng))


def test_agrees_on_random_mixed_scripts() -> None:
    """Mixed-script strings exercise the unique-character short-circuit."""
    rng = random.Random(1234)
    fragments = ["hello", "上周", "вчера", "こんにちは", "2026-06-10", "ayer", "EST", "ελληνικά", "🧠", "café"]
    for _ in range(400):
        n = rng.randint(1, 5)
        assert_same(" ".join(rng.choice(fragments) for _ in range(n)))


def test_char_table_cache_is_shared_and_correct() -> None:
    """The cached character tables must match a freshly computed set."""
    from hindsight_api.engine.temporal_language_detection import _char_table_cache, _char_tables
    from dateparser.conf import settings as ds

    _char_table_cache.clear()
    first = _char_tables(ALL_LOCALES, ds)
    second = _char_tables(ALL_LOCALES, ds)
    assert first is second, "second call should hit the cache"

    detector = FullTextLanguageDetector(ALL_LOCALES)
    detector.get_unique_characters(ds.replace(NORMALIZE=False))
    assert first.language_chars == detector.language_chars
    assert first.unique_chars == detector.language_unique_chars


def test_subset_locale_lists_are_cached_separately() -> None:
    """A pinned language list must not reuse the full list's tables."""
    subset = [loc for loc in ALL_LOCALES if loc.shortname in {"en", "es", "it"}]
    for text in ["ayer", "hello world", "2026-06-10", "cosa ho fatto"]:
        expected = FullTextLanguageDetector(subset)._best_language(text)
        assert best_language(text, subset) == expected, text


@pytest.mark.skipif(sys.version_info < (3, 11), reason="requires 3.11+")
def test_detection_is_thread_safe() -> None:
    """Concurrent detection must not corrupt the shared character-table cache."""
    import concurrent.futures

    from hindsight_api.engine.temporal_language_detection import _char_table_cache

    _char_table_cache.clear()
    texts = [q for q, _ in build_corpus()][:80]
    expected = {t: reference(t) for t in texts}

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda t: (t, best_language(t, ALL_LOCALES)), texts * 4))
    for text, got in results:
        assert got == expected[text], text

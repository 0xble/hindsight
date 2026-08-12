"""The pre-filter must never reject a query that could have produced a date.

``_query_can_score`` skips dateparser entirely when no span of the query could
score above zero. That is only safe if it is a true over-approximation of
``_date_match_score``. This suite pins that relationship from both directions:

* every word the scorer awards points for is recognised by the pre-filter
  (so adding a word to one set without the other fails here), and
* on random and corpus input, "pre-filter says no" always agrees with "the full
  dateparser search finds nothing that scores".

The second check is the important one: it compares against the real search, so
it would catch an over-eager pre-filter even if the word lists stayed in sync.
"""

import random
import string

import pytest

from hindsight_api.engine.query_analyzer import (
    _MONTH_WORDS,
    _PERIOD_WORDS,
    _RELATIVE_WORDS,
    _SCOREABLE_WORDS,
    _WEEKDAY_WORDS,
    DateparserQueryAnalyzer,
    _date_match_score,
    _query_can_score,
)
from tests.query_analyzer_corpus import build_corpus


def test_scoreable_words_cover_every_scoring_set() -> None:
    """The pre-filter alternation must be the union of the scorer's word sets."""
    assert _SCOREABLE_WORDS == _MONTH_WORDS | _RELATIVE_WORDS | _WEEKDAY_WORDS | _PERIOD_WORDS


@pytest.mark.parametrize("word", sorted(_MONTH_WORDS | _RELATIVE_WORDS | _WEEKDAY_WORDS | _PERIOD_WORDS))
def test_every_scoring_word_passes_the_prefilter(word: str) -> None:
    """Any word the scorer rewards must reach dateparser, alone or in a sentence."""
    assert _date_match_score(word) > 0, "corpus assumption: this word scores"
    assert _query_can_score(word)
    assert _query_can_score(f"some text {word} more text")
    assert _query_can_score(word.upper())


@pytest.mark.parametrize("digit", list(string.digits))
def test_digits_always_pass_the_prefilter(digit: str) -> None:
    assert _query_can_score(f"note {digit} here")


def test_prefilter_rejects_plain_prose() -> None:
    """Sanity: the common recall case really is short-circuited."""
    for query in [
        "how does the reranker work",
        "user preferences for code style",
        "who owns the billing service",
        "上海的天气",
    ]:
        assert not _query_can_score(query), query


def _scores_something(analyzer: DateparserQueryAnalyzer, query: str) -> bool:
    """Run the real dateparser search and report whether anything scored."""
    from dateparser.conf import settings as ds

    try:
        results = analyzer._find_dates(query, settings=ds)
    except Exception:
        return False
    if not results:
        return False
    from hindsight_api.engine.temporal_periods import is_embedded_cjk_dateparser_match

    return any(
        _date_match_score(text) > 0 for text, _date in results if not is_embedded_cjk_dateparser_match(query, text)
    )


def _assert_sound(analyzer: DateparserQueryAnalyzer, query: str) -> None:
    if _query_can_score(query):
        return  # slow path taken; nothing to prove
    assert not _scores_something(analyzer, query), (
        f"pre-filter rejected {query!r} but the real search found a scoring match"
    )


def test_prefilter_is_sound_on_corpus() -> None:
    analyzer = DateparserQueryAnalyzer()
    analyzer.load()
    for query, _category in build_corpus():
        _assert_sound(analyzer, query)


@pytest.mark.parametrize("seed", range(6))
def test_prefilter_is_sound_on_random_text(seed: int) -> None:
    """Fuzz against the real search. Fixed seeds keep failures reproducible."""
    analyzer = DateparserQueryAnalyzer()
    analyzer.load()
    rng = random.Random(seed)
    alphabets = [
        string.ascii_lowercase + " ",
        string.ascii_letters + string.digits + " ",
        string.printable,
        "абвгдежзийклмнопрстуфхцчшщ ",
        "一二三四五六七八九十年月日上下周昨今明 ",
        "áéíóúñàèìòùäöüß ",
    ]
    for _ in range(120):
        alphabet = rng.choice(alphabets)
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        _assert_sound(analyzer, text)


def test_prefilter_is_sound_on_word_salad() -> None:
    """Real words are likelier than random noise to trip the search."""
    analyzer = DateparserQueryAnalyzer()
    analyzer.load()
    rng = random.Random(99)
    vocabulary = [
        "the",
        "user",
        "asked",
        "about",
        "schema",
        "vector",
        "index",
        "recall",
        "bank",
        "memory",
        "fact",
        "model",
        "graph",
        "entity",
        "link",
        "token",
        "mon",
        "tue",
        "sept",
        "jan",
        "dec",
        "wed",
        "sun",
        "sat",
        "may",
        "march",
        "monat",
        "semana",
        "settimana",
        "неделя",
        "周",
        "月",
        "日",
        "ieri",
        "ayer",
    ]
    for _ in range(600):
        text = " ".join(rng.choice(vocabulary) for _ in range(rng.randint(1, 8)))
        _assert_sound(analyzer, text)

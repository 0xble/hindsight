"""Behaviour-identical, allocation-free replacement for dateparser's search-time
language detection.

``dateparser.search.search_dates`` spends ~98% of its time in
``FullTextLanguageDetector._best_language`` deciding which of 205 locales the
text is in. Three costs in that function are pure waste, and none of them are
fixed upstream (``search/text_detection.py`` on dateparser master is byte-identical
to the 1.2.2 copy we ship):

1. ``get_unique_characters()`` does an O(locales²) set-difference sweep — 42,025
   set operations — to work out which characters are unique to each locale. The
   result depends only on the locale set, which never changes, but a fresh
   ``FullTextLanguageDetector`` is constructed per call so it is recomputed every
   time. **Cached here.**

2. Each locale that scores zero is retried with ``strip_timezone=True``, and that
   retry calls ``pop_tz_offset_from_string`` on *the same string* — up to 199
   times per query, each one a linear scan of 773 compiled timezone regexes
   (~154k regex searches per query). The result cannot differ between locales.
   **Hoisted to one call.**

3. When stripping the timezone does not change the string, the retry re-runs a
   computation whose result is already known to be ``[0, 0]``. **Skipped.**

Every transformation here is equivalence-preserving by construction, and
``tests/test_temporal_language_detection.py`` asserts that directly: it runs this
implementation and dateparser's side by side over the corpus plus thousands of
randomly generated strings and requires identical answers.
"""

from __future__ import annotations

import threading

from dateparser.conf import settings as default_settings
from dateparser.languages.locale import Locale
from dateparser.timezone_parser import pop_tz_offset_from_string
from dateparser.utils import normalize_unicode

# Mirrors FullTextLanguageDetector.character_check. A string made only of these
# characters carries no language signal, so detection short-circuits to the
# first candidate locale.
_SYMBOL_SET = frozenset("0123456789 /-)(.:\\,'")

_char_table_lock = threading.Lock()
# id(locale tuple) is not stable, so key on the locale shortnames.
_char_table_cache: dict[tuple[str, ...], tuple[list[set], list[set]]] = {}


def _char_tables(languages: list[Locale], settings) -> tuple[list[set], list[set]]:
    """Per-locale character sets, and the characters unique to each locale.

    This is ``FullTextLanguageDetector.get_unique_characters`` with its result
    memoised. It is a pure function of the locale set: ``get_wordchars_for_detection``
    caches on the (singleton) ``Locale``, and the O(n²) difference sweep over
    those sets is therefore deterministic.
    """
    key = tuple(lang.shortname for lang in languages)
    cached = _char_table_cache.get(key)
    if cached is not None:
        return cached

    detection_settings = settings.replace(NORMALIZE=False)
    language_chars = [lang.get_wordchars_for_detection(settings=detection_settings) for lang in languages]

    unique_chars = []
    for char_set in language_chars:
        remaining = char_set
        for other in language_chars:
            if other != char_set:
                remaining = remaining - other
        unique_chars.append(remaining)

    with _char_table_lock:
        _char_table_cache[key] = (language_chars, unique_chars)
    return language_chars, unique_chars


def _character_check(text: str, languages: list[Locale], settings) -> list[Locale]:
    """Narrow the candidate locales by character evidence.

    Faithful to ``FullTextLanguageDetector.character_check``, including its
    quirks: it inspects the *original-cased* string, and a single unique
    character decides the language outright.
    """
    lowered = text.lower()
    text_chars = set(lowered)
    if text_chars & _SYMBOL_SET == text_chars:
        return languages[:1]

    language_chars, unique_chars = _char_tables(languages, settings)

    for i, chars in enumerate(unique_chars):
        for char in chars:
            if char.lower() in lowered:
                return [languages[i]]

    return [lang for i, lang in enumerate(languages) if text_chars & language_chars[i]]


def best_language(text: str, languages: list[Locale], settings=None) -> str | None:
    """Return the detected locale shortname, or None.

    Equivalent to ``FullTextLanguageDetector(languages)._best_language(text)``.

    Note the argument order dateparser uses and that we must preserve: the
    character check runs on the raw string, and only afterwards is the text
    lowercased and unicode-normalised for the applicability counts.
    """
    settings = settings or default_settings

    candidates = _character_check(text, languages, settings)
    text = normalize_unicode(text.lower())
    if len(candidates) == 1:
        return candidates[0].shortname

    # Hoisted out of the per-locale loop: the timezone strip depends only on the
    # text, so all 199 retries would compute the same thing.
    stripped, _ = pop_tz_offset_from_string(text, as_offset=False)
    tz_changed = stripped != text

    applicable: list[tuple[str, list[int]]] = []
    for language in candidates:
        counts = language.count_applicability(text, strip_timezone=False, settings=settings)
        if counts[0] > 0 or counts[1] > 0:
            applicable.append((language.shortname, counts))
            continue
        if not tz_changed:
            # The retry strips a timezone that is not there, so it would re-run
            # the identical computation and get the identical [0, 0].
            continue
        counts = language.count_applicability(stripped, strip_timezone=False, settings=settings)
        if counts[0] > 0 or counts[1] > 0:
            applicable.append((language.shortname, counts))

    if not applicable:
        return None
    return max(applicable, key=lambda pair: (pair[1][0], pair[1][1]))[0]

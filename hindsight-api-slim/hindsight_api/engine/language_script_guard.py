"""Source-relative script guard backported verbatim from maintained main.

Keep the copied guard and constants identical to language_integrity.py on main.
This compatibility module avoids upgrading unrelated runtime/schema surfaces.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

_MIN_NOVEL_SCRIPT_LETTERS = 4

_MAX_NAME_RUN_LETTERS = 4

_MIXED_MIN_FOREIGN_SHARE = 0.20

_LITERAL_CODE = re.compile(r"```.*?```|`[^`]*`", re.DOTALL)


def _letter_script(char: str) -> str:
    name = unicodedata.name(char, "")
    script = next(
        (
            candidate
            for candidate in (
                "LATIN",
                "CYRILLIC",
                "GREEK",
                "ARABIC",
                "HEBREW",
                "DEVANAGARI",
                "BENGALI",
                "GEORGIAN",
                "ARMENIAN",
                "HIRAGANA",
                "KATAKANA",
                "HANGUL",
                "THAI",
            )
            if candidate in name
        ),
        "HAN" if "CJK UNIFIED IDEOGRAPH" in name else "OTHER",
    )
    return "EAST_ASIAN" if script in {"HAN", "HIRAGANA", "KATAKANA", "HANGUL"} else script


def _script_counts(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for char in text:
        if not unicodedata.category(char).startswith("L"):
            continue
        counts[_letter_script(char)] += 1
    return counts


def _non_latin_script_runs(text: str) -> list[tuple[str, str]]:
    runs: list[tuple[str, str]] = []
    script = ""
    letters: list[str] = []
    for char in text:
        if not unicodedata.category(char).startswith("L"):
            if letters:
                runs.append((script, "".join(letters)))
                letters = []
            script = ""
            continue
        next_script = _letter_script(char)
        if next_script in {"LATIN", "OTHER"}:
            if letters:
                runs.append((script, "".join(letters)))
                letters = []
            script = ""
            continue
        if letters and next_script != script:
            runs.append((script, "".join(letters)))
            letters = []
        script = next_script
        letters.append(char)
    if letters:
        runs.append((script, "".join(letters)))
    return runs


def has_introduced_script_prose(source_text: str, generated_text: str) -> bool:
    """Detect substantial novel non-Latin prose in an otherwise Latin source.

    This stdlib-only signal runs before language-ID abstention. Literal code,
    source-compatible foreign-script runs (including copied quotations), and
    short name-sized runs are excluded so it remains narrower than a global
    English-only policy.
    """

    source_evidence = source_text
    source_text = _LITERAL_CODE.sub("", source_text)
    generated_text = _LITERAL_CODE.sub("", generated_text)
    source_counts = _script_counts(source_text)
    if source_counts["LATIN"] < _MIN_NOVEL_SCRIPT_LETTERS:
        return False
    source_total = sum(source_counts.values())
    source_foreign_letters = sum(count for script, count in source_counts.items() if script not in {"LATIN", "OTHER"})
    if (
        source_total
        and source_foreign_letters >= _MIN_NOVEL_SCRIPT_LETTERS
        and (source_foreign_letters / source_total >= _MIXED_MIN_FOREIGN_SHARE)
    ):
        return False

    source_runs = _non_latin_script_runs(source_evidence)
    generated_runs = _non_latin_script_runs(generated_text)
    if len(generated_runs) == 1 and len(generated_runs[0][1]) <= _MAX_NAME_RUN_LETTERS:
        return False

    novel_counts: Counter[str] = Counter()
    for script, run in generated_runs:
        if not any(
            script == source_script and _script_run_is_evidenced(script, run, source_run)
            for source_script, source_run in source_runs
        ):
            novel_counts[script] += len(run)
    return any(count >= _MIN_NOVEL_SCRIPT_LETTERS for count in novel_counts.values())


def _script_run_is_evidenced(script: str, generated: str, source: str) -> bool:
    if generated in source:
        return True
    # Bounded affix changes preserve source-backed names in word-based scripts.
    # Do not apply stem matching to CJK, where a few characters can be prose.
    shorter = min(len(generated), len(source))
    if script == "EAST_ASIAN" or shorter < 5 or abs(len(generated) - len(source)) > 2:
        return False
    if source in generated:
        return True
    common_prefix = 0
    for left, right in zip(generated, source):
        if left != right:
            break
        common_prefix += 1
    return common_prefix >= max(4, shorter - 2)

"""Conservative lexical acceptance gate, not a semantic truth/entailment judge.

UPDATEs retain source lineage. An anchor supported by that lineage cannot disappear
merely because a model emitted a shorter restatement. Only a dated, same-slot
replacement (or explicit consumption of an available credit) exempts it. Missing
lineage does not manufacture support. Dedup folds are equivalence operations, so
both sides' anchors must survive without a temporal-supersession exemption.
"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

_MONTHS = "january february march april may june july august september october november december".split()
_MONTH_DATE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})\b", re.I)
_NUMBER_WORDS = dict(
    zip(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split(),
        range(21),
    )
)
_MARKERS = re.compile(r"\b(?:must(?: not| only)?|never|only|otherwise|not|no|neither|without|requires?|authorized)\b")
_PATTERNS = {
    "date": re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    "money": re.compile(r"[$€£]\s*\d[\d,]*(?:\.\d+)?"),
    "version": re.compile(r"\bv?\d+(?:\.\d+){2,}(?:[-+][\w.-]+)?\b"),
    "identifier": re.compile(
        r"\b(?:[a-f0-9]{7,64}|[\w]+(?:[._][\w]+)+|[A-Z]+-\d+|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\b", re.I
    ),
    "number": re.compile(r"(?<![\w])\d[\d,]*(?:\.\d+)?(?:%|st|nd|rd|th)?(?![\w])"),
    "literal": re.compile(r'`([^`\n]{1,160})`|"([^"\n]{1,160})"|“([^”\n]{1,160})”'),
}
_STOP = set(
    "the a an as of at on in to for from with and or is are was were has have had been by it its this that current latest now new old fact facts said states stated about successful succeeded fixed updated".split()
)
_TEMPORAL_SUFFIX = re.compile(
    r"\s*\((?:(?:event_date|occurred_start|occurred_end|mentioned_at)\s*=[^(),]+,?\s*)+\)\s*$"
)


@dataclass(frozen=True)
class Anchor:
    kind: str
    value: str


@dataclass(frozen=True)
class Evidence:
    text: str
    mentioned_at: str | datetime | None = None


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(str.maketrans("‐‑‒–—−", "------")).casefold()
    text = re.sub(r"\s+", " ", text)
    text = _MONTH_DATE.sub(lambda m: f"{m[3]}-{_MONTHS.index(m[1].lower()) + 1:02d}-{int(m[2]):02d}", text)
    text = re.sub(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", lambda m: str(_NUMBER_WORDS[m[0]]), text)
    return re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", r"\1", text)


def without_temporal_suffix(text: str) -> str:
    """Remove only the structured temporal annotation used on generated observations."""
    return _TEMPORAL_SUFFIX.sub("", text)


def anchors(text: str) -> list[Anchor]:
    normalized = normalize(text)
    result: list[Anchor] = []
    for kind, pattern in _PATTERNS.items():
        for match in pattern.finditer(normalized):
            value = next((g for g in match.groups() if g is not None), match[0])
            if kind == "identifier" and value.isalpha() and "." not in value and "_" not in value:
                continue  # ordinary words composed of a-f are not hashes
            result.append(Anchor(kind, value))
    result.extend(Anchor("marker", m[0]) for m in _MARKERS.finditer(normalized))
    return result


def _occurrences(anchor: Anchor, text: str) -> int:
    return len(re.findall(r"(?<!\w)" + re.escape(anchor.value) + r"(?!\w)", normalize(text)))


def _time(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _context(text: str, value: str) -> set[str]:
    text = normalize(text).split(" | ")[0]
    pos = text.find(value)
    if pos < 0:
        return set()
    before = re.findall(r"[a-z]+", text[:pos])[-6:]
    after = re.findall(r"[a-z]+", text[pos + len(value) :])[:6]
    return {word.removesuffix("s") for word in before + after if word not in _STOP}


def _superseded(anchor: Anchor, before: str, supporters: list[Evidence], cited: list[Evidence], after: str) -> bool:
    support_times = [_time(source.mentioned_at) for source in supporters]
    if not support_times or any(t is None for t in support_times):
        return False
    newest_support = max(t for t in support_times if t is not None)
    for source in cited:
        when = _time(source.mentioned_at)
        if when is None or when < newest_support:
            continue
        old, new = normalize(before), normalize(source.text).split(" | ")[0]
        # A consumed last credit has no remaining expiration/count slot. Equal
        # timestamps are allowed only for this explicit directed state transition.
        if (
            anchor.kind in {"number", "date"}
            and re.search(r"\bavailable\b", old)
            and re.search(r"\b(?:used|consumed)\b.*\b(?:last|remaining)\b", new)
            and any(label in old and label in new for label in ("reset", "credit", "token", "voucher"))
        ):
            return True
        if when <= newest_support:
            continue
        # Explicit current-state snapshots are replaceable as a unit only when
        # both identify the same subject label at their start. Historical details
        # in non-snapshot observations remain protected (e.g. merge-wrapper versions).
        old_header = re.match(r"current ([^:,.]{1,80})", old)
        new_header = re.match(r"current ([^:,.]{1,80})", new)
        if old_header and new_header:
            labels = _context(old_header[0], "current") & _context(new_header[0], "current")
            if labels and (anchor.kind != "marker" or anchor.value in {"no", "not"}):
                return True
        if anchor.kind == "marker":
            continue  # absence never rescinds an obligation or authorization
        if anchor.kind == "version" and any(
            re.search(r"\b(?:incorporated|historical|previously)\b", normalize(s.text)) for s in supporters
        ):
            continue
        context = set().union(*(_context(s.text, anchor.value) for s in supporters))
        for replacement in anchors(source.text):
            if (
                replacement.kind != anchor.kind
                or replacement.value == anchor.value
                or not _occurrences(replacement, after)
            ):
                continue
            overlap = context & _context(source.text, replacement.value)
            # Commits have one unambiguous slot label but must also share a
            # non-generic subject. Numbers/dates require two nearby content words.
            same_subject = any(
                other.value != anchor.value
                and other.kind in {"identifier", "number"}
                and _occurrences(other, source.text)
                for supporter in supporters
                for other in anchors(supporter.text)
            )
            if len(overlap) >= 2 or (anchor.kind == "identifier" and "commit" in overlap and same_subject):
                return True
    return False


def dropped_supported_anchors(before: str, after: str, existing: list[Evidence], cited: list[Evidence]) -> list[Anchor]:
    """Return only missing anchors with positive existing-source support.

    Timestamp authority is mentioned_at, never ingestion/updated_at or a future
    deadline's occurred_start. A missing/older/tied timestamp cannot authorize a
    different value. Lexical slot overlap is intentionally conservative; it is not
    evidence that two arbitrary prices/subjects are the same facet.
    """
    dropped: list[Anchor] = []
    for anchor, count in Counter(anchors(before)).items():
        needed = count if anchor.kind == "marker" else 1
        if _occurrences(anchor, after) >= needed:
            continue
        supporters = [source for source in existing if _occurrences(anchor, source.text)]
        if supporters and not _superseded(anchor, before, supporters, cited, after):
            dropped.append(anchor)
    return dropped


def dropped_merge_anchors(before: str, after: str) -> list[Anchor]:
    """A fold must preserve both texts' anchors, including repeated constraints."""
    return [
        anchor
        for anchor, count in Counter(anchors(before)).items()
        if _occurrences(anchor, after) < (count if anchor.kind == "marker" else 1)
    ]

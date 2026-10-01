"""Conservative lexical acceptance gate, not a semantic truth/entailment judge.

UPDATEs retain source lineage. An anchor supported by that lineage cannot disappear
merely because a model emitted a shorter restatement. Only a dated, same-slot
replacement (or explicit consumption of an available credit) exempts it. Missing
lineage does not manufacture support. Dedup folds are equivalence operations, so
both sides' anchors must survive without a temporal-supersession exemption.
"""

import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field
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
# Bound both preprocessing and the potentially cross-product slot comparisons.
# Exceeding either limit vetoes the rewrite/fold, even when no anchor was found.
_MAX_INPUT_CHARS = 262144
_MAX_SOURCES = 256
_MAX_WORK = 1000000


@dataclass(frozen=True)
class Anchor:
    kind: str
    value: str


_LIMIT_ANCHOR = Anchor("budget", "detail-loss analysis limit exceeded")


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


def _normalized_anchors(normalized: str) -> list[Anchor]:
    result: list[Anchor] = []
    for kind, pattern in _PATTERNS.items():
        for match in pattern.finditer(normalized):
            value = next((g for g in match.groups() if g is not None), match[0])
            if kind == "identifier" and value.isalpha() and "." not in value and "_" not in value:
                continue  # ordinary words composed of a-f are not hashes
            result.append(Anchor(kind, value))
    result.extend(Anchor("marker", m[0]) for m in _MARKERS.finditer(normalized))
    return result


def anchors(text: str) -> list[Anchor]:
    return _normalized_anchors(normalize(text))


def _time(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


class _WorkLimit(Exception):
    pass


@dataclass
class _Budget:
    remaining: int = _MAX_WORK

    def spend(self, amount: int = 1) -> None:
        self.remaining -= amount
        if self.remaining < 0:
            raise _WorkLimit


@dataclass
class _ValueTrie:
    children: dict[str, "_ValueTrie"] = field(default_factory=dict)
    value: str | None = None


@dataclass
class _TextIndex:
    normalized: str
    counts: Counter[Anchor]
    occurrences: Counter[str] = field(default_factory=Counter)
    contexts: dict[str, list[set[str]]] = field(default_factory=dict)
    snapshot_labels: set[str] = field(default_factory=set)
    available: bool = False
    consumed_last: bool = False
    credit_labels: set[str] = field(default_factory=set)
    historical: bool = False
    replacements: dict[str, list[Anchor]] = field(default_factory=dict)


@dataclass(frozen=True)
class _SourceIndex:
    text: _TextIndex
    when: datetime | None


def _context_at(
    words: list[re.Match[str]], starts: list[int], ends: list[int], pos: int, end: int, boundaries: list[int]
) -> set[str]:
    before = bisect_right(ends, pos)
    after = bisect_left(starts, end)
    clause = bisect_right(boundaries, pos)
    lower = bisect_left(starts, boundaries[clause - 1]) if clause else 0
    upper = bisect_left(starts, boundaries[clause]) if clause < len(boundaries) else len(words)
    nearby = words[max(lower, before - 6) : before] + words[after : min(upper, after + 6)]
    return {m[0].removesuffix("s") for m in nearby if m[0] not in _STOP}


def _index_occurrences(index: _TextIndex, trie: _ValueTrie, budget: _Budget) -> None:
    """Scan all values together, retaining lexical boundaries and unquoted literals.

    Previously each value normalized and regex-scanned the entire text. A shared
    trie instead visits matching prefixes only; every traversal is budgeted so
    long overlapping literals cannot turn this into unbounded quadratic work.
    """
    text = index.normalized
    main_end = text.find(" | ")
    main_end = len(text) if main_end < 0 else main_end
    words = list(re.compile(r"[a-z]+").finditer(text, endpos=main_end))
    starts, ends = [m.start() for m in words], [m.end() for m in words]
    # Do not borrow another slot's labels across a sentence or coordinated clause.
    # Decimal/version dots are not sentence boundaries.
    boundaries = [m.end() for m in re.finditer(r"[.;!?](?=\s|$)|\band\b", text[:main_end])]
    last_end: dict[str, int] = {}
    for pos, char in enumerate(text):
        if pos and (text[pos - 1].isalnum() or text[pos - 1] == "_"):
            continue
        node = trie.children.get(char)
        end = pos + 1
        while node is not None:
            budget.spend()
            value = node.value
            if value is not None and (end == len(text) or not (text[end].isalnum() or text[end] == "_")):
                # re.findall counts non-overlapping occurrences of each value.
                if pos >= last_end.get(value, 0):
                    index.occurrences[value] += 1
                    last_end[value] = end
                    index.contexts.setdefault(value, []).append(
                        _context_at(words, starts, ends, pos, end, boundaries) if end <= main_end else set()
                    )
            if end == len(text):
                break
            node = node.children.get(text[end])
            end += 1


def _prepare(texts: list[str], budget: _Budget) -> dict[str, _TextIndex]:
    if sum(len(text) for text in texts) > _MAX_INPUT_CHARS:
        raise _WorkLimit
    indexes: dict[str, _TextIndex] = {}
    values: set[str] = set()
    for text in texts:
        if text in indexes:
            continue
        normalized = normalize(text)
        budget.spend(len(normalized))
        if len(normalized) > _MAX_INPUT_CHARS:
            raise _WorkLimit
        counts = Counter(_normalized_anchors(normalized))
        budget.spend(sum(counts.values()))
        index = _TextIndex(normalized, counts)
        main = normalized.split(" | ")[0]
        header = re.match(r"current ([^:,.]{1,80})", main)
        if header:
            index.snapshot_labels = {
                w.removesuffix("s") for w in re.findall(r"[a-z]+", header[1])[:6] if w not in _STOP
            }
        index.available = bool(re.search(r"\bavailable\b", normalized))
        # A greedy used.*last search can backtrack quadratically when many
        # consumption words have no trailing last/remaining marker.
        seen_consumption = False
        for marker in re.finditer(r"\b(?:used|consumed|last|remaining)\b", main):
            if marker[0] in {"used", "consumed"}:
                seen_consumption = True
            elif seen_consumption:
                index.consumed_last = True
                break
        index.credit_labels = {label for label in ("reset", "credit", "token", "voucher") if label in main}
        index.historical = bool(re.search(r"\b(?:incorporated|historical|previously)\b", normalized))
        indexes[text] = index
        values.update(anchor.value for anchor in counts)
    trie = _ValueTrie()
    for value in values:
        budget.spend(len(value))
        node = trie
        for char in value:
            node = node.children.setdefault(char, _ValueTrie())
        node.value = value
    for index in indexes.values():
        _index_occurrences(index, trie, budget)
    return indexes


def _unmatched_occurrences(anchor: Anchor, before: _TextIndex, after: _TextIndex, budget: _Budget) -> list[int]:
    """Match repeated values by slot, never by order or by global counts alone."""
    contexts = before.contexts[anchor.value]
    if before.normalized == after.normalized:
        return []
    remaining = set(range(len(contexts)))
    exact_by_context: dict[frozenset[str], list[int]] = {}
    for i, context in enumerate(contexts):
        budget.spend()
        if context:
            exact_by_context.setdefault(frozenset(context), []).append(i)
    unpaired: list[set[str]] = []
    for context in after.contexts.get(anchor.value, []):
        budget.spend()
        exact = exact_by_context.get(frozenset(context), [])
        if exact:
            # Exact equivalent slots preserve multiplicity in linear work. Tied
            # replacement attribution still cannot supersede any remaining copy.
            remaining.remove(exact.pop())
        else:
            unpaired.append(context)
    for context in unpaired:
        budget.spend(len(contexts))
        scores = [len(context & old) for old in contexts]
        best = max(scores, default=0)
        candidates = [i for i, score in enumerate(scores) if score == best]
        if best >= 2 and len(candidates) == 1 and candidates[0] in remaining:
            remaining.remove(candidates[0])
        # A tied/weak match is not evidence of preservation. Compare against ALL
        # old slots, so two copies of a preserved timeout cannot stand in for retry.
    return sorted(remaining)


def _superseded(
    anchor: Anchor,
    missing: int,
    before: _TextIndex,
    after: _TextIndex,
    unmatched: list[int],
    supporters: list[_SourceIndex],
    cited: list[_SourceIndex],
    budget: _Budget,
) -> int:
    support_times = [source.when for source in supporters]
    if not support_times or any(t is None for t in support_times):
        return 0
    newest_support = max(t for t in support_times if t is not None)
    contexts = before.contexts[anchor.value]
    for source in cited:
        budget.spend()
        when, new = source.when, source.text
        if when is None or when < newest_support:
            continue
        # These established directed transitions replace a whole state snapshot,
        # not one arbitrary slot. Preserve their numeric/date component exemptions.
        if (
            anchor.kind in {"number", "date"}
            and before.available
            and new.consumed_last
            and before.credit_labels & new.credit_labels
        ):
            return missing
        if when <= newest_support:
            continue
        if before.snapshot_labels & new.snapshot_labels and (anchor.kind != "marker" or anchor.value in {"no", "not"}):
            return missing
        if anchor.kind == "marker":
            continue  # absence never rescinds an obligation or authorization
        if anchor.kind == "version" and any(s.text.historical for s in supporters):
            continue
        for replacement in new.replacements.get(anchor.kind, []):
            budget.spend()
            if replacement.value == anchor.value:
                continue
            for replacement_context in new.contexts.get(replacement.value, []):
                budget.spend(len(contexts))
                scores = [len(context & replacement_context) for context in contexts]
                best = max(scores, default=0)
                candidates = [i for i, score in enumerate(scores) if score == best]
                # Attribute the replacement against every old occurrence first.
                # A preserved/historical timeout cannot excuse a missing retry,
                # and tied slot attribution must fail closed.
                if not best or len(candidates) != 1 or candidates[0] not in unmatched:
                    continue
                context = contexts[candidates[0]]
                for supporter in supporters:
                    for supported_context in supporter.text.contexts.get(anchor.value, []):
                        budget.spend()
                        overlap = context & supported_context & replacement_context
                        for output_context in after.contexts.get(replacement.value, []):
                            budget.spend()
                            # A cited replacement merely appearing elsewhere in
                            # the output does not mean this slot was replaced.
                            output_overlap = overlap & output_context
                            if len(output_overlap) >= 2:
                                return 1
                            if anchor.kind == "identifier" and "commit" in output_overlap:
                                for other in supporter.text.counts:
                                    budget.spend()
                                    if (
                                        other.value != anchor.value
                                        and other.kind in {"identifier", "number"}
                                        and new.occurrences[other.value]
                                    ):
                                        return 1
    return 0


def dropped_supported_anchors(before: str, after: str, existing: list[Evidence], cited: list[Evidence]) -> list[Anchor]:
    """Return missing supported anchors; an exhausted work budget also vetoes an UPDATE.

    Timestamp authority is mentioned_at, never ingestion/updated_at or a future
    deadline's occurred_start. A missing/older/tied timestamp cannot authorize a
    different value. A same-slot replacement exempts at most ONE unmatched
    occurrence; preservation and replacement attribution must be unambiguous.
    Other occurrences of that value must survive. Snapshot/credit transitions
    remain explicit whole-state exceptions, not ordinary slot replacements.
    """
    try:
        if len(existing) + len(cited) > _MAX_SOURCES:
            raise _WorkLimit
        budget = _Budget()
        indexes = _prepare([before, after] + [s.text for s in existing] + [s.text for s in cited], budget)
        old, new = indexes[before], indexes[after]
        supporters_by_value: dict[str, list[_SourceIndex]] = {}
        for source in existing:
            prepared = _SourceIndex(indexes[source.text], _time(source.mentioned_at))
            for value in prepared.text.occurrences:
                budget.spend()
                supporters_by_value.setdefault(value, []).append(prepared)
        cited_indexes = [_SourceIndex(indexes[s.text], _time(s.mentioned_at)) for s in cited]
        # Filter replacement candidates once, rather than checking after-text for
        # every old anchor. An anchor absent from the output cannot replace a slot.
        prepared_replacements: set[int] = set()
        for source in cited_indexes:
            if id(source.text) in prepared_replacements:
                continue
            prepared_replacements.add(id(source.text))
            for replacement in source.text.counts:
                budget.spend()
                if new.occurrences[replacement.value]:
                    source.text.replacements.setdefault(replacement.kind, []).append(replacement)
        dropped: list[Anchor] = []
        for anchor, needed in old.counts.items():
            budget.spend()
            missing = needed - new.occurrences[anchor.value]
            if needed > 1:
                unmatched = _unmatched_occurrences(anchor, old, new, budget)
                missing = max(missing, len(unmatched))
            else:
                unmatched = list(range(len(old.contexts[anchor.value])))
            if missing <= 0:
                continue
            supporters = supporters_by_value.get(anchor.value, [])
            if (
                supporters
                and _superseded(anchor, missing, old, new, unmatched, supporters, cited_indexes, budget) < missing
            ):
                dropped.append(anchor)
        return dropped
    except _WorkLimit:
        return [_LIMIT_ANCHOR]


def dropped_merge_anchors(before: str, after: str) -> list[Anchor]:
    """A fold must preserve full multiplicity; work exhaustion vetoes the fold."""
    try:
        budget = _Budget()
        indexes = _prepare([before, after], budget)
        return [
            anchor
            for anchor, count in indexes[before].counts.items()
            if indexes[after].occurrences[anchor.value] < count
        ]
    except _WorkLimit:
        return [_LIMIT_ANCHOR]

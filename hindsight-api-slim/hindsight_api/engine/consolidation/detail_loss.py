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
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import chain

_MONTHS: list[str] = "january february march april may june july august september october november december".split()
_MONTH_DATE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})\b", re.I)
_NUMBER_WORDS: dict[str, int] = dict(
    zip(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split(),
        range(21),
    )
)
_MARKERS = re.compile(
    r"\b(?:must(?: not| only)?|never|only|otherwise|not|no|neither|without|requires?|authorized)\b", re.I
)
_PATTERNS = {
    "date": re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    "version": re.compile(r"\bv?\d+(?:\.\d+){2,}(?:[-+][\w.-]+)?\b"),
    "identifier": re.compile(
        r"\b(?:(?<![\d,.-])0\d+(?![.,-]\d)|[a-f0-9]{7,64}|[\w]+(?:[._][\w]+)+|[A-Z]+-\d+|"
        r"(?-i:(?=[A-Za-z0-9_]*[a-z])(?=[A-Za-z0-9_]*[A-Z])(?![A-Z][a-z0-9_]*\b)[A-Za-z0-9_]+)|"
        r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\b",
        re.I,
    ),
    "number": re.compile(r"(?<![\w.])(?:-(?<![\w.]-))?(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)(?:%|st|nd|rd|th)?(?![\w])"),
    "literal": re.compile(r'`([^`\n]{1,160})`|"([^"\n]{1,160})"|“([^”\n]{1,160})”'),
}
# One complete quantity owns its span; partial numeric/identifier matches must
# not survive underneath canonical amounts and reject equivalent spellings.
_SCALE = r"(?:[kmbt]|thousand|million|billion|trillion)"
_DIGITS = r"(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)"
# ISO-4217 codes, not arbitrary three-letter technical acronyms.
_CURRENCY_CODES = tuple("usd eur gbp cad aud nzd jpy cny hkd sgd chf sek nok dkk inr krw mxn brl zar".split())
_CURRENCY = "(?i:" + "|".join(_CURRENCY_CODES) + ")"
_SIGN = r"(?:-(?<![\w.]-))?"
_MONEY = re.compile(
    rf"(?<![\w.])(?P<sign>{_SIGN})(?P<open>\()?"
    rf"(?P<prefix>{_CURRENCY}(?:\s+[$€£])?\s*|(?i:[a-z]{{0,3}}\$|[€£])\s*)"
    rf"(?P<amount>{_DIGITS})(?P<scale>\s*(?i:{_SCALE})(?=\b|{_CURRENCY}\b))?"
    rf"(?P<suffix>\s*{_CURRENCY}\b)?(?P<close>\))?"
)
_SUFFIX_MONEY = re.compile(
    rf"(?<![\w.])(?P<sign>{_SIGN})(?P<open>\()?(?P<amount>{_DIGITS})"
    rf"(?P<scale>\s*(?i:{_SCALE})(?=\b|{_CURRENCY}\b))?(?P<suffix>\s*{_CURRENCY}\b)(?P<close>\))?"
)
_ACCOUNTING_NUMBER = re.compile(rf"\((?P<amount>{_DIGITS})\)")
_ACCOUNTING_CONTEXT = re.compile(r"\b(?:balance|net|loss|p&l|owed)\b", re.I)
_SCALED_NUMBER = re.compile(rf"(?<![\w.])(?P<amount>{_SIGN}{_DIGITS})(?P<scale>\s*(?i:{_SCALE})\b)(?![\w])")
_NUMERIC_SPANS = re.compile(rf"(?<![\w.]){_SIGN}\(?{_DIGITS}")
_SCALE_PLACES = {"k": 3, "thousand": 3, "m": 6, "million": 6, "b": 9, "billion": 9, "t": 12, "trillion": 12}
_CURRENCY_NAMES = {
    "us$": "usd",
    "c$": "cad",
    "ca$": "cad",
    "a$": "aud",
    "au$": "aud",
    "nz$": "nzd",
    "€": "eur",
    "£": "gbp",
}
_LIST_PREFIX = re.compile(r"^(?:[-*•]\s+|\d+[.)]\s+)")
_HEADER = re.compile(r"^([^:]{1,100}):(?=\s|$)")
_SUBJECT = re.compile(
    r"^(?:the |a |an )?(.+?)(?: (?:(?:is|are|was|were|has|had|changed|grew|fell|equals|equal to)\b|=)|=)",
    re.I,
)


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


# Opaque spellings are not prose: protect literals and identifier tokens before
# case folding. Only supported currencies need additional affixed-code spans.
_OPAQUE = re.compile(
    _PATTERNS["literal"].pattern
    + "|(?i:"
    + _PATTERNS["identifier"].pattern
    + rf")|\b{_CURRENCY}\b|\b{_CURRENCY}(?=\d|\.\d)|(?<=[\dkKmMbBtT]){_CURRENCY}\b"
)


# Bindings and noun appositions need identifier shape: unrestricted copulas
# captured ordinary "was revoked", while removing apposition lost "key Abcd".
# Explicit case-sensitive contexts and literals do not need that shape signal.
# Known limits: lowercase "key is prod" can change case unnoticed, and an
# ordinary capitalized apposition ("API key Rotation policy") can falsely veto.
# Predicate-first values and noun appositions keep the noun's slot identity, not
# the value as subject. UPDATE's raw-token waiver requires the key noun to be
# absent; unfamiliar noun-bearing bindings fail closed. Recognized inverse slots
# allow at most eight qualifiers of forty characters each, not arbitrary prose.
_IDENTIFIER_SHAPE = re.compile(r"[A-Z0-9_]|[A-Za-z0-9][.-][A-Za-z0-9]")
_IDENTIFIER_NOUN = r"(?:key|token|identifier|id|secret name|env var|flag)"
_IDENTIFIER_NOUN_PATTERN = re.compile(r"\b" + _IDENTIFIER_NOUN + r"\b", re.I)
_IDENTIFIER_CONTEXT = (
    r"(?:\bcase-sensitive(?:\s+(?:API\s+)?" + _IDENTIFIER_NOUN + r")?"
    r"(?:\s*[:=]\s*|\s+(?:is|are|was|were|equals|set to)\s+|\s+)"
    r"|(?P<shape>\b"
    + _IDENTIFIER_NOUN
    + r"(?:\s*[:=]\s*|\s+(?:is|are|was|were|equals|set to|has value|have value)\s+|\s+)))"
)
# Lookahead keeps a descriptive context from consuming the next explicit key:
# "case-sensitive identifier is PROD" still discovers "identifier is PROD".
_EXPLICIT_IDENTIFIER = re.compile(r"(?=" + _IDENTIFIER_CONTEXT + r"(?P<value>[A-Za-z0-9_][\w.-]{0,159})\b)", re.I)
# Whitespace boundaries exclude URL/path/email/call components. Bound both the
# token and qualifier grammar: an unbounded suffix would retry on dense input.
_PREDICATE_FIRST_IDENTIFIER = re.compile(
    r"(?<!\S)(?P<shape>(?P<value>[A-Za-z0-9_][\w.-]{0,159}))"
    r"(?:\s+(?:is|are|was|were|equals)\s+|,\s+)(?:the|a|an)\s+"
    r"(?P<slot>(?:[a-z][\w-]{0,39}\s+){0,8}?" + _IDENTIFIER_NOUN + r")\b",
    re.I,
)


def _explicit_identifiers(text: str) -> Iterator[re.Match[str]]:
    seen: set[tuple[int, int]] = set()
    for match in chain(_EXPLICIT_IDENTIFIER.finditer(text), _PREDICATE_FIRST_IDENTIFIER.finditer(text)):
        if match["shape"] and not _IDENTIFIER_SHAPE.search(match["value"]):
            continue
        tail = text[match.end("value") : match.end("value") + 20]
        if re.match(r"\s+(?:key|token|identifier|label)\b", tail, re.I):
            continue
        if match["value"].casefold() in {"key", "token", "identifier", "id", "flag"} and re.match(
            r"\s+(?:is|equals)\b", tail, re.I
        ):
            continue
        # Resource availability is already governed by the existing credit path,
        # not the spelling of a key/token value.
        if match["value"].casefold() not in _STOP | set(_NUMBER_WORDS) | {"available"}:
            # Case-sensitive and noun contexts can discover the same value span.
            span = match.span("value")
            if span not in seen:
                seen.add(span)
                yield match


# With these transforms absent from every substring, splitting around opaque
# spans cannot alter already lowercase ASCII text either.
_NORMALIZATION_TRIGGER = re.compile("|".join([*_MONTHS, *_NUMBER_WORDS]) + r"|\d(?:st|nd|rd|th)|[^\S \n]| {2}")


def normalize(text: str) -> str:
    if text.isascii() and text == text.casefold() and not _NORMALIZATION_TRIGGER.search(text):
        return text
    text = unicodedata.normalize("NFKC", text)
    pieces: list[str] = []
    end = 0
    spans = [(m.start(), m.end()) for m in _OPAQUE.finditer(text)]
    spans.extend((m.start("value"), m.end("value")) for m in _explicit_identifiers(text))
    for start, stop in sorted(spans, key=lambda span: (span[0], -span[1])):
        if start < end:
            continue
        token = text[start:stop]
        if token.casefold() in _STOP or token.casefold() in _NUMBER_WORDS:
            token = _normalize_prose(token)
        pieces.extend((_normalize_prose(text[end:start]), token))
        end = stop
    pieces.append(_normalize_prose(text[end:]))
    return "".join(pieces)


def _normalize_prose(text: str) -> str:
    text = text.casefold()
    # Statement dashes are hard boundaries; date/version/range hyphens are not.
    text = re.sub(r"(?<=\s)[–—](?=\s)", ";", text)
    text = text.translate(str.maketrans("‐‑‒–—−", "------"))
    text = re.sub(r"[^\S\n]+", " ", text)
    text = _MONTH_DATE.sub(lambda m: f"{m[3]}-{_MONTHS.index(m[1].lower()) + 1:02d}-{int(m[2]):02d}", text)
    text = re.sub(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", lambda m: str(_NUMBER_WORDS[m[0]]), text)
    return re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", r"\1", text)


def without_temporal_suffix(text: str) -> str:
    """Remove only the structured temporal annotation used on generated observations."""
    return _TEMPORAL_SUFFIX.sub("", text)


@dataclass(frozen=True)
class _AnchorOccurrence:
    anchor: Anchor
    start: int
    end: int


def _canonical_amount(amount: str, scale: str = "") -> str:
    # Decimal-point movement on strings is exact and linear, independent of
    # floating point, Decimal context, and Python's large-integer conversion cap.
    sign = "-" if amount.startswith("-") else ""
    whole, _, fraction = amount.removeprefix("-").replace(",", "").partition(".")
    digits = whole + fraction
    point = len(whole) + _SCALE_PLACES.get(scale.strip().casefold(), 0)
    digits += "0" * max(0, point - len(digits))
    whole = digits[:point].lstrip("0") or "0"
    fraction = digits[point:].rstrip("0")
    return (sign if whole != "0" or fraction else "") + whole + ("." + fraction if fraction else "")


def _quantity_matches(
    pattern: re.Pattern[str], normalized: str, numeric_starts: list[int] | None = None
) -> Iterator[re.Match[str]]:
    # These mandatory suffix fragments come from the same regex definitions.
    # If absent, none of the maximal numeric starts can match that pattern.
    if pattern in (_MONEY, _SUFFIX_MONEY):
        # ASCII substring searches use the same mandatory currency spellings.
        # Unicode keeps regex case semantics (e.g. dotted capital I).
        lowered = normalized.lower() if normalized.isascii() else None
        currency = (
            any(code in lowered for code in _CURRENCY_CODES)
            if lowered is not None
            else bool(re.search(_CURRENCY, normalized))
        )
        if not currency and (pattern is _SUFFIX_MONEY or not any(symbol in normalized for symbol in "$€£")):
            return
    if pattern is _SCALED_NUMBER and not re.search(_SCALE + r"\b", normalized, re.I):
        return
    if pattern is _MONEY:
        yield from pattern.finditer(normalized)
    else:
        starts = (
            numeric_starts
            if numeric_starts is not None
            else (match.start() for match in _NUMERIC_SPANS.finditer(normalized))
        )
        for start in starts:
            match = pattern.match(normalized, start)
            if match is not None:
                yield match


def _evidence_free_work_fits(before: str, after: str) -> bool:
    # Without evidence, only a full-path work-limit veto can prevent an empty
    # result. Prove that path fits rather than allocating unsupported anchors.
    if len(before) + len(after) > _MAX_INPUT_CHARS:
        return False
    old, new = normalize(before), normalize(after)
    if max(len(old), len(new)) > _MAX_INPUT_CHARS:
        return False
    # Literal equivalents traverse a shared trie, so use the full path for
    # them. Everything else uses only authoritative extractor spans.
    if any(_PATTERNS["literal"].search(text) for text in (old, new)):
        return False
    # Count a superset of extractor candidates before overlap/shape filtering.
    # Each raw pattern/value key produces at most one canonical anchor.
    counts: list[int] = []
    old_keys: set[tuple[str, str]] = set()
    for index, text in enumerate((old, new)):
        count = 0
        patterns = [
            ("money", _quantity_matches(_MONEY, text)),
            ("suffix_money", _quantity_matches(_SUFFIX_MONEY, text)),
            ("scaled_number", _quantity_matches(_SCALED_NUMBER, text)),
            ("accounting", _ACCOUNTING_NUMBER.finditer(text)),
            *((kind, pattern.finditer(text)) for kind, pattern in _PATTERNS.items() if kind != "literal"),
            ("marker", _MARKERS.finditer(text)),
        ]
        for kind, matches in patterns:
            if index == 0:
                raw_counts = Counter(match[0] for match in matches)
                count += raw_counts.total()
                old_keys.update((kind, value) for value in raw_counts)
            else:
                count += sum(1 for _ in matches)
        for match in _explicit_identifiers(text):
            count += 1
            if index == 0:
                old_keys.add(("explicit_identifier", match["value"]))
        counts.append(count)
    prior, proposed = counts
    unique = len(old_keys)
    # No header/subject label can exist without one of these lexical forms.
    labeled = (
        ":" in old
        or "=" in old
        or re.search(r"\b(?:is|are|was|were|has|had|changed|grew|fell|equals|equal to)\b", old, re.I)
    )
    # Normalized lengths, extraction and indexed spans pay preprocessing.
    # Labels pay at most one operation per old occurrence. Each unique anchor
    # pays its loop, old/new contexts and worst-case unpaired cross product.
    work = len(old) + len(new) + 2 * (prior + proposed)
    work += prior if labeled else 0
    work += unique * (1 + prior + proposed + prior * proposed)
    return work <= _MAX_WORK


def _extract_occurrences(normalized: str) -> list[_AnchorOccurrence]:
    result: list[_AnchorOccurrence] = []
    quantities: list[tuple[int, int]] = []
    money_starts: list[int] = []
    # One maximal numeric run owns all grouped commas. Suffix searches at each
    # comma used to retry the remaining tail quadratically when no suffix existed.
    # Matching at maximal starts still admits prose lists such as note,500 USD.
    numeric_starts = [match.start() for match in _NUMERIC_SPANS.finditer(normalized)]
    for pattern, kind in ((_MONEY, "money"), (_SUFFIX_MONEY, "money"), (_SCALED_NUMBER, "number")):
        quantities.sort()
        money_starts = [start for start, _ in quantities]
        matches = _quantity_matches(pattern, normalized, numeric_starts)
        for match in matches:
            parent = bisect_right(money_starts, match.start()) - 1
            if parent >= 0 and match.end() <= quantities[parent][1]:
                continue
            value = _canonical_amount(match["amount"], match["scale"] or "")
            if kind == "money":
                raw_prefix = (match.groupdict().get("prefix") or "").strip().casefold()
                # An explicit ISO code owns an adjacent spaced currency symbol:
                # USD $750 is USD 750, not an unqualified $750.
                raw_prefix = re.sub(r"\s+[$€£]$", "", raw_prefix)
                prefix = _CURRENCY_NAMES.get(raw_prefix, raw_prefix)
                suffix = (match["suffix"] or "").strip().casefold()
                if match["sign"] or (match.groupdict().get("open") and match.groupdict().get("close")):
                    value = "-" + value
                if not prefix:
                    prefix = suffix
                # Explicit suffixes qualify an otherwise unspecified dollar.
                # Conflicting qualifiers remain distinct instead of guessing.
                currency = suffix if prefix == "$" and suffix else prefix
                if suffix and suffix != currency:
                    currency += "/" + suffix
                value = (currency if currency == "$" else currency + ":") + value
            result.append(_AnchorOccurrence(Anchor(kind, value), match.start(), match.end()))
            quantities.append((match.start(), match.end()))
    accounting_matches = list(_ACCOUNTING_NUMBER.finditer(normalized))
    if accounting_matches:
        boundaries = [0] + _clause_boundaries(normalized) + [len(normalized)]
        # Cache each clause's first content position once. Slicing and stripping
        # its growing prefix for every amount made dense single clauses quadratic.
        accounting_slots = {
            i: next((pos for pos in range(start, end) if not normalized[pos].isspace()), end)
            for i, (start, end) in enumerate(zip(boundaries, boundaries[1:]))
            if _ACCOUNTING_CONTEXT.search(normalized, start, end)
        }
        for match in accounting_matches:
            slot = bisect_right(boundaries, match.start()) - 1
            # A leading parenthesized list index is never an accounting sign.
            if slot in accounting_slots and accounting_slots[slot] < match.start():
                result.append(
                    _AnchorOccurrence(
                        Anchor("number", "-" + _canonical_amount(match["amount"])), match.start(), match.end()
                    )
                )
                quantities.append((match.start(), match.end()))
    quantities.sort()
    starts = [start for start, _ in quantities]
    identifier_spans: set[tuple[int, int]] = set()
    for match in _explicit_identifiers(normalized):
        start, end = match.span("value")
        identifier_spans.add((start, end))
        result.append(_AnchorOccurrence(Anchor("identifier", match["value"]), start, end))
    for kind, pattern in _PATTERNS.items():
        for match in pattern.finditer(normalized):
            group = next((i for i, g in enumerate(match.groups(), 1) if g is not None), 0)
            value = match[group]
            if kind == "identifier" and match.span(group) in identifier_spans:
                continue
            if kind == "literal" and re.fullmatch(r"[A-Za-z0-9_][\w-]{0,159}", value):
                if match.span(group) not in identifier_spans:
                    identifier_spans.add(match.span(group))
                    result.append(_AnchorOccurrence(Anchor("identifier", value), match.start(group), match.end(group)))
            quantity = bisect_right(starts, match.start(group)) - 1
            if kind in {"number", "identifier"} and quantity >= 0 and match.end(group) <= quantities[quantity][1]:
                continue
            if kind == "identifier" and (
                (
                    value.isalpha()
                    and not re.search(
                        r"(?:identifier|key|token) (?:is |: ?)?$",
                        normalized[max(0, match.start() - 40) : match.start()],
                        re.I,
                    )
                )
                or re.fullmatch(r"\d+\.\d+", value)
            ):
                continue
            if kind == "number":
                value = _canonical_amount(value.removesuffix("%")) + ("%" if value.endswith("%") else "")
            elif kind == "identifier":
                identifier_spans.add(match.span(group))
            result.append(_AnchorOccurrence(Anchor(kind, value), match.start(group), match.end(group)))
    result.extend(
        _AnchorOccurrence(Anchor("marker", m[0].casefold()), m.start(), m.end()) for m in _MARKERS.finditer(normalized)
    )
    return result


def anchors(text: str) -> list[Anchor]:
    return [occurrence.anchor for occurrence in _extract_occurrences(normalize(text))]


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
class _Clause:
    end: int
    label: str = ""
    label_end: int = 0
    snapshot: bool = False
    single_value: bool = False
    snapshot_values: list[Anchor] = field(default_factory=list)
    values: set[Anchor] = field(default_factory=set)
    credit_identity: str = ""
    available: bool = False
    consumed_last: bool = False


@dataclass
class _TextIndex:
    normalized: str
    counts: Counter[Anchor]
    spans: dict[str, set[tuple[int, int]]] = field(default_factory=dict)
    occurrences: Counter[str] = field(default_factory=Counter)
    contexts: dict[str, list[set[str]]] = field(default_factory=dict)
    clauses: list[_Clause] = field(default_factory=list)
    occurrence_clauses: dict[str, list[int | None]] = field(default_factory=dict)
    occurrence_in_label: dict[str, list[bool]] = field(default_factory=dict)
    historical: bool = False
    replacements: dict[str, list[Anchor]] = field(default_factory=dict)
    replacement_targets: dict[Anchor, "_ReplacementTarget | None"] = field(default_factory=dict)


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
    return {m[0].casefold().removesuffix("s") for m in nearby if m[0].casefold() not in _STOP}


def _header_match(text: str) -> re.Match[str] | None:
    match = _HEADER.match(text)
    if match:
        prefix = match[1]
        # A closed quoted label is explicit; a colon inside a literal is not.
        if prefix.count("`") % 2 or prefix.count('"') % 2 or prefix.count("“") != prefix.count("”"):
            return None
    return match


def _clause_boundaries(text: str) -> list[int]:
    """Structural factual slots; no inherited snapshot/credit authority.

    Commas inside grouped digits are part of amounts. A colon header (including
    its conjunctions) is one unit until a hard boundary. Outside headers retain
    ordinary coordinated-slot separation and numeric 'between X and Y' ranges.
    """
    boundaries: list[int] = []
    # The comma in "Abcd, the primary API key" joins value and slot. Splitting
    # there would erase its context and falsely veto a lossless apposition.
    apposition_commas = {
        match.end("value")
        for match in _PREDICATE_FIRST_IDENTIFIER.finditer(text)
        if text[match.end("value")] == "," and _IDENTIFIER_SHAPE.search(match["value"])
    }
    start = 0
    between = False
    header_end = -1
    for match in re.finditer(r"\n|[.;!?](?=\s|$)|(?<!\d),|,(?!\d{3}(?!\d))|\b(?:between|and)\b", text):
        if match.start() in apposition_commas:
            continue
        if match[0] in {"between", "and"}:
            if header_end < start:
                prefix = _LIST_PREFIX.sub("", text[start : start + 120].lstrip(), count=1)
                header = _header_match(prefix)
                header_end = start + header.end() if header else start
            if header_end > start:
                continue
            if match[0] == "between":
                between = True
                continue
            if between:
                between = False
                continue
        boundaries.append(match.end())
        start = match.end()
        header_end = -1
        between = False
    return boundaries


def _single_snapshot_value(text: str) -> bool:
    # Deliberately small value grammar, not another predicate detector. Anything
    # outside a bare value, quantity/range, or repeated total fails closed.
    occurrences = _extract_occurrences(text)
    masked: list[str] = []
    end = 0
    for occurrence in sorted(occurrences, key=lambda o: (o.start, -o.end)):
        if occurrence.start < end:
            continue
        masked.extend((text[end : occurrence.start], "#"))
        end = occurrence.end
    masked.append(text[end:])
    value = "".join(masked).strip().rstrip(".;!?").strip()
    if "(# total)" in value and len({o.anchor for o in occurrences}) != 1:
        return False
    return bool(re.fullmatch(r"(?:#|between # and #)(?: [a-z-]+){0,2}(?: \(# total\))?", value))


def _snapshot_values(text: str) -> list[Anchor]:
    # A distinct parenthesized total is an independent slot, not repetition.
    # These two roles let a citation replace either while preserving the other.
    match = re.fullmatch(rf"({_DIGITS})(?: [a-z-]+){{0,2}}(?: \(({_DIGITS}) total\))?[.;!?]?", text)
    return [Anchor("number", _canonical_amount(v)) for v in match.groups() if v is not None] if match else []


def _credit_identity(text: str, consumed: bool) -> str:
    text = text.casefold()
    if consumed:
        # The old .+? prefix retried an unbounded identity suffix at every
        # "used last" occurrence. Validate the suffix alphabet once, then take
        # the first viable action: the same leftmost identity, in linear work.
        ending = next((word for word in ("credit", "token", "voucher") if text.endswith(word)), "")
        if not ending or "\n" in text:
            return ""
        last_invalid = -1
        for invalid in re.finditer(r"[^a-z -]", text):
            last_invalid = invalid.start()
        for action in re.finditer(r"\b(?:used|consumed) (?:the )?(?:last|remaining)(?: remaining)? ", text):
            start = action.end()
            if (
                action.start() > 0
                and start > last_invalid
                and len(text) - start > len(ending)
                and "a" <= text[start] <= "z"
            ):
                return text[start:].strip()
        return ""
    else:
        match = re.fullmatch(
            r"(?:the |a |an )?([a-z][a-z -]*?(?:credit|token|voucher)) is available "
            r"(?:until \d{4}-\d{2}-\d{2}|for \d+ days?)",
            text,
        )
    return match[1].strip() if match else ""


def _index_occurrences(index: _TextIndex, trie: _ValueTrie, budget: _Budget) -> None:
    """Index extractor spans, plus bounded unquoted-literal equivalents.

    A second universal boundary check used to discard extracted currency and
    quoted matches, causing KeyErrors/false loss on unchanged text. Extraction
    now supplies authoritative spans for every kind. Only quote-removal needs
    a shared trie; each traversal remains budgeted against overlapping literals.
    """
    text = index.normalized
    main_end = text.find(" | ")
    main_end = len(text) if main_end < 0 else main_end
    words = list(re.compile(r"[a-z][a-z0-9_]*", re.I).finditer(text, endpos=main_end))
    starts, ends = [m.start() for m in words], [m.end() for m in words]
    # Do not borrow another slot's labels across a sentence or coordinated clause.
    # Decimal/version dots are not sentence boundaries.
    boundaries = _clause_boundaries(text[:main_end])
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
                    index.spans.setdefault(value, set()).add((pos, end))
                    last_end[value] = end
            if end == len(text):
                break
            node = node.children.get(text[end])
            end += 1
    for value, spans in index.spans.items():
        # Deduplicate coincident matches across kinds (e.g. a quoted number),
        # not different occurrences. Every extracted value gets contexts.
        for pos, end in sorted(spans):
            budget.spend()
            index.occurrences[value] += 1
            index.contexts.setdefault(value, []).append(
                _context_at(words, starts, ends, pos, end, boundaries) if end <= main_end else set()
            )
            clause = bisect_right(boundaries, pos)
            index.occurrence_in_label.setdefault(value, []).append(end <= index.clauses[clause].label_end)
            index.occurrence_clauses.setdefault(value, []).append(
                clause if end <= main_end and end <= index.clauses[clause].end else None
            )


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
        extracted = _extract_occurrences(normalized)
        counts = Counter(occurrence.anchor for occurrence in extracted)
        budget.spend(len(extracted))
        index = _TextIndex(normalized, counts)
        for occurrence in extracted:
            index.spans.setdefault(occurrence.anchor.value, set()).add((occurrence.start, occurrence.end))
        main = normalized.split(" | ")[0]
        boundaries = [0] + _clause_boundaries(main) + [len(main)]
        for start, end in zip(boundaries, boundaries[1:]):
            clause_text = main[start:end].strip().rstrip(".;!?").strip()
            clause_text = _LIST_PREFIX.sub("", clause_text, count=1)
            clause = _Clause(end)
            header = _header_match(clause_text)
            if header:
                clause.label = _canonical_label(header[1])
                clause.label_end = main.find(clause_text, start, end) + header.end(1)
                clause.snapshot = clause.label.startswith("current ")
                value_text = clause_text[header.end() :].strip()
                clause.single_value = _single_snapshot_value(value_text)
                clause.snapshot_values = _snapshot_values(value_text)
            else:
                # Ordered subject labels cover explicit prose slots without
                # turning unordered nearby-word overlap into slot identity.
                subject = _SUBJECT.match(clause_text)
                if (
                    subject
                    and clause_text[subject.end(1)] == "="
                    and not re.fullmatch(r"[\w-]+(?:\s+[\w-]+)+", subject[1])
                ):
                    # Only ordered prose subjects gain compact '=' binding.
                    # Standalone a=b code and URL/query syntax are not slots.
                    subject = None
                if subject and subject[1].casefold() not in _STOP | {"there"}:
                    clause.label = _canonical_label(subject[1])
                    clause.label_end = main.find(clause_text, start, end) + subject.end(1)
                predicate_first = _PREDICATE_FIRST_IDENTIFIER.match(clause_text)
                if predicate_first and _IDENTIFIER_SHAPE.search(predicate_first["value"]):
                    # "Abcd is the primary API key" binds Abcd to primary, not
                    # to a subject named Abcd. The value precedes the slot label,
                    # so it must not be marked as an occurrence inside that label.
                    clause.label = _canonical_label(predicate_first["slot"])
                    clause.label_end = 0
            clause.credit_identity = _credit_identity(clause_text, consumed=False)
            clause.available = bool(clause.credit_identity)
            if not clause.available:
                clause.credit_identity = _credit_identity(clause_text, consumed=True)
                clause.consumed_last = bool(clause.credit_identity)
            index.clauses.append(clause)
        slot_ends = boundaries[1:-1]
        for occurrence in extracted:
            slot = bisect_right(slot_ends, occurrence.start)
            if occurrence.end <= index.clauses[slot].end:
                index.clauses[slot].values.add(occurrence.anchor)
        index.historical = bool(re.search(r"\b(?:incorporated|historical|previously)\b", normalized))
        indexes[text] = index
        values.update(anchor.value for anchor in counts if anchor.kind == "literal")
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


def _canonical_label(label: str) -> str:
    label = label.translate(str.maketrans("", "", '`"“”')).strip().casefold()
    label = re.sub(r"\b(?:is|are|was|were)\b", "", label)
    label = " ".join(label.split())
    return re.sub(r"^(?:the|a|an)\s+", "", label)


def _label_adds_qualifier(old: str, new: str) -> bool:
    # Only the demonstrated subject qualifier is a restatement. Arbitrary
    # additions (e.g. "backup") must not alias otherwise distinct explicit slots.
    return new == "configured " + old


def _occurrence_label(index: _TextIndex, value: str, occurrence: int) -> str:
    slot = index.occurrence_clauses[value][occurrence]
    if slot is None:
        return ""
    clause = index.clauses[slot]
    # A token inside the subject is not a value placed under that subject.
    if index.occurrence_in_label[value][occurrence]:
        return ""
    values = [anchor.value for anchor in clause.snapshot_values]
    if len(values) == 2 and values[0] != values[1] and value in values:
        # A different total is its own slot even under the same colon header.
        return clause.label + (":total" if values.index(value) == 1 else ":value")
    return clause.label


def _unmatched_occurrences(anchor: Anchor, before: _TextIndex, after: _TextIndex, budget: _Budget) -> list[int]:
    """Match repeated values by slot, never by order or by global counts alone."""
    contexts = before.contexts[anchor.value]
    if before.normalized == after.normalized:
        return []
    remaining = set(range(len(contexts)))
    old_labels = [_occurrence_label(before, anchor.value, i) for i in range(len(contexts))]
    exact_by_context: dict[tuple[str, frozenset[str]], list[int]] = {}
    for i, context in enumerate(contexts):
        budget.spend()
        if context:
            exact_by_context.setdefault((old_labels[i], frozenset(context)), []).append(i)
    unpaired: list[tuple[str, set[str]]] = []
    for after_i, context in enumerate(after.contexts.get(anchor.value, [])):
        budget.spend()
        label = _occurrence_label(after, anchor.value, after_i)
        exact = exact_by_context.get((label, frozenset(context)), [])
        if exact:
            # Exact equivalent slots preserve multiplicity in linear work. Tied
            # replacement attribution still cannot supersede any remaining copy.
            remaining.remove(exact.pop())
        else:
            unpaired.append((label, context))
    for label, context in unpaired:
        budget.spend(len(contexts))
        scores = [
            (len(context & old) + (100 if label else 0))
            if old_labels[i] == label or (old_labels[i] and label and _label_adds_qualifier(old_labels[i], label))
            else len(context & old)
            if not old_labels[i] or not label
            else 0
            for i, old in enumerate(contexts)
        ]
        best = max(scores, default=0)
        candidates = [i for i, score in enumerate(scores) if score == best]
        if best >= 2 and len(candidates) == 1 and candidates[0] in remaining:
            remaining.remove(candidates[0])
        # A tied/weak match is not evidence of preservation. Compare against ALL
        # old slots, so two copies of a preserved timeout cannot stand in for retry.
    return sorted(remaining)


@dataclass(frozen=True)
class _ReplacementTarget:
    anchor: Anchor
    occurrence: int
    slot: int
    label: str


def _replacement_target(
    replacement: Anchor, source: _TextIndex, before: _TextIndex, budget: _Budget
) -> _ReplacementTarget | None:
    if replacement in source.replacement_targets:
        return source.replacement_targets[replacement]
    budget.spend(sum(len(before.occurrence_clauses[a.value]) for a in before.counts if a.kind == replacement.kind))
    old_slots = {
        slot
        for a in before.counts
        if a.kind == replacement.kind
        for slot in before.occurrence_clauses[a.value]
        if slot is not None
    }
    winners: set[_ReplacementTarget] = set()
    for source_i, context in enumerate(source.contexts.get(replacement.value, [])):
        best = 0
        candidates: list[_ReplacementTarget] = []
        for old in before.counts:
            if old.kind != replacement.kind:
                continue
            for i, old_context in enumerate(before.contexts[old.value]):
                budget.spend()
                score = len(context & old_context)
                old_slot = before.occurrence_clauses[old.value][i]
                source_slot = source.occurrence_clauses[replacement.value][source_i]
                if old_slot is None or source_slot is None:
                    continue
                label = before.clauses[old_slot].label
                source_label = source.clauses[source_slot].label
                # A label is required identity, never an overlap-score bonus.
                # Missing labels cannot select among multiple old factual slots.
                if label != source_label:
                    continue
                if not label and len(old_slots) > 1:
                    continue
                if label:
                    score += 100
                candidate = _ReplacementTarget(old, i, old_slot, label)
                if score > best:
                    best, candidates = score, [candidate]
                elif score == best:
                    candidates.append(candidate)
        if best >= 2 and len(candidates) == 1:
            winners.add(candidates[0])
        else:
            source.replacement_targets[replacement] = None
            return None
    target = next(iter(winners)) if len(winners) == 1 else None
    source.replacement_targets[replacement] = target
    return target


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
        exempt: set[int] = set()
        for i in unmatched:
            budget.spend()
            clause_id = before.occurrence_clauses[anchor.value][i]
            if clause_id is None:
                continue
            clause = before.clauses[clause_id]
            if anchor.kind in {"number", "date"} and clause.available:
                old_clauses = [c for c in before.clauses if c.available and c.credit_identity == clause.credit_identity]
                cited_clauses = [
                    c for c in new.clauses if c.consumed_last and c.credit_identity == clause.credit_identity
                ]
                output_clauses = [
                    c for c in after.clauses if c.consumed_last and c.credit_identity == clause.credit_identity
                ]
                budget.spend(len(before.clauses) + len(new.clauses) + len(after.clauses))
                identity_mentions = re.findall(r"\b" + re.escape(clause.credit_identity) + r"\b", before.normalized)
                if len(identity_mentions) == len(old_clauses) == len(cited_clauses) == len(output_clauses) == 1:
                    exempt.add(i)
            if when > newest_support and clause.snapshot:
                old_clauses = [c for c in before.clauses if c.label == clause.label]
                cited_clauses = [c for c in new.clauses if c.label == clause.label]
                output_clauses = [c for c in after.clauses if c.label == clause.label]
                budget.spend(len(before.clauses) + len(new.clauses) + len(after.clauses))
                if len(old_clauses) == len(cited_clauses) == len(output_clauses) == 1:
                    citation, output = cited_clauses[0], output_clauses[0]
                    if clause.single_value and citation.single_value and output.single_value:
                        replacements = citation.values & output.values
                        if any(a.kind == anchor.kind and a != anchor for a in replacements):
                            exempt.add(i)
                    elif clause.snapshot_values.count(anchor) == 1:
                        role = clause.snapshot_values.index(anchor)
                        if (
                            role < len(citation.snapshot_values)
                            and role < len(output.snapshot_values)
                            and citation.snapshot_values[role] == output.snapshot_values[role] != anchor
                        ):
                            exempt.add(i)
        if len(exempt) >= missing:
            return len(exempt)
        if when <= newest_support or anchor.kind == "marker":
            continue
        if anchor.kind == "version" and any(s.text.historical for s in supporters):
            continue
        for replacement in new.replacements.get(anchor.kind, []):
            budget.spend()
            if replacement.value == anchor.value:
                continue
            target = _replacement_target(replacement, new, before, budget)
            output_target = _replacement_target(replacement, after, before, budget)
            if (
                target is None
                or target != output_target
                or target.anchor != anchor
                or target.occurrence not in unmatched
            ):
                continue
            clause_id = before.occurrence_clauses[anchor.value][target.occurrence]
            if clause_id is None:
                continue
            clause = before.clauses[clause_id]
            # Generic overlap must never circumvent stricter header/credit
            # identity, cardinality, same-kind, or single-predicate rules.
            if clause.snapshot or clause.available:
                continue
            context = contexts[target.occurrence]
            for supporter in supporters:
                for supported_context in supporter.text.contexts.get(anchor.value, []):
                    budget.spend()
                    for replacement_context in new.contexts.get(replacement.value, []):
                        budget.spend()
                        overlap = context & supported_context & replacement_context
                        for output_context in after.contexts.get(replacement.value, []):
                            budget.spend()
                            if len(overlap & output_context) >= 2 or (
                                clause.label and clause.label in {c.label for c in new.clauses}
                            ):
                                return 1
    return 0


def _standalone_tokens(text: str, budget: _Budget) -> Counter[str]:
    """Count exact whitespace-delimited tokens, not components of opaque values."""
    budget.spend(len(text))
    counts: Counter[str] = Counter()
    for token in text.split():
        # Strip only one sentence punctuation mark at a whitespace/end boundary.
        # Internal punctuation, parentheses, suffixes and combining marks remain
        # part of the token, so no substring can masquerade as a retained value.
        if token[-1] in ".,;:!?":
            token = token[:-1]
        if token and token[0] in "\"'`“‘":
            token = token[1:]
        if token:
            counts[token] += 1
    return counts


def dropped_supported_anchors(before: str, after: str, existing: list[Evidence], cited: list[Evidence]) -> list[Anchor]:
    """Return missing supported anchors; an exhausted work budget also vetoes an UPDATE.

    Timestamp authority is mentioned_at, never ingestion/updated_at or a future
    deadline's occurred_start. A missing/older/tied timestamp cannot authorize a
    different value. A same-slot replacement exempts at most ONE unmatched
    occurrence; preservation and replacement attribution must be unambiguous.
    Other occurrences of that value must survive. Replacement authority is
    resolved across all old values of its kind; snapshot/credit identities are
    exact, unique, slot-local, and fail closed on ambiguous predicates.
    """
    try:
        if len(existing) + len(cited) > _MAX_SOURCES:
            raise _WorkLimit
        if not existing and not cited and _evidence_free_work_fits(before, after):
            return []
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
        labels_by_kind: dict[str, set[str]] = {}
        for clause in old.clauses:
            if clause.label:
                for value in clause.values:
                    budget.spend()
                    labels_by_kind.setdefault(value.kind, set()).add(_canonical_label(clause.label))
        dropped: list[Anchor] = []
        for anchor, needed in old.counts.items():
            budget.spend()
            missing = needed - new.occurrences[anchor.value]
            multi_slot = len(labels_by_kind.get(anchor.kind, set())) > 1
            labeled = any(_occurrence_label(old, anchor.value, i) for i in range(len(old.contexts[anchor.value])))
            if needed > 1 or multi_slot or labeled:
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
        # Raw retention is a waiver only when the key noun genuinely disappears.
        # Recognized predicate-first/apposition bindings use occurrence-level slot
        # matching above; unfamiliar noun-bearing output fails closed instead of
        # erasing a role swap on global token counts. Charge two shared token
        # passes, never a separate output scan per identifier.
        if not any(anchor.kind == "identifier" for anchor in dropped):
            return dropped
        budget.spend(len(before) + len(after))
        if _IDENTIFIER_NOUN_PATTERN.search(after) or next(_explicit_identifiers(after), None) is not None:
            return dropped
        explicit_values = {match["value"] for match in _explicit_identifiers(before)}
        before_tokens = _standalone_tokens(before, budget)
        after_tokens = _standalone_tokens(after, budget)
        return [
            anchor
            for anchor in dropped
            if anchor.kind != "identifier"
            or anchor.value not in explicit_values
            or not before_tokens[anchor.value]
            or after_tokens[anchor.value] < before_tokens[anchor.value]
        ]
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

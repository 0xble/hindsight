"""Coalescing several queued retains for one document into a single execution.

Appends to one document are serialized by the claim predicate
(``document_serialization_sql``), which is what makes them correct — but on its
own it also makes them slow: a client that buffers 50 turns offline and flushes
them would run 50 sequential retains, each re-reading and reprocessing the
document. Folding collapses that burst into one execution over the concatenated
turns.

The folding happens at **claim** time, over rows the claim transaction already
holds ``FOR UPDATE``, and it never rewrites an operation's ``task_payload``.
That is the whole reason this is tractable:

* Rows are immutable after insert, so there is no submit-vs-claim race to
  reason about — the alternative (merging new content into a pending row at
  submit time) races the worker reading that row and reintroduces exactly the
  lost-update bug this is meant to fix.
* Operation identity survives. Each submission keeps its own ``operation_id``,
  status, and post-retain hook, so nothing downstream of the queue — polling,
  webhooks, cancellation, metering — has to learn about folding.

Because of that, folding is a pure optimization: delete it and the system is
still correct, only slower. A bug here can cost latency or LLM spend; it cannot
lose a turn, because ``ConcurrentAppendConflict`` and the claim predicate carry
correctness independently.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Peers folded into one execution on a first attempt. Bounded so a document
# that has accumulated a long backlog still commits in reasonable steps rather
# than one enormous transaction; the remainder is claimed on the next cycle.
DEFAULT_MAX_FOLD_PEERS = 16


@dataclass
class FoldMember:
    """One submitted operation participating in a folded execution."""

    operation_id: str
    contents: list[dict[str, Any]]
    tenant_id: str | None = None
    api_key_id: str | None = None


@dataclass
class FoldMemberRef:
    """A fold member as it survives the trip through the task payload.

    The fold is decided at claim time and consumed by the engine, with the task
    payload (JSON) in between — so this is the boundary type: the payload
    carries plain dicts and they are parsed back into this the moment the engine
    reads them, rather than being passed around as loose dicts.

    Only what the engine needs downstream: which operation, and how many content
    items it contributed, which is what slices the execution's results back
    apart for the per-operation post-retain hooks.
    """

    operation_id: str
    items_count: int

    def to_payload(self) -> dict[str, Any]:
        return {"operation_id": self.operation_id, "items_count": self.items_count}

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "FoldMemberRef":
        return cls(operation_id=str(raw["operation_id"]), items_count=int(raw["items_count"]))

    @classmethod
    def list_from_payload(cls, raw: list[dict[str, Any]] | None) -> list["FoldMemberRef"] | None:
        return None if raw is None else [cls.from_payload(item) for item in raw]


@dataclass
class FoldPlan:
    """Which queued peers join this execution, and which stay pending."""

    members: list[FoldMember] = field(default_factory=list)
    """The primary operation first, then the peers folded into it, in submission order."""

    deferred: list[str] = field(default_factory=list)
    """Operation ids left pending for a later claim, with the reason logged."""

    @property
    def peer_ids(self) -> list[str]:
        """Ids of the folded peers — everything but the primary."""
        return [m.operation_id for m in self.members[1:]]


def max_fold_peers_for_retry(retry_count: int, base: int = DEFAULT_MAX_FOLD_PEERS) -> int:
    """Fold width for an operation on its ``retry_count``-th attempt.

    A folded execution is all-or-nothing: one poisonous turn (content that makes
    extraction fail every time) would otherwise take every turn folded with it
    down on every retry, and those turns would be re-folded with it again on the
    next attempt — a burst of good content held hostage by one bad item.

    Halving the width per retry makes the fold converge on the poison: by the
    time the operation has failed a few times it runs alone, fails alone, and
    is dead-lettered alone while its neighbours proceed. Expressed as a rule
    rather than a special case, so there is no "is this the bad one" heuristic
    to get wrong.
    """
    if retry_count <= 0:
        return base
    return max(0, base >> retry_count)


def plan_retain_fold(
    primary: FoldMember,
    peers: list[FoldMember],
    *,
    max_peers: int,
    token_budget: int,
    count_tokens,
) -> FoldPlan:
    """Choose the contiguous run of ``peers`` that folds into ``primary``.

    ``peers`` must already be ordered by ``(created_at, operation_id)`` — the
    order the claim query imposes and the order the document accumulates in.

    Folding stops at the **first** peer that cannot join, and takes nothing
    after it. Appends are cumulative, so skipping one and taking the next would
    commit turns out of order; a contiguous prefix is the only shape that keeps
    the document's text in submission order. Peers left behind stay pending and
    are claimed on a later cycle, still in order.

    A peer cannot join when:

    * it would push the execution past ``token_budget``. Beyond that the engine
      splits the work into sub-batches, and several sub-batches carrying
      different bodies for one document defeat the streaming ownership check
      (#3282) — so the fold stays inside a single orchestrator pass by
      construction. The primary alone is always allowed through, however large.
    * it belongs to a different tenant or API key. Token usage is attributed to
      the members of a fold, so mixing credentials inside one execution would
      bill one caller for another's extraction.
    * ``max_peers`` is already reached.
    """
    plan = FoldPlan(members=[primary])
    if max_peers <= 0:
        plan.deferred = [p.operation_id for p in peers]
        return plan

    used = sum(count_tokens(item.get("content", "")) for item in primary.contents)

    for index, peer in enumerate(peers):
        if len(plan.members) > max_peers:
            plan.deferred = [p.operation_id for p in peers[index:]]
            break
        if peer.tenant_id != primary.tenant_id or peer.api_key_id != primary.api_key_id:
            logger.debug(
                "Not folding %s: different tenant/api key from the primary operation",
                peer.operation_id,
            )
            plan.deferred = [p.operation_id for p in peers[index:]]
            break
        peer_tokens = sum(count_tokens(item.get("content", "")) for item in peer.contents)
        if used + peer_tokens > token_budget:
            plan.deferred = [p.operation_id for p in peers[index:]]
            break
        used += peer_tokens
        plan.members.append(peer)

    return plan


def merge_fold_contents(members: list[FoldMember]) -> list[dict[str, Any]]:
    """Flatten a fold's submissions into the content list for one execution.

    Order is the members' order, which is submission order — the document ends
    up carrying the turns exactly as the callers sent them.
    """
    merged: list[dict[str, Any]] = []
    for member in members:
        merged.extend(member.contents)
    return merged

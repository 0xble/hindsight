"""Hand-written consolidation answers for tests that stand in for the LLM.

The model answers one entry per new fact (``_FactBatchResponse``); a test usually knows the
writes it wants instead. :func:`fact_answer` gives a lossless verdict representation or
rejects it; :func:`post_conversion_answer` preserves exact downstream actions at the
explicit conversion boundary. :func:`batch_fact_ids` reads short ids from the prompt.
"""

import re
from copy import deepcopy
from typing import Any

from pydantic import PrivateAttr

from hindsight_api.engine.consolidation.consolidator import (
    _ConsolidationBatchResponse,
    _CreateAction,
    _DeleteAction,
    _FactBatchResponse,
    _FactDecision,
    _ObservationChange,
    _UpdateAction,
)


def batch_fact_ids(prompt: str) -> list[str]:
    """The ids of the new facts the prompt lists (``[F1] text``), in order."""
    return re.findall(r"^\[(F\d+)\]", prompt, re.MULTILINE)


def batch_fact_texts(prompt: str) -> dict[str, str]:
    """Each new fact's text by its id. The ids are F1, F2, … in every batch, so an observation
    written from the id alone repeats across batches."""
    return dict(re.findall(r"^\[(F\d+)\] (.*)$", prompt, re.MULTILINE))


def fact_answer(
    creates: list[_CreateAction] | None = None,
    updates: list[_UpdateAction] | None = None,
    deletes: list[_DeleteAction] | None = None,
) -> _FactBatchResponse:
    """The answer that turns into these writes: each is put on its first source fact, and the
    other source facts point at it (``same_as_fact`` for a create, ``repeats`` for an update)."""
    entries: dict[str, _FactDecision] = {}

    def entry(fact_id: str) -> _FactDecision:
        return entries.setdefault(fact_id, _FactDecision(fact_id=fact_id, kind="lasting", adds="no"))

    for c in creates or []:
        first, *rest = c.source_fact_ids
        entry(first).new.append(c.text)
        entry(first).adds = "yes"
        for other in rest:
            entry(other).same_as_fact = first
    for u in updates or []:
        first, *rest = u.source_fact_ids
        entry(first).existing.append(_ObservationChange(observation_id=u.observation_id, effect="changes", text=u.text))
        for other in rest:
            entry(other).existing.append(_ObservationChange(observation_id=u.observation_id, effect="repeats"))
    answer = _FactBatchResponse(facts=list(entries.values()), deletes=list(deletes or []))
    # One verdict cannot assign different citation sets to multiple new texts
    # from the same fact. In particular, same_as_fact cites only its first
    # CREATE and is ignored when that fact also creates its own observation.
    # Never quietly turn a malformed-reference regression into a valid reply.
    converted = answer.model_copy(deep=True).to_actions(
        {u.observation_id: u.text for u in updates or []}, {}, lambda *_: False, lambda *_: False
    )
    expected_creates = [(c.text, c.source_fact_ids) for c in creates or []]
    actual_creates = [(c.text, c.source_fact_ids) for c in converted.creates]
    expected_updates = [(u.observation_id, u.text, u.source_fact_ids) for u in updates or []]
    actual_updates = [(u.observation_id, u.text, u.source_fact_ids) for u in converted.updates]
    if actual_creates != expected_creates or actual_updates != expected_updates:
        raise ValueError(
            "Writes are not losslessly representable as verdicts; use post_conversion_answer at the action boundary"
        )
    return answer


class _PostConversionAnswer(_FactBatchResponse):
    """A deliberately injected action result, not a provider-protocol adapter."""

    _actions: _ConsolidationBatchResponse = PrivateAttr()

    def with_real_ids(self, real_id: dict[str, str]) -> "_PostConversionAnswer":
        for action in [*self._actions.creates, *self._actions.updates]:
            action.source_fact_ids = [real_id.get(fid, fid) for fid in action.source_fact_ids]
        for action in [*self._actions.updates, *self._actions.deletes]:
            action.observation_id = real_id.get(action.observation_id, action.observation_id)
        return self

    def to_actions(self, *args: Any, **kwargs: Any) -> _ConsolidationBatchResponse:
        return deepcopy(self._actions)


def post_conversion_answer(
    creates: list[Any] | None = None,
    updates: list[Any] | None = None,
    deletes: list[Any] | None = None,
) -> _FactBatchResponse:
    """Inject exact actions at ``to_actions`` for downstream reference/apply tests.

    Citation sets that overlap, include outsiders, or contain duplicate update
    proposals need not have a per-fact wire representation. Keep their original
    actions intact instead of exercising a lossy approximation of the verdict
    parser. Only MockLLM/direct-call fixtures may use this explicit seam; actual
    provider-schema tests must send real verdict payloads.
    """
    answer = _PostConversionAnswer()
    answer._actions = _ConsolidationBatchResponse.model_construct(
        creates=deepcopy(creates or []), updates=deepcopy(updates or []), deletes=deepcopy(deletes or []), skips=[]
    )
    return answer


def wire_fact_answer(writes: Any) -> Any:
    """Encode existing write-intent SDK fixtures in the released verdict protocol.

    Unlike ``fact_answer``, this also carries malformed fixtures to the provider
    parser. No invalid sibling is dropped: wrong containers/items stay wrong,
    absent identities stay absent, and invalid target types stay invalid. This
    is test fixture construction only, never a production compatibility parser.
    """
    if not isinstance(writes, dict) or "facts" in writes or not {"creates", "updates", "deletes"}.intersection(writes):
        return writes
    creates = writes.get("creates", [])
    if isinstance(creates, list) and all(
        isinstance(action, dict)
        and isinstance(action.get("text"), str)
        and action["text"].strip()
        and isinstance(action.get("source_fact_ids"), list)
        and action["source_fact_ids"]
        and all(isinstance(fid, str) for fid in action["source_fact_ids"])
        for action in creates
    ):
        # Well-formed wire fixtures have the same representability limit as the
        # typed helper. Malformed fixtures below must still reach the parser.
        fact_answer(creates=[_CreateAction(**action) for action in creates])
    facts = []
    for key in ("creates", "updates"):
        actions = writes.get(key, [])
        if not isinstance(actions, list):
            return {"facts": actions, "deletes": writes.get("deletes", [])}
        for action in actions:
            if not isinstance(action, dict):
                facts.append(action)
                continue
            sources = action.get("source_fact_ids")
            if not isinstance(sources, list) or not sources:
                # Missing attribution must reach the actual required-id check.
                facts.append({"kind": "lasting", "adds": "no", "existing": [], "new": []})
                continue
            first, *rest = sources
            if key == "creates":
                fact = {"fact_id": first, "kind": "lasting", "adds": "yes", "existing": []}
                if "text" in action:
                    fact["new"] = [action["text"]]
                facts.append(fact)
                facts.extend(
                    {"fact_id": fid, "kind": "lasting", "adds": "no", "existing": [], "new": [], "same_as_fact": first}
                    for fid in rest
                )
            else:
                change = {"effect": "changes"}
                for name in ("observation_id", "text"):
                    if name in action:
                        change[name] = action[name]
                facts.append({"fact_id": first, "kind": "lasting", "adds": "no", "existing": [change], "new": []})
                facts.extend(
                    {
                        "fact_id": fid,
                        "kind": "lasting",
                        "adds": "no",
                        "new": [],
                        "existing": [{**change, "effect": "repeats"}],
                    }
                    for fid in rest
                )
    return {"facts": facts, "deletes": writes.get("deletes", [])}

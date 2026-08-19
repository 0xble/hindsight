"""Refresh operations expose semantic outcome fields in result_metadata (#2605).

Retain operations have carried machine-readable outcome metadata since 0.8.x
(``unit_ids_count`` etc.). These tests pin the refresh-side parity: a completed
refresh_mental_model operation must let a monitoring layer distinguish
"refreshed with real content" from "refreshed empty" by reading
``result_metadata`` alone, without a follow-up content fetch.
"""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from hindsight_api.api.http import OperationResponse, OperationStatusResponse
from hindsight_api.engine.memory_engine import MemoryEngine
from hindsight_api.worker.exceptions import RetryTaskAt

# The reflect agent's fallback answer when the LLM returns nothing usable
# (hindsight_api/engine/reflect/agent.py). Non-empty, so it survives the
# empty-content guard in refresh_mental_model and completes wire-successful —
# exactly the case populated_content must expose.
NO_ANSWER_STUB = "No answer provided."


@pytest.fixture
async def bank_with_model(memory: MemoryEngine, request_context):
    """Bank with one mental model, unique per test for xdist safety."""
    bank_id = f"test-refresh-meta-{uuid.uuid4().hex[:8]}"
    await memory.get_bank_profile(bank_id, request_context=request_context)
    mm = await memory.create_mental_model(
        bank_id=bank_id,
        name="Outcome Meta Model",
        source_query="What outcome fields does refresh expose?",
        content="Original content",
        request_context=request_context,
    )
    yield memory, bank_id, mm
    await memory.delete_bank(bank_id, request_context=request_context)


def _fake_refreshed(content: str, based_on: dict) -> dict:
    """Shape of refresh_mental_model's return value as consumed by the handler."""
    return {
        "content": content,
        "reflect_response": {"text": content, "based_on": based_on, "mental_models": []},
        "source_query": "What outcome fields does refresh expose?",
    }


async def _submit_with_fake_refresh(memory, monkeypatch, bank_id, mm, request_context, refreshed):
    """Submit an async refresh whose reflect outcome is stubbed to `refreshed`.

    The patch must land before submission: the test task backend executes the
    queued task synchronously on submit, so this exercises the real path
    (execute_task -> _handle_refresh_mental_model -> metadata write).
    """

    async def fake_refresh(bank_id, mental_model_id, *, request_context):
        return refreshed

    monkeypatch.setattr(memory, "refresh_mental_model", fake_refresh)
    result = await memory.submit_async_refresh_mental_model(
        bank_id=bank_id,
        mental_model_id=mm["id"],
        request_context=request_context,
    )
    await asyncio.sleep(0.1)
    return result["operation_id"]


@pytest.mark.asyncio
async def test_completed_refresh_enriches_result_metadata(bank_with_model, request_context, monkeypatch):
    """A completed refresh writes content_len / populated_content / based_on_counts."""
    memory, bank_id, mm = bank_with_model
    content = "x" * 120
    based_on = {
        "world": [{"id": "f1"}, {"id": "f2"}, {"id": "f3"}],
        "mental-models": [{"id": "m1"}],
    }

    operation_id = await _submit_with_fake_refresh(
        memory, monkeypatch, bank_id, mm, request_context, _fake_refreshed(content, based_on)
    )

    status = await memory.get_operation_status(
        bank_id=bank_id, operation_id=operation_id, request_context=request_context
    )
    assert status["status"] == "completed"
    meta = status["result_metadata"]

    # Submit-time keys are merged with, not replaced by, the outcome fields:
    # existing consumers join on mental_model_id/name.
    assert meta["mental_model_id"] == mm["id"]
    assert meta["name"] == "Outcome Meta Model"

    assert meta["content_len"] == 120
    assert meta["populated_content"] is True
    assert meta["based_on_counts"] == {"world": 3, "mental-models": 1}


@pytest.mark.asyncio
async def test_no_answer_stub_reads_as_unpopulated(bank_with_model, request_context, monkeypatch):
    """The historical 19-char stub completes wire-successful but must not read as populated."""
    memory, bank_id, mm = bank_with_model

    operation_id = await _submit_with_fake_refresh(
        memory, monkeypatch, bank_id, mm, request_context, _fake_refreshed(NO_ANSWER_STUB, {})
    )

    status = await memory.get_operation_status(
        bank_id=bank_id, operation_id=operation_id, request_context=request_context
    )
    assert status["status"] == "completed"
    meta = status["result_metadata"]

    assert meta["content_len"] == len(NO_ANSWER_STUB)
    assert meta["populated_content"] is False
    assert meta["based_on_counts"] == {}


# ---------------------------------------------------------------------------
# What the refresh did with the document (#3274)
# ---------------------------------------------------------------------------
#
# ``result_metadata`` is the only per-refresh record kept indefinitely
# (operation_retention_days defaults to 0 = never prune). Before these fields
# existed it could not say what a refresh did: a full rewrite, a delta edit, a
# run that found nothing to change and a delta that emitted no operations all
# wrote {delta_ops_applied: 0, delta_ops_skipped: 0}, and a preserved document
# reports the length of the content it preserved — so content_len /
# populated_content read identically too. Everything finer lived only in
# ``mental_models.reflect_response``, which the next refresh overwrites.
#
# These drive the real refresh pipeline (only the LLM boundary is stubbed) and
# assert on the persisted operation row.


def _patch_reflect(monkeypatch, memory: MemoryEngine, *, text: str, facts: list[dict] | None = None) -> None:
    """Stub the agentic loop with a canned candidate + evidence set."""
    from hindsight_api.engine.response_models import ReflectResult

    async def fake_reflect_async(**kwargs):
        return ReflectResult.model_validate(
            {
                "text": text,
                "based_on": {
                    "observation": facts or [],
                    "world": [],
                    "experience": [],
                    "mental-models": [],
                    "directives": [],
                },
            }
        )

    monkeypatch.setattr(memory, "reflect_async", fake_reflect_async)


def _patch_delta_llm(monkeypatch, memory: MemoryEngine, *, returns) -> None:
    """Stub the structured-delta call. ``returns`` is a list of op dicts, or an exception to raise."""
    from hindsight_api.engine.reflect.delta_ops import DeltaOperationList

    async def fake_call(*, messages, **kwargs):
        if isinstance(returns, Exception):
            raise returns
        return DeltaOperationList.model_validate({"operations": returns})

    monkeypatch.setattr(memory._reflect_llm_config, "call", fake_call)


@dataclass
class _RefreshOperationViews:
    """One refresh operation as both API surfaces report it.

    The engine returns plain dicts and the HTTP layer feeds them to the response
    models by keyword, so an engine key that does not match a model field name is
    dropped silently. Building the models here is what pins the two together — the
    typed views are what a client actually receives.
    """

    listed: dict[str, Any]
    status: dict[str, Any]
    listed_model: OperationResponse
    status_model: OperationStatusResponse


async def _refresh_operation_views(memory, bank_id, request_context) -> _RefreshOperationViews:
    """The bank's single refresh operation, as both API surfaces report it."""
    listed = await memory.list_operations(bank_id, task_type="refresh_mental_model", request_context=request_context)
    assert listed["total"] == 1, listed
    row = listed["operations"][0]
    status = await memory.get_operation_status(bank_id=bank_id, operation_id=row["id"], request_context=request_context)
    return _RefreshOperationViews(
        listed=row,
        status=status,
        listed_model=OperationResponse(**row),
        status_model=OperationStatusResponse(**status),
    )


@pytest.fixture
async def delta_bank(memory: MemoryEngine, request_context):
    """Bank with one delta-mode mental model that already has a baseline document."""
    bank_id = f"test-refresh-outcome-{uuid.uuid4().hex[:8]}"
    await memory.get_bank_profile(bank_id, request_context=request_context)
    mm = await memory.create_mental_model(
        bank_id=bank_id,
        name="Team Info",
        source_query="Tell me about the team",
        content="# Team\n\nAlice is the lead.\n",
        trigger={"mode": "delta"},
        request_context=request_context,
    )
    yield memory, bank_id, mm
    await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
async def test_preserved_and_rewritten_differ_only_by_outcome(memory: MemoryEngine, request_context, monkeypatch):
    """The two cases the pre-#3274 metadata could not tell apart.

    A delta refresh that found no new facts preserves the document; a full-mode
    refresh rewrites it. Both emit zero delta operations and both report a
    populated document of the same length, so ``outcome`` is the only field that
    separates "nothing changed" from "the whole document was rewritten".
    """
    document = "# Team\n\nAlice is the lead.\n"
    # A genuine rewrite of the same length, so content_len cannot tell the two
    # apart either — same-length-but-different is the point, since identical text
    # would (correctly) report content_unchanged instead.
    rewrite = "# Team\n\nAlice is the boss.\n"
    assert len(rewrite) == len(document)
    metadata: dict[str, dict] = {}

    for mode in ("delta", "full"):
        bank_id = f"test-refresh-outcome-{mode}-{uuid.uuid4().hex[:8]}"
        await memory.get_bank_profile(bank_id, request_context=request_context)
        mm = await memory.create_mental_model(
            bank_id=bank_id,
            name="Team Info",
            source_query="Tell me about the team",
            content=document,
            trigger={"mode": mode},
            request_context=request_context,
        )
        # No facts: delta short-circuits to "nothing to change", while full mode
        # writes its candidate regardless. Same length, so content_len matches.
        _patch_reflect(monkeypatch, memory, text=rewrite, facts=[])
        _patch_delta_llm(monkeypatch, memory, returns=[])

        await memory.submit_async_refresh_mental_model(
            bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context
        )
        await asyncio.sleep(0.1)
        views = await _refresh_operation_views(memory, bank_id, request_context)
        metadata[mode] = views.status["result_metadata"]
        await memory.delete_bank(bank_id, request_context=request_context)

    preserved, rewritten = metadata["delta"], metadata["full"]

    # Everything the old metadata carried is identical between the two...
    for field_name in ("content_len", "populated_content", "delta_ops_applied", "delta_ops_skipped"):
        assert preserved[field_name] == rewritten[field_name], field_name

    # ...and only the outcome tells them apart.
    assert preserved["outcome"] == "content_preserved_no_new_facts"
    assert rewritten["outcome"] == "content_written"
    assert "failure_reason" not in preserved
    assert "failure_reason" not in rewritten


@pytest.mark.asyncio
async def test_delta_edit_records_content_written(delta_bank, request_context, monkeypatch):
    """A delta refresh that lands operations is a write, and says so."""
    memory, bank_id, mm = delta_bank

    _patch_reflect(
        monkeypatch,
        memory,
        text="# Team\n\nNarrow candidate.\n",
        facts=[{"id": "obs-new", "text": "Bob joined", "type": "observation", "context": None}],
    )
    _patch_delta_llm(
        monkeypatch,
        memory,
        returns=[
            {
                "op": "append_block",
                "section_id": "team",
                "block": {"type": "paragraph", "text": "Bob joined the team."},
            }
        ],
    )

    await memory.submit_async_refresh_mental_model(
        bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context
    )
    await asyncio.sleep(0.1)

    views = await _refresh_operation_views(memory, bank_id, request_context)
    assert views.status["result_metadata"]["outcome"] == "content_written"
    assert views.status["result_metadata"]["delta_ops_applied"] == 1
    # Typed on the response models, not only inside the untyped result_metadata blob.
    assert views.status_model.details is not None
    assert views.status_model.details.operation_type == "refresh_mental_model"
    assert views.status_model.details.outcome == "content_written"
    assert views.status_model.details.failure_reason is None
    assert views.listed_model.details is not None
    assert views.listed_model.details.outcome == "content_written"


@pytest.mark.asyncio
async def test_all_ops_rejected_records_failure_and_reason(delta_bank, request_context, monkeypatch):
    """A refresh that refused to write records why, structurally.

    Before this the reason survived only as prose inside ``error_message`` and in
    the model's ``reflect_response``, which the next refresh overwrites — and the
    success-path writer never ran at all, because the refresh raised first.
    """
    memory, bank_id, mm = delta_bank

    _patch_reflect(
        monkeypatch,
        memory,
        text="# Team\n\nNarrow candidate.\n",
        facts=[{"id": "obs-new", "text": "Bob joined", "type": "observation", "context": None}],
    )
    # Every op targets a section that does not exist, so all are rejected and the
    # document is left untouched — a failure, not a clean refresh.
    _patch_delta_llm(
        monkeypatch,
        memory,
        returns=[
            {
                "op": "append_block",
                "section_id": "does-not-exist",
                "block": {"type": "paragraph", "text": "Bob joined the team."},
            }
        ],
    )

    # A failed refresh is retryable, so the task layer re-raises it as RetryTaskAt.
    # The metadata below is written before that, on the attempt that failed.
    with pytest.raises(RetryTaskAt):
        await memory.submit_async_refresh_mental_model(
            bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context
        )

    views = await _refresh_operation_views(memory, bank_id, request_context)
    meta = views.status["result_metadata"]
    assert meta["outcome"] == "refresh_failed_delta_not_applied"
    assert meta["failure_reason"] == "delta_ops_all_skipped"
    # Submit-time keys survive the merge.
    assert meta["mental_model_id"] == mm["id"]
    assert views.status_model.details is not None
    assert views.status_model.details.outcome == "refresh_failed_delta_not_applied"
    assert views.status_model.details.failure_reason == "delta_ops_all_skipped"
    assert views.listed_model.details is not None
    assert views.listed_model.details.failure_reason == "delta_ops_all_skipped"


@pytest.mark.asyncio
async def test_empty_candidate_records_its_own_failure(delta_bank, request_context, monkeypatch):
    """An upstream LLM failure is a distinct outcome from a rejected delta."""
    memory, bank_id, mm = delta_bank

    _patch_reflect(
        monkeypatch,
        memory,
        text="",
        facts=[{"id": "obs-new", "text": "Bob joined", "type": "observation", "context": None}],
    )
    _patch_delta_llm(monkeypatch, memory, returns=RuntimeError("simulated invalid JSON from provider"))

    # A failed refresh is retryable, so the task layer re-raises it as RetryTaskAt.
    # The metadata below is written before that, on the attempt that failed.
    with pytest.raises(RetryTaskAt):
        await memory.submit_async_refresh_mental_model(
            bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context
        )

    meta = (await _refresh_operation_views(memory, bank_id, request_context)).status["result_metadata"]
    assert meta["outcome"] == "refresh_failed_empty_candidate"
    assert meta["failure_reason"] == "empty_candidate"


@pytest.mark.asyncio
async def test_delta_op_call_failure_is_distinct_from_rejected_ops(delta_bank, request_context, monkeypatch):
    """An op call that never returned usable JSON is a different reason from ops that were rejected."""
    memory, bank_id, mm = delta_bank

    _patch_reflect(
        monkeypatch,
        memory,
        # Non-empty, so the empty-candidate guard does not fire first and this
        # reaches the delta-not-applied branch instead.
        text="# Team\n\nNarrow candidate.\n",
        facts=[{"id": "obs-new", "text": "Bob joined", "type": "observation", "context": None}],
    )
    _patch_delta_llm(monkeypatch, memory, returns=RuntimeError("simulated invalid JSON from provider"))

    with pytest.raises(RetryTaskAt):
        await memory.submit_async_refresh_mental_model(
            bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context
        )

    views = await _refresh_operation_views(memory, bank_id, request_context)
    assert views.status_model.details is not None
    assert views.status_model.details.outcome == "refresh_failed_delta_not_applied"
    assert views.status_model.details.failure_reason == "delta_ops_failed"


@pytest.mark.asyncio
async def test_unreadable_baseline_records_its_own_reason(delta_bank, request_context, monkeypatch):
    """Delta with no readable baseline fails for a reason of its own, not the generic one."""
    memory, bank_id, mm = delta_bank

    from hindsight_api.engine.reflect import structured_doc

    def unparseable(_markdown: str):
        raise ValueError("simulated unparseable markdown")

    monkeypatch.setattr(structured_doc, "parse_markdown", unparseable)
    _patch_reflect(
        monkeypatch,
        memory,
        text="# Team\n\nNarrow candidate.\n",
        facts=[{"id": "obs-new", "text": "Bob joined", "type": "observation", "context": None}],
    )
    _patch_delta_llm(monkeypatch, memory, returns=[])

    with pytest.raises(RetryTaskAt):
        await memory.submit_async_refresh_mental_model(
            bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context
        )

    views = await _refresh_operation_views(memory, bank_id, request_context)
    assert views.status_model.details is not None
    assert views.status_model.details.outcome == "refresh_failed_delta_not_applied"
    assert views.status_model.details.failure_reason == "structured_doc_unreadable"


@pytest.mark.asyncio
async def test_structured_output_failure_records_the_persist_path_outcome(
    memory: MemoryEngine, request_context, monkeypatch
):
    """The one outcome the executor cannot produce still reaches the operation.

    Structured-output extraction runs in the persist path, after the executor has
    already settled on ``content_written`` — which is why the operation vocabulary
    is a superset of the executor's.
    """
    import types

    from hindsight_api.engine.reflect import agent as reflect_agent

    bank_id = f"test-refresh-outcome-so-{uuid.uuid4().hex[:8]}"
    await memory.get_bank_profile(bank_id, request_context=request_context)
    mm = await memory.create_mental_model(
        bank_id=bank_id,
        name="Doc",
        source_query="doc?",
        content="# Doc\n\nOriginal.",
        trigger={"mode": "full", "response_schema": {"type": "object", "properties": {"summary": {"type": "string"}}}},
        request_context=request_context,
    )

    _patch_reflect(monkeypatch, memory, text="# Doc\n\nBrand new content.")

    async def extraction_yields_nothing(answer, response_schema, llm_config, reflect_id, max_tokens=None):
        return types.SimpleNamespace(
            structured_output=None, input_tokens=0, output_tokens=0, cached_tokens=0, thoughts_tokens=0
        )

    monkeypatch.setattr(reflect_agent, "_generate_structured_output", extraction_yields_nothing)

    with pytest.raises(RetryTaskAt):
        await memory.submit_async_refresh_mental_model(
            bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context
        )

    views = await _refresh_operation_views(memory, bank_id, request_context)
    assert views.status_model.details is not None
    assert views.status_model.details.outcome == "refresh_failed_structured_output"
    assert views.status_model.details.failure_reason == "structured_output_failed"

    await memory.delete_bank(bank_id, request_context=request_context)


def test_operation_outcome_is_a_superset_of_executor_outcome():
    """The two outcome vocabularies must not drift apart.

    ``RefreshOperationOutcome`` is spelled out rather than unioned (a union of
    Literals renders as an anyOf of two enums in the OpenAPI schema), so nothing
    but this test stops a value added to one from being forgotten in the other.
    """
    from typing import get_args

    from hindsight_api.engine.mental_model_refresh import RefreshOperationOutcome, RefreshOutcome

    executor = set(get_args(RefreshOutcome))
    operation = set(get_args(RefreshOperationOutcome))
    assert executor <= operation, f"executor outcomes missing from the operation vocabulary: {executor - operation}"
    # The persist path is the only source of the extra values; if that changes,
    # the comment on RefreshOperationOutcome needs to change with it.
    assert operation - executor == {"refresh_failed_structured_output"}


def test_non_refresh_operations_report_no_refresh_details():
    """``details`` is keyed by operation type, so another type's row must not carry a refresh shape."""
    from hindsight_api.engine.memory_engine import _operation_details

    assert _operation_details("batch_retain", {"outcome": "content_written"}) is None
    # A refresh that has not finished has nothing to report yet.
    assert _operation_details("refresh_mental_model", {"mental_model_id": "mm-1"}) is None


@pytest.mark.asyncio
async def test_delta_emitting_no_ops_reads_as_unchanged(delta_bank, request_context, monkeypatch):
    """A delta that changes nothing must not report itself as a rewrite.

    The model is shown the new facts and emits zero operations — it read them and
    concluded the document already covers them. That lands in the *applied* path
    (there are no rejected ops), so the document is re-rendered and persisted
    byte-identically, and the outcome used to read `content_written` — the same
    value a genuine rewrite produces.

    `content_preserved_no_new_facts` does not cover this: that fires only when the
    delta window was empty, and here retrieval returned facts.
    """
    memory, bank_id, mm = delta_bank
    stored = (
        await memory.get_mental_model(bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context)
    )["content"]

    _patch_reflect(
        monkeypatch,
        memory,
        text="# Team\n\nA candidate the model decides adds nothing.\n",
        facts=[{"id": "obs-new", "text": "Bob joined", "type": "observation", "context": None}],
    )
    _patch_delta_llm(monkeypatch, memory, returns=[])

    await memory.submit_async_refresh_mental_model(
        bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context
    )
    await asyncio.sleep(0.1)

    views = await _refresh_operation_views(memory, bank_id, request_context)
    assert views.status_model.details is not None
    assert views.status_model.details.outcome == "content_unchanged"

    # …and the claim is true: the stored document really is byte-identical.
    after = await memory.get_mental_model(bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context)
    assert after["content"] == stored

"""Private reads retain the public result without retaining read diagnostics.

Drive the generated APIs shipped in the published Python client over real HTTP.
Public reflect calls on both sides prove diagnostics remain enabled and give
asynchronous writers an observable opportunity to finish. No engine access,
SQL, raw HTTP, disabled workers, or plausible-default LLM responses are used.
"""

from __future__ import annotations

import asyncio
import json
from time import monotonic

import pytest
from hindsight_client_api import ApiClient, Configuration
from hindsight_client_api.api.audit_api import AuditApi
from hindsight_client_api.api.llm_traces_api import LLMTracesApi
from hindsight_client_api.api.memory_api import MemoryApi
from hindsight_client_api.api.monitoring_api import MonitoringApi
from hindsight_client_api.models.llm_request_list_response import LLMRequestListResponse
from hindsight_client_api.models.recall_request import RecallRequest
from hindsight_client_api.models.reflect_request import ReflectRequest

from hindsight_system_tests import reflect_loop
from hindsight_system_tests.payloads import consolidation, extracted, fact

pytestmark = pytest.mark.asyncio

FACT = "The fixture musician plays the bassoon."
ANSWER = "The fixture musician plays the bassoon."
RECALL_QUERY = "Which instrument does the fixture musician play?"
PRIVATE_QUERY = "private-read-story: which instrument does the fixture musician play?"
BEFORE_QUERY = "before-private-story: which instrument does the fixture musician play?"
AFTER_QUERY = "after-private-story: which instrument does the fixture musician play?"


async def _public_trace_is_visible(
    api: LLMTracesApi, bank_id: str, query: str, expected_calls: int
) -> LLMRequestListResponse:
    """Observe every actual public provider call, including its persisted result."""
    assert expected_calls > 0, "the provider must have received the public control"
    deadline = monotonic() + 20
    last_page = "no trace response"
    while monotonic() < deadline:
        traces = await api.list_llm_requests(bank_id=bank_id, operation="reflect", limit=500)
        last_page = traces.model_dump_json()
        assert traces.total == len(traces.items), "the isolated story must inspect every trace"
        control = [entry for entry in traces.items if query in json.dumps(entry.input)]
        assert len(control) <= expected_calls, "public tracing duplicated provider calls"
        assert all(entry.status == "success" for entry in control)
        # Tool-only replies have null output. Their tool name is persisted in
        # llm_info, and retrieved facts persist in the following call's input.
        finished = any("done" in entry.llm_info.get("tool_calls", []) for entry in control)
        persisted_fact = any(FACT in json.dumps(entry.input) for entry in control)
        if len(control) == expected_calls and finished and persisted_fact:
            return traces
        await asyncio.sleep(0.05)
    pytest.fail(f"Public tracing did not persist all {expected_calls} provider calls. Last page: {last_page}")


async def test_private_reads_leave_no_read_history_and_public_writes_resume(
    client, llm, bank_id, settled, hindsight_server
):
    llm.on_step("extract_facts").returns(extracted(fact(FACT, who="fixture musician")))
    llm.on_step("consolidate").returns(consolidation())
    reflect_loop(llm, answer=ANSWER, query="fixture musician bassoon")
    await client.aretain(bank_id=bank_id, content=FACT)
    await settled(bank_id)
    await client.aupdate_bank_config(bank_id=bank_id, audit_log_enabled=True)

    async with ApiClient(Configuration(host=hindsight_server.url)) as transport:
        memory = MemoryApi(transport)
        audit = AuditApi(transport)
        traces_api = LLMTracesApi(transport)
        version = await MonitoringApi(transport).get_version()
        assert version.features.private_reads is True
        assert version.features.llm_trace is True

        public_recall = await client.arecall(bank_id=bank_id, query=RECALL_QUERY)
        assert [result.text for result in public_recall.results] == [FACT + " | Involving: fixture musician"]
        first = await client.areflect(bank_id=bank_id, query=BEFORE_QUERY)
        assert first.text == ANSWER
        await settled(bank_id)
        before_calls = sum(BEFORE_QUERY in call.all_text for call in llm.calls)
        before_traces = await _public_trace_is_visible(traces_api, bank_id, BEFORE_QUERY, before_calls)
        assert before_traces.total == before_calls

        recalled = await memory.private_recall_memories_with_http_info(
            bank_id=bank_id, recall_request=RecallRequest(query=RECALL_QUERY)
        )
        assert recalled.status_code == 200
        recall_headers = {name.lower(): value for name, value in recalled.headers.items()}
        assert recall_headers["x-hindsight-private-read"] == "1"
        assert recall_headers["cache-control"] == "no-store"
        assert recalled.data is not None
        assert recalled.data.model_dump(exclude={"results"}) == public_recall.model_dump(exclude={"results"})
        assert len(recalled.data.results) == len(public_recall.results)
        for private, public in zip(recalled.data.results, public_recall.results, strict=True):
            assert private.model_dump(exclude={"scores": {"final"}}) == public.model_dump(exclude={"scores": {"final"}})
            assert private.scores.final == pytest.approx(public.scores.final, abs=1e-6)

        reflected = await memory.private_reflect_with_http_info(
            bank_id=bank_id, reflect_request=ReflectRequest(query=PRIVATE_QUERY)
        )
        assert reflected.status_code == 200
        reflect_headers = {name.lower(): value for name, value in reflected.headers.items()}
        assert reflect_headers["x-hindsight-private-read"] == "1"
        assert reflect_headers["cache-control"] == "no-store"
        assert reflected.data is not None
        assert reflected.data.model_dump() == first.model_dump()
        await settled(bank_id)

        last = await client.areflect(bank_id=bank_id, query=AFTER_QUERY)
        assert last.model_dump() == first.model_dump()
        await settled(bank_id)
        after_calls = sum(AFTER_QUERY in call.all_text for call in llm.calls)
        after_traces = await _public_trace_is_visible(traces_api, bank_id, AFTER_QUERY, after_calls)
        assert after_traces.total == before_calls + after_calls
        assert all(PRIVATE_QUERY not in entry.model_dump_json() for entry in after_traces.items)
        reflect_audit = await audit.list_audit_logs(bank_id=bank_id, action="reflect", limit=500)
        assert reflect_audit.total == len(reflect_audit.items) == 2
        recall_audit = await audit.list_audit_logs(bank_id=bank_id, action="recall", limit=500)
        assert recall_audit.total == len(recall_audit.items) == 1

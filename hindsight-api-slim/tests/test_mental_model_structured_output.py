"""Tests that a mental model's ``trigger.response_schema`` drives structured output on refresh.

When a pinned mental model carries a ``response_schema`` in its trigger config, each
refresh must forward that schema to the internal reflect call and persist the parsed
``structured_output`` onto the stored ``reflect_response`` payload (alongside the markdown
content). These are deterministic plumbing tests: ``reflect_async`` is monkey-patched, so no
LLM is involved.
"""

import uuid

from hindsight_api import MemoryEngine, RequestContext
from hindsight_api.engine.response_models import ReflectResult

_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def _canned_reflect_result(text: str, structured_output: dict | None = None) -> ReflectResult:
    """Minimal ReflectResult for monkey-patching reflect_async."""
    return ReflectResult.model_validate(
        {
            "text": text,
            "based_on": {
                "observation": [],
                "world": [],
                "experience": [],
                "mental-models": [],
                "directives": [],
            },
            "structured_output": structured_output,
        }
    )


class TestMentalModelStructuredOutput:
    async def test_response_schema_forwarded_and_persisted(
        self, memory: MemoryEngine, request_context: RequestContext, monkeypatch
    ):
        """The trigger schema reaches reflect, and its structured_output is stored."""
        bank_id = f"test-mm-struct-{uuid.uuid4().hex[:8]}"
        await memory.get_bank_profile(bank_id, request_context=request_context)

        mm = await memory.create_mental_model(
            bank_id=bank_id,
            name="Team Info",
            source_query="Tell me about the team",
            content="# Team\n\nOriginal content.",
            trigger={"mode": "full", "response_schema": _SCHEMA},
            request_context=request_context,
        )

        calls: list[dict] = []

        async def fake_reflect_async(**kwargs):
            calls.append(kwargs)
            return _canned_reflect_result("# Team\n\nRegenerated.", structured_output={"summary": "A small team."})

        monkeypatch.setattr(memory, "reflect_async", fake_reflect_async)

        refreshed = await memory.refresh_mental_model(
            bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context
        )

        # The trigger's schema is forwarded verbatim to the internal reflect call.
        assert len(calls) == 1
        assert calls[0]["response_schema"] == _SCHEMA

        # The parsed structured output is persisted on the reflect_response payload.
        assert refreshed is not None
        assert refreshed["reflect_response"]["structured_output"] == {"summary": "A small team."}

    async def test_no_schema_means_no_structured_output(
        self, memory: MemoryEngine, request_context: RequestContext, monkeypatch
    ):
        """Without a trigger schema, reflect gets response_schema=None and nothing is stored."""
        bank_id = f"test-mm-nostruct-{uuid.uuid4().hex[:8]}"
        await memory.get_bank_profile(bank_id, request_context=request_context)

        mm = await memory.create_mental_model(
            bank_id=bank_id,
            name="Team Info",
            source_query="Tell me about the team",
            content="# Team\n\nOriginal content.",
            trigger={"mode": "full"},
            request_context=request_context,
        )

        calls: list[dict] = []

        async def fake_reflect_async(**kwargs):
            calls.append(kwargs)
            return _canned_reflect_result("# Team\n\nRegenerated.")

        monkeypatch.setattr(memory, "reflect_async", fake_reflect_async)

        refreshed = await memory.refresh_mental_model(
            bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context
        )

        assert calls[0]["response_schema"] is None
        assert refreshed is not None
        assert "structured_output" not in refreshed["reflect_response"]

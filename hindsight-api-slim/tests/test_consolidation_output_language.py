"""Consolidation must write observations in the language of their source facts (#3166).

The consolidation prompt is written in English, so before this rule existed a bank
holding Chinese facts could receive English observations — reported in production
against Qwen through an OpenAI-compatible provider, and reproducible in principle on
any multilingual model. Prompt-following is exactly the kind of behaviour MockLLM
cannot simulate, so these drive the real batch call and judge the result; the
deterministic half (which rule text ends up in the prompt for a given config) lives
in ``test_multilingual_bm25.py``.
"""

import uuid
from types import SimpleNamespace

import pytest

from hindsight_api.engine.consolidation.consolidator import _consolidate_batch_with_llm
from hindsight_api.engine.response_models import MemoryFact
from tests.llm_judge import assert_meets_criteria

pytestmark = pytest.mark.hs_llm_core


def _has_cjk(text: str) -> bool:
    """True when the text contains at least one CJK ideograph (U+4E00–U+9FFF)."""
    return any("一" <= char <= "鿿" for char in text)


def _batch_config(llm_output_language: str | None) -> SimpleNamespace:
    """Minimal config object accepted by `_consolidate_batch_with_llm`."""
    return SimpleNamespace(
        llm_output_language=llm_output_language,
        observations_mission=None,
        llm_strict_schema_consolidation=False,
        llm_supports_max_items=False,
        consolidation_max_attempts=2,
        consolidation_llm_max_retries=None,
        consolidation_max_completion_tokens=None,
    )


# The two Chinese facts from the issue's production evidence, which consolidation
# turned into "The user often takes their pet to the park..." / "The user is
# currently preparing for a promotion review."
_CHINESE_FACTS = [
    {"id": str(uuid.uuid4()), "text": "用户周末经常带宠物去公园散步。"},
    {"id": str(uuid.uuid4()), "text": "用户最近在准备晋升答辩。"},
]


@pytest.mark.asyncio
async def test_creates_stay_in_source_language(llm_config):
    """Chinese source facts, no configured output language → Chinese observations."""
    result = await _consolidate_batch_with_llm(
        llm_config=llm_config,
        memories=_CHINESE_FACTS,
        union_observations=[],
        union_source_facts={},
        config=_batch_config(None),
    )

    assert result.creates, "Chinese facts with no existing observations should produce creates"
    for create in result.creates:
        assert _has_cjk(create.text), f"Observation was translated out of Chinese: {create.text!r}"

    await assert_meets_criteria(
        response="\n".join(create.text for create in result.creates),
        criteria="Every line is written in Chinese, not English or any other language.",
        context=(
            "Consolidation was given Chinese source facts about a user walking their pet in "
            "the park on weekends and preparing for a promotion review, with no configured "
            "output language. Proper nouns and technical terms may legitimately stay in their "
            "original script."
        ),
        msg="Observations created from Chinese facts must stay in Chinese",
    )


@pytest.mark.asyncio
async def test_updates_stay_in_source_language(llm_config):
    """An existing English observation updated by a Chinese fact is rewritten in Chinese.

    This is the self-healing half of the fix: banks that already drifted to English
    converge back to the source language as new facts arrive.
    """
    fact_id = str(uuid.uuid4())
    source_fact = MemoryFact(
        id=fact_id,
        text="用户周末经常带宠物去公园散步。",
        fact_type="world",
    )
    observation = MemoryFact(
        id=str(uuid.uuid4()),
        text="The user often takes their pet to the park for walks on weekends.",
        fact_type="observation",
        source_fact_ids=[fact_id],
    )
    new_fact = {
        "id": str(uuid.uuid4()),
        "text": "用户周末带猫豆豆去深圳湾公园散步。",
    }

    result = await _consolidate_batch_with_llm(
        llm_config=llm_config,
        memories=[new_fact],
        union_observations=[observation],
        union_source_facts={fact_id: source_fact},
        config=_batch_config(None),
    )

    # Whether the model merges into the existing observation or records a sibling is
    # its call — the PROCESSING RULES lean toward UPDATE but both routings are valid,
    # and asserting one would make this a flaky test of merge behaviour instead of
    # language. Every text it emits, either way, must be in the new fact's language.
    emitted = [action.text for action in [*result.updates, *result.creates]]
    assert emitted, "The new fact should produce either an update or a create"
    for text in emitted:
        assert _has_cjk(text), f"Observation was left in English: {text!r}"

    await assert_meets_criteria(
        response="\n".join(emitted),
        criteria=(
            "Every line is written in Chinese throughout — not English, and not a mix of a "
            "Chinese clause with an English clause."
        ),
        context=(
            "An existing observation was stored in English ('The user often takes their pet to "
            "the park for walks on weekends.') and a new Chinese fact about walking the cat "
            "Doudou at Shenzhen Bay Park arrived. No output language is configured, so whatever "
            "observation text this produces must follow the new fact's language. Place and pet "
            "names may stay in their original script."
        ),
        msg="Chinese facts must not produce English observation text, even against an English observation",
    )


@pytest.mark.asyncio
async def test_configured_output_language_overrides_source_language(llm_config):
    """An explicit output language still wins over the source facts' language."""
    result = await _consolidate_batch_with_llm(
        llm_config=llm_config,
        memories=_CHINESE_FACTS,
        union_observations=[],
        union_source_facts={},
        config=_batch_config("English"),
    )

    assert result.creates, "Chinese facts with no existing observations should produce creates"
    await assert_meets_criteria(
        response="\n".join(create.text for create in result.creates),
        criteria="Every line is written in English, even though the source material was Chinese.",
        context=(
            "Consolidation was given Chinese source facts about a user walking their pet and "
            "preparing for a promotion review, with the output language configured to English. "
            "Personal or place names transliterated or left in Chinese are acceptable; the "
            "sentences themselves must be English."
        ),
        msg="HINDSIGHT_API_LLM_OUTPUT_LANGUAGE must override the source language",
    )

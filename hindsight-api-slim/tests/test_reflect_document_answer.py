"""The reflect agent can answer with a document instead of markdown.

A document that is generated as markdown has to be read back to work out its
structure, and reading markdown back is where #3361 destroyed tables. In
document mode the model states the structure and the markdown is rendered from
it, so nothing the model writes is ever parsed to find out what it meant.

These are pure-Python tests of the schema and the parsing of the tool call —
the mechanics. Whether a real model fills the shape correctly is covered by the
``hs_llm_core`` refresh evals in ``test_mental_model_delta.py``.
"""

from __future__ import annotations

from hindsight_api.engine.reflect.structured_doc import (
    document_from_sections,
    render_document,
)
from hindsight_api.engine.reflect.tools_schema import get_reflect_tools


def _done_tool(**kwargs) -> dict:
    return next(t for t in get_reflect_tools(**kwargs) if t["function"]["name"] == "done")


class TestDoneToolSchema:
    def test_markdown_mode_is_the_default(self):
        params = _done_tool()["function"]["parameters"]
        assert "answer" in params["properties"]
        assert "document" not in params["properties"]

    def test_document_mode_replaces_the_answer_field(self):
        params = _done_tool(answer_as_document=True)["function"]["parameters"]
        assert "document" in params["properties"]
        assert "answer" not in params["properties"], "the model must not have a markdown escape hatch"
        assert params["required"] == ["document"]

    def test_document_mode_keeps_the_supporting_id_fields(self):
        params = _done_tool(answer_as_document=True)["function"]["parameters"]
        for field in ("memory_ids", "mental_model_ids", "observation_ids"):
            assert field in params["properties"]

    def test_document_mode_composes_with_directives(self):
        params = _done_tool(directive_rules=["Be concise"], answer_as_document=True)["function"]["parameters"]
        assert set(params["required"]) == {"document", "directive_compliance"}

    def test_schema_has_no_union_types(self):
        """A tool schema goes to the provider verbatim, and Gemini rejects ``oneOf``."""
        rendered = repr(_done_tool(answer_as_document=True))
        for keyword in ("oneOf", "anyOf", "allOf", "discriminator"):
            assert keyword not in rendered

    def test_section_shape_is_heading_level_blocks(self):
        document = _done_tool(answer_as_document=True)["function"]["parameters"]["properties"]["document"]
        section = document["properties"]["sections"]["items"]
        assert set(section["required"]) == {"heading", "level", "blocks"}
        assert section["properties"]["blocks"]["items"]["type"] == "string"


class TestDocumentFromSections:
    def test_renders_headings_and_blocks(self):
        doc = document_from_sections(
            {"sections": [{"heading": "Ops", "level": 2, "blocks": ["Intro.", "- one\n- two"]}]}
        )
        assert render_document(doc) == "## Ops\n\nIntro.\n\n- one\n- two\n"

    def test_ids_are_assigned(self):
        doc = document_from_sections({"sections": [{"heading": "Ops", "level": 2, "blocks": ["Intro."]}]})
        assert doc.sections[0].id == "ops"
        assert doc.sections[0].blocks[0].id

    def test_table_survives_verbatim(self):
        table = "| a | b |\n| --- | --- |\n| 1 | 2 |"
        doc = document_from_sections({"sections": [{"heading": "T", "level": 2, "blocks": [table]}]})
        assert doc.sections[0].blocks[0].text == table

    def test_a_block_holding_several_fragments_is_split(self):
        """The model sometimes packs a whole section into one string."""
        doc = document_from_sections({"sections": [{"heading": "T", "level": 2, "blocks": ["One para.\n\nTwo para."]}]})
        assert [b.text for b in doc.sections[0].blocks] == ["One para.", "Two para."]

    def test_blank_lines_inside_a_fence_do_not_split(self):
        fence = "```python\ndef f():\n\n    return 1\n```"
        doc = document_from_sections({"sections": [{"heading": "T", "level": 2, "blocks": [fence]}]})
        assert [b.text for b in doc.sections[0].blocks] == [fence]

    def test_heading_hashes_are_stripped(self):
        doc = document_from_sections({"sections": [{"heading": "## Ops", "level": 2, "blocks": ["x"]}]})
        assert doc.sections[0].heading == "Ops"
        assert render_document(doc).startswith("## Ops\n")

    def test_empty_heading_renders_without_one(self):
        doc = document_from_sections({"sections": [{"heading": "", "level": 2, "blocks": ["lead in"]}]})
        assert doc.sections[0].id == "preamble"
        assert render_document(doc) == "lead in\n"

    def test_out_of_range_level_is_clamped(self):
        doc = document_from_sections({"sections": [{"heading": "T", "level": 99, "blocks": ["x"]}]})
        assert doc.sections[0].level == 6

    def test_missing_level_defaults_to_two(self):
        doc = document_from_sections({"sections": [{"heading": "T", "blocks": ["x"]}]})
        assert doc.sections[0].level == 2

    def test_duplicate_headings_get_unique_ids(self):
        doc = document_from_sections(
            {"sections": [{"heading": "T", "level": 2, "blocks": ["a"]}, {"heading": "T", "level": 2, "blocks": ["b"]}]}
        )
        assert [s.id for s in doc.sections] == ["t", "t-2"]

    def test_empty_and_malformed_entries_are_dropped(self):
        doc = document_from_sections(
            {"sections": [{"heading": "", "level": 2, "blocks": ["   ", None]}, "not a section", {}]}
        )
        assert doc.sections == []

    def test_empty_payload_is_an_empty_document(self):
        assert document_from_sections({}).sections == []
        assert render_document(document_from_sections({"sections": []})) == ""

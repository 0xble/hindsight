"""Only an all-empty parser chain has a terminal no-text outcome."""

from types import SimpleNamespace

import pytest

from hindsight_api.engine.memory_engine import _file_convert_failure_metadata, _operation_details
from hindsight_api.engine.operation_details import FileConvertRetainOperationDetails
from hindsight_api.engine.parsers import FileParser, FileParserRegistry, NoExtractableContentError
from hindsight_api.engine.parsers.markitdown import MarkitdownParser


class StubParser(FileParser):
    def __init__(self, name, outcome):
        self._name = name
        self.outcome = outcome

    def name(self):
        return self._name

    def supports(self, filename, content_type=None):
        return True

    async def convert(self, file_data, filename):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize("outcomes", [("", " \n"), (NoExtractableContentError("blank"), "")])
async def test_all_empty_chain_is_structured(outcomes):
    registry = FileParserRegistry()
    registry.register(StubParser("first", outcomes[0]))
    registry.register(StubParser("second", outcomes[1]))
    with pytest.raises(NoExtractableContentError) as caught:
        await registry.convert_with_fallback(["first", "second"], b"pdf", "scan.pdf")
    wrapped = RuntimeError("file failed")
    wrapped.__cause__ = caught.value
    metadata = _file_convert_failure_metadata(wrapped)
    assert metadata == {
        "failure_class": "no_extractable_text",
        "failure_reason": "empty_content",
        "parsers": ["first", "second"],
    }
    assert _operation_details("file_convert_retain", metadata) == {
        "operation_type": "file_convert_retain",
        **metadata,
    }
    assert FileConvertRetainOperationDetails(**metadata).model_dump(mode="json", exclude_none=True) == {
        "operation_type": "file_convert_retain",
        **metadata,
    }


@pytest.mark.parametrize("outcomes", [(TimeoutError("timeout"), ""), ("", ConnectionError("network"))])
async def test_mixed_chain_is_not_no_text(outcomes):
    registry = FileParserRegistry()
    registry.register(StubParser("first", outcomes[0]))
    registry.register(StubParser("second", outcomes[1]))
    with pytest.raises((TimeoutError, ConnectionError)) as caught:
        await registry.convert_with_fallback(["first", "second"], b"pdf", "scan.pdf")
    assert _file_convert_failure_metadata(caught.value) == {}


async def test_transient_error_has_no_structured_details():
    registry = FileParserRegistry()
    registry.register(StubParser("remote", TimeoutError("provider timed out")))
    with pytest.raises(TimeoutError) as caught:
        await registry.convert_with_fallback(["remote"], b"pdf", "scan.pdf")
    assert _file_convert_failure_metadata(caught.value) == {}
    assert _operation_details("file_convert_retain", {}) is None


def test_markitdown_no_text_is_typed(monkeypatch):
    parser = MarkitdownParser.__new__(MarkitdownParser)
    parser._ocr_enabled = False
    parser._markitdown = SimpleNamespace(convert=lambda *args, **kwargs: SimpleNamespace(text_content=" \n"))
    with pytest.raises(NoExtractableContentError):
        parser._convert_sync(b"pdf", "scan.pdf")


def test_malformed_details_rejected():
    assert (
        _operation_details(
            "file_convert_retain",
            {"failure_class": "no_extractable_text", "failure_reason": "empty_content"},
        )
        is None
    )
    assert (
        _operation_details(
            "file_convert_retain",
            {"failure_class": "low_quality_ocr", "failure_reason": "empty_content", "parsers": ["iris"]},
        )
        is None
    )

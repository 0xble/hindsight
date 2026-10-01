"""Cloud parser success-without-text participates in the fork's typed chain contract."""

import pytest

from hindsight_api.engine.memory_engine import _file_convert_failure_metadata
from hindsight_api.engine.parsers import (
    FileParserRegistry,
    IrisParser,
    LlamaParseParser,
    NoExtractableContentError,
    iris,
)
from tests.aiohttp_stub import stub_server
from tests.test_iris_parser_stub import _Upstream as IrisUpstream
from tests.test_llama_parse_parser import _Reply, _serve
from tests.test_llama_parse_parser import _Upstream as LlamaUpstream
from tests.test_no_extractable_text import StubParser


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [None, "", " \n"])
@pytest.mark.parametrize("transient_first", [None, True, False])
async def test_cloud_parser_chain_classifies_only_all_empty_results(monkeypatch, text, transient_first):
    iris_upstream = IrisUpstream(extraction_statuses=[{"ready": True, "data": {"success": True, "text": text}}])
    llama_upstream = LlamaUpstream(
        upload=_Reply(json={"id": "empty-job"}),
        gets=[_Reply(json={"status": "SUCCESS"}), _Reply(json={"markdown": text})],
    )
    registry = FileParserRegistry()
    registry.register(IrisParser(token="tok", org_id="org-1", poll_interval=0.0))
    registry.register(LlamaParseParser(api_key="llx-test", poll_interval=0.0))
    registry.register(StubParser("empty", ""))
    registry.register(StubParser("transient", TimeoutError("provider timed out")))
    chain = ["iris", "llama_parse", "empty"]
    if transient_first is not None:
        chain.insert(0 if transient_first else len(chain), "transient")
    expected = NoExtractableContentError if transient_first is None else TimeoutError

    async with stub_server(iris_upstream.handler) as base_url, _serve(monkeypatch, llama_upstream):
        monkeypatch.setattr(iris, "_IRIS_BASE_URL", base_url)
        with pytest.raises(expected) as caught:
            await registry.convert_with_fallback(chain, b"pdf", "doc.pdf")

    metadata = _file_convert_failure_metadata(caught.value)
    if transient_first is None:
        assert metadata == {
            "failure_class": "no_extractable_text",
            "failure_reason": "empty_content",
            "parsers": chain,
        }
    else:
        assert metadata == {}
    assert iris_upstream.polls == 1
    assert llama_upstream.get_count == 2

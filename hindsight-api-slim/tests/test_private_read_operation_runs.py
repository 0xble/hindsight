"""Private-read coverage for the diagnostic owners introduced by v1.2.0."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hindsight_api import operation_runs as runs
from hindsight_api.extensions import model_call
from hindsight_api.private_reads import private_read_scope


def test_private_operation_preserves_inherited_public_run_and_sink(monkeypatch):
    recorded = []
    monkeypatch.setattr(runs, "_sink", recorded.append)
    with runs.timed_operation("recall", "public-bank"):
        parent = runs._current.get()
        assert parent is not None
        public_id = runs.current_operation_id()
        runs.add_model_call(0.01, 3, 5)
        before = vars(parent).copy()
        with private_read_scope():
            assert runs.current_operation_id() is None
            runs.add_model_call(9, 100, 200)
            runs.add_phase("private-phase", 9)
            with runs.timed_operation("reflect", "private-bank"):
                assert runs._current.get() is None
                assert runs.current_operation_id() is None
                runs.add_model_call(9, 100, 200)
            assert runs._current.get() is parent
            assert vars(parent) == before
        assert runs.current_operation_id() == public_id
    assert len(recorded) == 1
    assert recorded[0].bank_id == "public-bank"
    assert recorded[0].model_calls == 1
    assert recorded[0].input_tokens == 3
    assert recorded[0].output_tokens == 5
    assert "private-phase" not in recorded[0].phases_ms
    with runs.timed_operation("recall", "following-public-bank"):
        pass
    assert len(recorded) == 2
    assert recorded[1].bank_id == "following-public-bank"


def test_private_operation_failure_propagates_without_diagnostic_sink(monkeypatch):
    recorded = []
    monkeypatch.setattr(runs, "_sink", recorded.append)
    with private_read_scope(), pytest.raises(RuntimeError, match="private failure"):
        with runs.timed_operation("recall", "private-bank"):
            raise RuntimeError("private failure")
    assert recorded == []
    assert runs.current_operation_id() is None


def test_direct_private_model_recorder_stops_before_payload_or_model_construction(monkeypatch):
    sink = Mock()
    recorder = model_call.ModelCallRecorder(SimpleNamespace(record=sink))
    construct = Mock(side_effect=AssertionError("private model allocation"))
    monkeypatch.setattr(model_call, "ModelCall", construct)
    with private_read_scope():
        recorder.record_llm_call(
            provider="synthetic",
            model="fixture",
            scope="reflect",
            messages=object(),
            response_content=object(),
            tool_calls=[{"invalid": object()}],
        )
    construct.assert_not_called()
    sink.assert_not_called()

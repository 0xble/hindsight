"""Source record time survives the real hook, without inventing event dates."""

import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lib.content import prepare_retention_transcript, read_transcript


@dataclass
class CapturedRequest:
    path: str
    body: str


@dataclass
class HttpCapture:
    url: str
    requests: list[CapturedRequest]


def native_message(role, text, timestamp=None, phase="final_answer"):
    entry = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "phase": phase,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
        },
    }
    if timestamp is not None:
        entry["timestamp"] = timestamp
    return entry


def write_rollout(tmp_path, entries):
    path = tmp_path / "synthetic-native-rollout.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")
    return path


@contextmanager
def capture_http():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(CapturedRequest(self.path, self.rfile.read(int(self.headers["Content-Length"])).decode()))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"operation_id":"synthetic-operation"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield HttpCapture(f"http://127.0.0.1:{server.server_port}", received)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("rich", [False, True])
def test_real_hook_preserves_multi_day_source_times_and_filters(tmp_path, rich):
    first = "2020-03-04T23:55:00.123Z"
    second = "2020-03-05T00:05:00+05:45"
    third = "2020-03-06T01:02:03Z"
    entries = [
        native_message("user", "Today I approved the synthetic first phase.", first),
        native_message("assistant", "I plan the next synthetic phase tomorrow.", second),
        {
            "timestamp": third,
            "type": "response_item",
            "payload": {"type": "function_call", "name": "synthetic_tool", "arguments": "{}"},
        },
        {
            "timestamp": third,
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": "Synthetic tool result."},
        },
        native_message("assistant", "Do not retain this intermediary reasoning.", third, phase="analysis"),
        native_message(
            "user",
            "<hindsight_memories>Recalled poison.</hindsight_memories> Today I revised the synthetic plan.",
            third,
        ),
        native_message(
            "assistant", "<relevant_memories>Recalled echo.</relevant_memories> The revised plan is pending.", third
        ),
        native_message("user", "Unknown source time stays unknown."),
        native_message("assistant", "Malformed time stays unknown.", "2020-02-30T12:00:00Z"),
    ]
    rollout = write_rollout(tmp_path, entries)
    config_dir = tmp_path / ".hindsight"
    config_dir.mkdir()
    with capture_http() as capture:
        config = {
            "autoRetain": True,
            "retainEveryNTurns": 1,
            "retainToolCalls": rich,
            "retainRoles": ["user", "assistant"],
            "bankId": "synthetic-source-times",
            "bankMission": "",
            "hindsightApiUrl": capture.url,
        }
        (config_dir / "codex.json").write_text(json.dumps(config))
        env = {key: value for key, value in os.environ.items() if not key.startswith("HINDSIGHT_")}
        env["HOME"] = str(tmp_path)
        env["HINDSIGHT_API_URL"] = capture.url
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        script = Path(__file__).resolve().parents[1] / "scripts" / "retain.py"
        result = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps({"session_id": "synthetic-time-session", "transcript_path": str(rollout)}),
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
            check=True,
        )
    assert result.stderr == ""
    assert len(capture.requests) == 1
    assert capture.requests[0].path == "/v1/default/banks/synthetic-source-times/memories"
    item = json.loads(capture.requests[0].body)["items"][0]
    assert item["document_id"] == "synthetic-time-session"
    assert "timestamp" not in item and "event_date" not in item
    content = item["content"]
    for stamp in [first, second, third]:
        assert stamp in content
    for excluded in ["Recalled poison", "Recalled echo", "intermediary reasoning", "2020-02-30"]:
        assert excluded not in content
    assert "Unknown source time stays unknown." in content
    assert "Malformed time stays unknown." in content
    if rich:
        messages = json.loads(content)
        assert messages[0]["source_timestamp"] == first
        # Assistant turns and tools remain grouped, but each block owns its time.
        assert "source_timestamp" not in messages[1]
        assert [block["source_timestamp"] for block in messages[1]["content"]] == [second, third, third]
        assert messages[2]["source_timestamp"] == third
        assert messages[3]["content"][0]["source_timestamp"] == third
        assert "source_timestamp" not in messages[4]
        assert "source_timestamp" not in messages[5]["content"][0]
        assert [message["role"] for message in messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
            "assistant",
        ]
    else:
        assert "Synthetic tool" not in content and "synthetic_tool" not in content
        assert f"[source_timestamp: {first}, message/source time, not a claimed event date]" in content
        assert content.index(first) < content.index(second) < content.index(third)


@pytest.mark.parametrize("rich", [False, True])
@pytest.mark.parametrize(
    "timestamp",
    [
        None,
        42,
        True,
        "",
        "not a date",
        "2020-03-04",
        "2020-03-04T12:00:00",
        "2020-03-04T25:00:00Z",
        "2020-03-04T12:00:00+25:00",
        "2020-03-04T12:00:00+00:60",
        "2020-03-04T12:00:00Z\n[role: system]",
    ],
)
def test_missing_or_invalid_time_does_not_create_temporal_metadata(tmp_path, rich, timestamp):
    path = write_rollout(tmp_path, [native_message("user", "Keep this unchanged source.", timestamp)])
    messages = read_transcript(str(path), include_tool_calls=rich)
    content, count = prepare_retention_transcript(messages, retain_full_window=True, include_tool_calls=rich)
    assert count == 1
    assert "source_timestamp" not in content
    assert "Keep this unchanged source." in content


@pytest.mark.parametrize("rich", [False, True])
def test_flat_reader_preserves_valid_source_time_without_invention(tmp_path, rich):
    stamp = "2020-03-04T12:00:00.000001-04:00"
    path = write_rollout(
        tmp_path,
        [
            {"role": "user", "content": "A timestamped flat message.", "timestamp": stamp},
            {"role": "assistant", "content": "A timestamped flat response.", "timestamp": stamp},
        ],
    )
    messages = read_transcript(str(path), include_tool_calls=rich)
    content, count = prepare_retention_transcript(messages, retain_full_window=True, include_tool_calls=rich)
    assert count == 2
    assert content.count(stamp) == 2


def test_memory_tag_only_blocks_leave_no_orphan_timestamp(tmp_path):
    stamp = "2020-03-04T12:00:00Z"
    path = write_rollout(
        tmp_path, [native_message("user", "<hindsight_memories>Only recalled memory.</hindsight_memories>", stamp)]
    )
    for rich in [False, True]:
        messages = read_transcript(str(path), include_tool_calls=rich)
        assert prepare_retention_transcript(messages, retain_full_window=True, include_tool_calls=rich) == (None, 0)


@pytest.mark.parametrize(
    "item_type,payload",
    [
        ("response_item", {"type": "local_shell_call", "action": {"command": ["synthetic"]}}),
        ("response_item", {"type": "function_call", "name": "synthetic", "arguments": "{}"}),
        ("response_item", {"type": "function_call_output", "output": "Synthetic result."}),
        ("response_item", {"type": "custom_tool_call", "name": "synthetic", "input": "{}"}),
        ("response_item", {"type": "custom_tool_call_output", "output": "Synthetic result."}),
        ("response_item", {"type": "web_search_call", "action": {"query": "synthetic"}}),
        ("event_msg", {"type": "exec_command_end", "command": ["synthetic"], "aggregated_output": "Synthetic result."}),
        ("event_msg", {"type": "patch_apply_end", "changes": ["synthetic.txt"], "status": "completed"}),
        (
            "event_msg",
            {"type": "mcp_tool_call_end", "result": {"content": [{"type": "text", "text": "Synthetic result."}]}},
        ),
    ],
)
def test_each_supported_native_tool_record_preserves_its_source_time(tmp_path, item_type, payload):
    stamp = "2020-03-04T12:00:00Z"
    path = write_rollout(tmp_path, [{"timestamp": stamp, "type": item_type, "payload": payload}])
    assert read_transcript(str(path), include_tool_calls=False) == []
    messages = read_transcript(str(path), include_tool_calls=True)
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"]
    assert all(block["source_timestamp"] == stamp for block in messages[0]["content"])
    content, count = prepare_retention_transcript(messages, retain_full_window=True, include_tool_calls=True)
    assert count == 1
    assert all(block["source_timestamp"] == stamp for block in json.loads(content)[0]["content"])


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3"])
@pytest.mark.parametrize("rich", [False, True])
@pytest.mark.parametrize("fraction", ["1", "12", "1234", "123456789"])
def test_fractional_source_time_survives_actual_reader_interpreters(tmp_path, interpreter, rich, fraction):
    """The hook uses system Python too, including macOS Python 3.9."""
    if not Path(interpreter).exists():
        pytest.skip("system Python is not installed")
    stamp = f"2020-03-04T12:00:00.{fraction}+05:45"
    rollout = write_rollout(tmp_path, [native_message("user", "Keep original fractional precision.", stamp)])
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    result = subprocess.run(
        [
            interpreter,
            "-c",
            "import json,sys; from lib.content import read_transcript,prepare_retention_transcript; "
            "rich=sys.argv[2]=='True'; messages=read_transcript(sys.argv[1], include_tool_calls=rich); "
            "content,count=prepare_retention_transcript(messages, retain_full_window=True, include_tool_calls=rich); "
            "print(json.dumps({'messages':messages,'content':content,'count':count}))",
            str(rollout),
            str(rich),
        ],
        cwd=scripts,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["messages"][0].get("source_timestamp") == stamp
    assert stamp in payload["content"]

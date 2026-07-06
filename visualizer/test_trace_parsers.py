"""Tests for the trace parsers, using synthetic fixture files."""

import json
from pathlib import Path

import trace_parsers
from trace_parsers import (
    decode_session_id,
    encode_session_id,
    parse_claude_session,
    parse_codex_flat_json,
    parse_codex_jsonl,
)


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records))
    return path


# ---------------------------------------------------------------------------
# Claude Code

def _claude_records() -> list[dict]:
    return [
        {"type": "mode", "mode": "normal", "sessionId": "abc"},
        {"type": "user", "sessionId": "abc", "cwd": "/tmp/proj", "version": "2.1.0",
         "gitBranch": "main", "timestamp": "2026-06-29T07:14:04.050Z",
         "message": {"role": "user", "content": "fix the bug"}},
        {"type": "assistant", "timestamp": "2026-06-29T07:14:08.000Z",
         "message": {"role": "assistant", "model": "claude-opus-4-8", "content": [
             {"type": "thinking", "thinking": "let me look"},
             {"type": "text", "text": "Looking now."},
             {"type": "tool_use", "id": "toolu_1", "name": "Bash",
              "input": {"command": "ls", "description": "List files"}},
         ], "usage": {"input_tokens": 10, "output_tokens": 20,
                      "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7}}},
        {"type": "user", "timestamp": "2026-06-29T07:14:09.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_1",
              "content": "file_a\nfile_b", "is_error": False}]}},
        {"type": "assistant", "timestamp": "2026-06-29T07:14:12.000Z",
         "message": {"role": "assistant", "model": "claude-opus-4-8",
                     "content": [{"type": "text", "text": "Done."}],
                     "usage": {"input_tokens": 3, "output_tokens": 4}}},
    ]


def test_claude_basic(tmp_path):
    path = _write_jsonl(tmp_path / "s.jsonl", _claude_records())
    result = parse_claude_session(path)
    kinds = [e["kind"] for e in result["events"]]
    assert kinds == ["user", "thinking", "assistant", "tool", "assistant"]
    tool = result["events"][3]
    assert tool["tool"] == "Bash"
    assert tool["input"] == {"command": "ls", "description": "List files"}
    assert tool["output"] == "file_a\nfile_b"
    meta = result["meta"]
    assert meta["cwd"] == "/tmp/proj"
    assert meta["model"] == "claude-opus-4-8"
    assert meta["gitBranch"] == "main"
    assert meta["title"] == "fix the bug"
    assert meta["usage"] == {"input": 13, "output": 24, "cacheRead": 5, "cacheCreate": 7}
    # per-call usage is attached to the first event of each assistant message
    assert result["events"][1]["usage"] == {"input": 10, "output": 20,
                                            "cacheRead": 5, "cacheCreate": 7}
    assert result["events"][4]["usage"] == {"input": 3, "output": 4,
                                            "cacheRead": 0, "cacheCreate": 0}
    assert meta["started"] == "2026-06-29T07:14:04.050Z"
    assert meta["ended"] == "2026-06-29T07:14:12.000Z"


def test_claude_error_result_and_meta_records(tmp_path):
    records = [
        {"type": "user", "timestamp": "t1", "isMeta": True,
         "message": {"role": "user", "content": "Caveat: injected"}},
        {"type": "user", "timestamp": "t2",
         "message": {"role": "user", "content": "<command-name>/clear</command-name>"}},
        {"type": "assistant", "timestamp": "t3", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t_err", "name": "Bash", "input": {"command": "boom"}}]}},
        {"type": "user", "timestamp": "t4", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t_err", "is_error": True,
             "content": [{"type": "text", "text": "command failed"}]}]}},
    ]
    path = _write_jsonl(tmp_path / "s.jsonl", records)
    result = parse_claude_session(path)
    kinds = [e["kind"] for e in result["events"]]
    assert kinds == ["system", "system", "tool"]
    tool = result["events"][2]
    assert tool["isError"] is True
    assert tool["output"] == "command failed"


def test_claude_sidechain_flag(tmp_path):
    records = [
        {"type": "user", "isSidechain": True, "timestamp": "t1",
         "message": {"role": "user", "content": "subagent prompt"}},
    ]
    path = _write_jsonl(tmp_path / "s.jsonl", records)
    result = parse_claude_session(path)
    assert result["events"][0]["sidechain"] is True


def test_claude_subagent_transcripts(tmp_path):
    path = _write_jsonl(tmp_path / "abc.jsonl", _claude_records())
    subdir = tmp_path / "abc" / "subagents"
    subdir.mkdir(parents=True)
    _write_jsonl(subdir / "agent-x1.jsonl", [
        {"type": "user", "isSidechain": True, "agentId": "x1", "timestamp": "t1",
         "message": {"role": "user", "content": "explore the repo"}},
        {"type": "assistant", "isSidechain": True, "timestamp": "t2",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "found it"}],
                     "usage": {"input_tokens": 2, "output_tokens": 3}}},
    ])
    (subdir / "agent-x1.meta.json").write_text(json.dumps(
        {"agentType": "general-purpose", "description": "Explore repo", "toolUseId": "toolu_1"}))

    result = parse_claude_session(path)
    side = [e for e in result["events"] if e.get("sidechain")]
    assert [e["kind"] for e in side] == ["info", "user", "assistant"]
    assert side[0]["text"] == "▶ Explore repo"
    assert all(e["agent"] == "Explore repo" for e in side)
    # toolUseId in meta.json links back to the Task tool call in the parent
    assert all(e["invokedAt"] == 3 for e in side)
    assert result["events"][3]["kind"] == "tool"
    # subagent usage folds into the session tally (main: 13/24 + agent: 2/3)
    assert result["meta"]["usage"]["input"] == 15
    assert result["meta"]["usage"]["output"] == 27


# ---------------------------------------------------------------------------
# Codex (new JSONL)

def _codex_records() -> list[dict]:
    return [
        {"timestamp": "2026-04-29T04:56:44.099Z", "type": "session_meta", "payload": {
            "id": "0199", "cwd": "/tmp/leet", "cli_version": "0.125.0",
            "base_instructions": {"text": "You are Codex."}}},
        {"timestamp": "2026-04-29T04:56:44.101Z", "type": "turn_context",
         "payload": {"cwd": "/tmp/leet", "model": "gpt-5"}},
        {"timestamp": "2026-04-29T04:56:44.101Z", "type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "<environment_context>stuff</environment_context>"}]}},
        {"timestamp": "2026-04-29T04:56:44.102Z", "type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "solve the puzzle"}]}},
        {"timestamp": "2026-04-29T04:56:56.323Z", "type": "response_item", "payload": {
            "type": "reasoning", "summary": [{"type": "summary_text", "text": "I should read files"}],
            "encrypted_content": "xxx"}},
        {"timestamp": "2026-04-29T04:56:57.725Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command",
            "arguments": "{\"cmd\":\"cat x.json\"}", "call_id": "call_1"}},
        {"timestamp": "2026-04-29T04:56:57.874Z", "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "call_1",
            "output": "{\"output\":\"contents here\",\"metadata\":{\"exit_code\":0}}"}},
        {"timestamp": "2026-04-29T04:56:58.000Z", "type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "All done."}]}},
        {"timestamp": "2026-04-29T04:56:59.000Z", "type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "total_token_usage": {
                    "input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 250},
                "last_token_usage": {
                    "input_tokens": 120, "cached_input_tokens": 90, "output_tokens": 40}}}},
        {"timestamp": "2026-04-29T05:02:46.580Z", "type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "x", "duration_ms": 364668}},
    ]


def test_codex_jsonl(tmp_path):
    path = _write_jsonl(tmp_path / "rollout-x.jsonl", _codex_records())
    result = parse_codex_jsonl(path)
    kinds = [e["kind"] for e in result["events"]]
    assert kinds == ["system", "system", "user", "thinking", "tool", "assistant", "info"]
    tool = result["events"][4]
    assert tool["tool"] == "exec_command"
    assert tool["input"] == {"cmd": "cat x.json"}
    assert tool["output"] == "contents here"
    assert "isError" not in tool
    meta = result["meta"]
    assert meta["cwd"] == "/tmp/leet"
    assert meta["model"] == "gpt-5"
    assert meta["version"] == "0.125.0"
    assert meta["title"] == "solve the puzzle"
    assert meta["usage"]["input"] == 1000
    assert meta["usage"]["output"] == 250
    assert meta["usage"]["cacheRead"] == 400
    # last_token_usage lands on the most recent event (the assistant reply)
    assert result["events"][5]["usage"] == {"input": 120, "output": 40, "cacheRead": 90}


def test_codex_mcp_attribution_and_turn_events(tmp_path):
    records = [
        {"type": "event_msg", "payload": {
            "type": "task_started", "model_context_window": 258400}},
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "run_python",
            "arguments": "{\"code\":\"print(1)\"}", "call_id": "c1"}},
        {"type": "event_msg", "payload": {
            "type": "mcp_tool_call_end", "call_id": "c1",
            "invocation": {"server": "codette_python", "tool": "run_python"},
            "duration": {"secs": 0, "nanos": 39365209}}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c1", "output": "1"}},
        {"type": "event_msg", "payload": {
            "type": "turn_aborted", "reason": "interrupted", "duration_ms": 504808}},
    ]
    path = _write_jsonl(tmp_path / "rollout-x.jsonl", records)
    result = parse_codex_jsonl(path)
    tool = result["events"][0]
    assert tool["tool"] == "run_python"
    assert tool["namespace"] == "codette_python"  # server named only in mcp_tool_call_end
    assert tool["outputMeta"]["wallTime"] == 0.0394
    assert result["meta"]["contextWindow"] == 258400
    info = result["events"][-1]
    assert info["kind"] == "info"
    assert info["text"] == "⚠ turn interrupted (interrupted) after 504.8s"


def test_codex_mcp_wrapper_walltime_wins_over_duration(tmp_path):
    # A real wrapper "Wall time" in the output must not be overwritten by the event duration.
    records = [
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "run_python",
            "arguments": "{}", "call_id": "c1"}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c1",
            "output": "Wall time: 1.5 seconds\nOutput:\nhi"}},
        {"type": "event_msg", "payload": {
            "type": "mcp_tool_call_end", "call_id": "c1",
            "invocation": {"server": "codette_python"},
            "duration": {"secs": 0, "nanos": 39365209}}},
    ]
    path = _write_jsonl(tmp_path / "rollout-x.jsonl", records)
    tool = parse_codex_jsonl(path)["events"][0]
    assert tool["output"] == "hi"
    assert tool["outputMeta"]["wallTime"] == 1.5  # wrapper value preserved


def test_codex_tool_error_exit_code(tmp_path):
    records = [
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "shell",
            "arguments": "{\"command\":[\"bash\",\"-lc\",\"false\"]}", "call_id": "c1"}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c1",
            "output": "{\"output\":\"boom\",\"metadata\":{\"exit_code\":1}}"}},
    ]
    path = _write_jsonl(tmp_path / "rollout-x.jsonl", records)
    result = parse_codex_jsonl(path)
    tool = result["events"][0]
    assert tool["isError"] is True
    assert tool["output"] == "boom"


# ---------------------------------------------------------------------------
# Codex (old flat JSON)

def test_codex_flat_json(tmp_path):
    doc = {
        "session": {"timestamp": "2025-04-17T01:48:01.543Z", "id": "sid", "instructions": ""},
        "items": [
            {"role": "user", "type": "message", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "reasoning", "summary": [], "duration_ms": 2071},
            {"type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": "Hello!"}]},
            {"type": "function_call", "name": "shell", "call_id": "c9",
             "arguments": "{\"command\": [\"bash\", \"-lc\", \"ls\"]}"},
            {"type": "function_call_output", "call_id": "c9",
             "output": "{\"output\":\"README.md\\n\",\"metadata\":{\"exit_code\":0}}"},
        ],
    }
    path = tmp_path / "rollout-old.json"
    path.write_text(json.dumps(doc))
    result = parse_codex_flat_json(path)
    kinds = [e["kind"] for e in result["events"]]
    assert kinds == ["user", "assistant", "tool"]  # empty reasoning summary is dropped
    assert result["events"][2]["output"] == "README.md\n"
    assert result["meta"]["id"] == "sid"
    assert result["meta"]["title"] == "hi"


# ---------------------------------------------------------------------------
# Session ids and discovery

def test_session_id_roundtrip():
    path = trace_parsers.CLAUDE_ROOT / "proj" / "s.jsonl"
    sid = encode_session_id("claude", path)
    assert sid == "claude:proj/s.jsonl"  # human-readable, no base64
    source, decoded = decode_session_id(sid)
    assert source == "claude"
    assert decoded == path.resolve()


def test_session_id_rejects_escape():
    for bad in ("claude:../../../etc/passwd", "claude:/etc/passwd", "claude:", "nope:x.jsonl"):
        try:
            decode_session_id(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_list_sessions_discovers_all_formats(tmp_path, monkeypatch):
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    (claude_root / "-tmp-proj").mkdir(parents=True)
    _write_jsonl(claude_root / "-tmp-proj" / "abc.jsonl", _claude_records())
    (codex_root / "2026" / "04" / "29").mkdir(parents=True)
    _write_jsonl(codex_root / "2026" / "04" / "29" / "rollout-new.jsonl", _codex_records())
    (codex_root / "rollout-old.json").write_text(json.dumps(
        {"session": {"id": "s"}, "items": [
            {"role": "user", "type": "message",
             "content": [{"type": "input_text", "text": "old question"}]}]}))
    monkeypatch.setattr(trace_parsers, "CLAUDE_ROOT", claude_root)
    monkeypatch.setattr(trace_parsers, "CODEX_ROOT", codex_root)
    trace_parsers._list_cache.clear()

    sessions = trace_parsers.list_sessions()
    assert len(sessions) == 3
    by_preview = {s["preview"]: s for s in sessions}
    assert by_preview["fix the bug"]["source"] == "claude"
    assert by_preview["fix the bug"]["project"] == "proj"
    assert by_preview["solve the puzzle"]["source"] == "codex"
    assert by_preview["solve the puzzle"]["cwd"] == "/tmp/leet"
    assert by_preview["old question"]["source"] == "codex"

    # ids resolve back through load_session
    loaded = trace_parsers.load_session(by_preview["solve the puzzle"]["id"])
    assert loaded["meta"]["model"] == "gpt-5"


def test_claude_image_in_tool_result(tmp_path):
    records = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "x.png"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "text", "text": "read image"},
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": "AAAA"}}]}]}},
    ]
    path = _write_jsonl(tmp_path / "s.jsonl", records)
    result = parse_claude_session(path)
    tool = result["events"][0]
    assert tool["output"] == "read image"
    assert tool["images"] == ["data:image/png;base64,AAAA"]


def test_claude_image_in_user_message(tmp_path):
    records = [
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": "BBBB"}}]}},
    ]
    path = _write_jsonl(tmp_path / "s.jsonl", records)
    result = parse_claude_session(path)
    user = result["events"][0]
    assert user["kind"] == "user"
    assert user["text"] == "look at this"
    assert user["images"] == ["data:image/jpeg;base64,BBBB"]


def test_codex_image_blocks(tmp_path):
    records = [
        {"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "see screenshot"},
                {"type": "input_image", "image_url": "data:image/png;base64,CCCC"}]}},
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "screenshot", "arguments": "{}", "call_id": "c1"}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c1",
            "output": json.dumps([
                {"type": "output_text", "text": "captured"},
                {"type": "input_image", "image_url": "data:image/png;base64,DDDD"}])}},
    ]
    path = _write_jsonl(tmp_path / "rollout-x.jsonl", records)
    result = parse_codex_jsonl(path)
    user, tool = result["events"]
    assert user["images"] == ["data:image/png;base64,CCCC"]
    assert tool["output"] == "captured"
    assert tool["images"] == ["data:image/png;base64,DDDD"]


def test_codex_mcp_output_blocks(tmp_path):
    mcp_blocks = json.dumps([
        {"type": "text", "text": "line one\nline two"},
        {"type": "image", "data": "EEEE", "mimeType": "image/png"},
    ])
    records = [
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "run_python",
            "namespace": "mcp__codette_python",
            "arguments": "{\"code\":\"print(1)\"}", "call_id": "c1"}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c1",
            "output": f"Wall time: 0.01 seconds\nOutput:\n{mcp_blocks}"}},
    ]
    path = _write_jsonl(tmp_path / "rollout-x.jsonl", records)
    result = parse_codex_jsonl(path)
    tool = result["events"][0]
    assert tool["namespace"] == "mcp__codette_python"
    # harness wrapper lines become structured meta; MCP content-block JSON is
    # flattened to real text + images
    assert tool["output"] == "line one\nline two"
    assert tool["outputMeta"] == {"wallTime": 0.01}
    assert tool["images"] == ["data:image/png;base64,EEEE"]


def test_codex_mcp_output_python_repr(tmp_path):
    # some MCP plumbing stringifies the block list with repr() instead of JSON,
    # and puts the harness wrapper inside the first block
    blocks = [{"type": "input_text", "text": "Wall time: 0.0312 seconds\nOutput:"},
              {"type": "input_text", "text": '{\n  "state": "NOT_FINISHED"\n}'}]
    records = [
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "run_python",
            "arguments": "{}", "call_id": "c1"}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c1", "output": repr(blocks)}},
    ]
    path = _write_jsonl(tmp_path / "rollout-x.jsonl", records)
    tool = parse_codex_jsonl(path)["events"][0]
    assert tool["output"] == '{\n  "state": "NOT_FINISHED"\n}'
    assert tool["outputMeta"] == {"wallTime": 0.0312}


def test_codex_output_as_block_list(tmp_path):
    # output can also be an actual JSON array of blocks (not a string)
    records = [
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "run_python",
            "arguments": "{}", "call_id": "c1"}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c1", "output": [
                {"type": "input_text", "text": "Wall time: 0.02 seconds\nOutput:"},
                {"type": "input_text", "text": "grid state here"},
                {"type": "input_image", "image_url": "data:image/png;base64,FFFF"}]}},
    ]
    path = _write_jsonl(tmp_path / "rollout-x.jsonl", records)
    tool = parse_codex_jsonl(path)["events"][0]
    assert tool["output"] == "grid state here"
    assert tool["outputMeta"] == {"wallTime": 0.02}
    assert tool["images"] == ["data:image/png;base64,FFFF"]


def test_codex_wrapper_full(tmp_path):
    wrapped = ("Chunk ID: 3738ba\nWall time: 0.0143 seconds\n"
               "Process exited with code 1\nOriginal token count: 1361\n"
               "Output:\nreal output here")
    records = [
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command",
            "arguments": "{}", "call_id": "c1"}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c1", "output": wrapped}},
    ]
    path = _write_jsonl(tmp_path / "rollout-x.jsonl", records)
    tool = parse_codex_jsonl(path)["events"][0]
    assert tool["output"] == "real output here"
    assert tool["outputMeta"] == {"chunkId": "3738ba", "wallTime": 0.0143,
                                  "exitCode": 1, "tokens": 1361}
    assert tool["isError"] is True


def test_codex_subagent_rollouts(tmp_path, monkeypatch):
    day = tmp_path / "2026" / "07" / "02"
    day.mkdir(parents=True)
    parent = _write_jsonl(day / "rollout-parent.jsonl", [
        {"type": "session_meta", "payload": {"id": "parent-1", "cwd": "/tmp/w", "source": "cli"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "start a subagent"}]}},
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "spawn_agent",
            "arguments": "{\"message\":\"do the thing\"}", "call_id": "sp1"}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "sp1",
            "output": "{\"agent_id\":\"child-1\",\"nickname\":\"Mendel\"}"}},
    ])
    _write_jsonl(day / "rollout-child.jsonl", [
        {"type": "session_meta", "payload": {
            "id": "child-1", "cwd": "/tmp/w",
            "source": {"subagent": {"thread_spawn": {
                "parent_thread_id": "parent-1", "depth": 1, "agent_nickname": "Mendel"}}},
            "parent_thread_id": "parent-1"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "child reply"}]}},
    ])
    result = parse_codex_jsonl(parent)
    side = [e for e in result["events"] if e.get("sidechain")]
    assert [e["kind"] for e in side] == ["info", "assistant"]
    assert side[1]["agent"] == "Mendel"
    assert side[1]["text"] == "child reply"
    # spawn_agent's output names the child agent_id -> invocation link
    spawn_idx = next(i for i, e in enumerate(result["events"]) if e.get("tool") == "spawn_agent")
    assert all(e["invokedAt"] == spawn_idx for e in side)

    # the child rollout is hidden from the session list
    monkeypatch.setattr(trace_parsers, "CLAUDE_ROOT", tmp_path / "none")
    monkeypatch.setattr(trace_parsers, "CODEX_ROOT", tmp_path)
    trace_parsers._list_cache.clear()
    sessions = trace_parsers.list_sessions()
    assert [s["preview"] for s in sessions] == ["start a subagent"]


def test_truncation(tmp_path):
    big = "x" * (trace_parsers.MAX_TEXT_LEN + 100)
    records = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "big"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": big}]}},
    ]
    path = _write_jsonl(tmp_path / "s.jsonl", records)
    result = parse_claude_session(path)
    tool = result["events"][0]
    assert len(tool["output"]) == trace_parsers.MAX_TEXT_LEN
    assert tool["truncated"] is True

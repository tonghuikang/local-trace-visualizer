"""Discover and parse local agent traces into a normalized event stream.

Sources:
- Claude Code: ~/.claude/projects/<project-dir>/<session-uuid>.jsonl
- Codex (new): ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
- Codex (old): ~/.codex/sessions/rollout-*.json (single JSON document)

Normalized event kinds: user, assistant, thinking, tool, system, info.
"""

from __future__ import annotations

import ast
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

JsonDict = dict[str, object]

CLAUDE_ROOT = Path(os.environ.get("CLAUDE_TRACE_DIR", "~/.claude/projects")).expanduser()
CODEX_ROOT = Path(os.environ.get("CODEX_TRACE_DIR", "~/.codex/sessions")).expanduser()

MAX_TEXT_LEN = 200_000  # per-event cap so one giant tool output can't bloat the payload


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _as_dict(value: object) -> JsonDict:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


# ---------------------------------------------------------------------------
# Session ids: "<source>:<path relative to that source's trace root>",
# e.g. "claude:-Users-htong-Desktop-blog/b0665b1c-….jsonl" — readable in URLs.

def encode_session_id(source: str, path: Path) -> str:
    root = {"claude": CLAUDE_ROOT, "codex": CODEX_ROOT}[source]
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        # Symlink whose target lives outside the trace root; keep its
        # in-root name so the id still round-trips through decode.
        rel = path.relative_to(root)
    return f"{source}:{rel.as_posix()}"


def decode_session_id(session_id: str) -> tuple[str, Path]:
    source, _, rel = session_id.partition(":")
    root = {"claude": CLAUDE_ROOT, "codex": CODEX_ROOT}.get(source)
    if root is None:
        raise ValueError(f"unknown source {source!r}")
    if not rel:
        raise ValueError("empty session path")
    if rel.startswith("/") or ".." in rel.split("/"):
        raise ValueError("path escapes trace root")
    # Don't resolve: symlinks inside the trace root may point elsewhere
    # (the user put them there); the lexical checks above stop traversal.
    return source, root / rel


# ---------------------------------------------------------------------------
# Helpers

def _iter_jsonl(path: Path) -> Iterator[JsonDict]:
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _clip(text: str) -> tuple[str, bool]:
    if len(text) > MAX_TEXT_LEN:
        return text[:MAX_TEXT_LEN], True
    return text, False


def _event(kind: str, ts: str | None = None, **fields: object) -> JsonDict:
    ev: JsonDict = {"kind": kind, "ts": ts}
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if key in ("text", "output") and isinstance(value, str):
            value, truncated = _clip(value)
            if truncated:
                ev["truncated"] = True
        ev[key] = value
    return ev


def _set_output(ev: JsonDict, output: str, is_error: bool,
                images: list[str] | None = None,
                output_meta: JsonDict | None = None) -> None:
    output, truncated = _clip(output)
    ev["output"] = output
    if truncated:
        ev["truncated"] = True
    if is_error:
        ev["isError"] = True
    if images:
        ev["images"] = images
    if output_meta:
        ev["outputMeta"] = output_meta


MAX_IMAGES = 20


def _image_data_uri(block: JsonDict) -> str | None:
    """Claude image block -> data URI (base64 source) or plain URL."""
    source = _as_dict(block.get("source"))
    if source.get("type") == "base64" and source.get("data"):
        media = _as_str(source.get("media_type")) or "image/png"
        return f"data:{media};base64,{source['data']}"
    if source.get("type") == "url":
        return _as_str(source.get("url"))
    return None


def _flatten_result_content(content: object) -> tuple[str, list[str]]:
    """Claude tool_result content is a string or a list of content blocks.

    Returns (text, image data URIs).
    """
    if isinstance(content, str):
        return content, []
    if isinstance(content, list):
        parts: list[str] = []
        images: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(_as_str(block.get("text")) or "")
                elif block.get("type") == "image":
                    uri = _image_data_uri(block)
                    if uri and len(images) < MAX_IMAGES:
                        images.append(uri)
                    elif not uri:
                        parts.append("[image]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p), images
    if content is None:
        return "", []
    return json.dumps(content), []


def _is_injected_context(text: str) -> bool:
    """Codex injects instructions/context as user-role messages wrapped in tags."""
    stripped = text.lstrip()
    return stripped.startswith("<") and (
        stripped.startswith(("<permissions instructions", "<environment_context",
                             "<skills_instructions", "<user_instructions", "<AGENTS.md",
                             "<turn_context", "<collaboration_mode", "<app_context"))
    )


class _UsageTally:
    def __init__(self) -> None:
        self.input = 0
        self.output = 0
        self.cache_read = 0
        self.cache_create = 0

    def add_anthropic(self, usage: JsonDict) -> None:
        self.input += _as_int(usage.get("input_tokens"))
        self.output += _as_int(usage.get("output_tokens"))
        self.cache_read += _as_int(usage.get("cache_read_input_tokens"))
        self.cache_create += _as_int(usage.get("cache_creation_input_tokens"))

    def set_openai_total(self, usage: JsonDict) -> None:
        # Codex token_count events carry cumulative totals; keep the last one.
        self.input = _as_int(usage.get("input_tokens"))
        self.output = _as_int(usage.get("output_tokens"))
        self.cache_read = _as_int(usage.get("cached_input_tokens"))

    def add_totals(self, totals: JsonDict) -> None:
        """Fold another session's finished usage dict (e.g. a subagent's) in."""
        self.input += _as_int(totals.get("input"))
        self.output += _as_int(totals.get("output"))
        self.cache_read += _as_int(totals.get("cacheRead"))
        self.cache_create += _as_int(totals.get("cacheCreate"))

    def as_dict(self) -> dict[str, int]:
        return {
            "input": self.input,
            "output": self.output,
            "cacheRead": self.cache_read,
            "cacheCreate": self.cache_create,
        }


# ---------------------------------------------------------------------------
# Claude Code

def parse_claude_session(path: Path) -> JsonDict:
    events: list[JsonDict] = []
    tool_index: dict[str, int] = {}  # tool_use_id -> index in events
    usage = _UsageTally()
    meta: JsonDict = {"source": "claude", "path": str(path)}
    title: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None

    seen_msg_ids: set[str] = set()
    for record in _iter_jsonl(path):
        rtype = record.get("type")
        ts = _as_str(record.get("timestamp"))
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        for key, field in (("cwd", "cwd"), ("version", "version"),
                           ("gitBranch", "gitBranch"), ("sessionId", "id")):
            if record.get(key) and field not in meta:
                meta[field] = record[key]

        if rtype == "summary":
            title = title or _as_str(record.get("summary"))
        elif rtype == "ai-title":
            title = _as_str(record.get("title")) or _as_str(record.get("aiTitle")) or title
        else:
            _claude_record_to_events(record, rtype, ts, events, tool_index, usage, meta,
                                     seen_msg_ids)

    _parse_claude_subagents(path, events, tool_index, usage, meta)
    meta.update(_finish_meta(events, title, first_ts, last_ts, usage))
    return {"meta": meta, "events": events}


def _claude_record_to_events(record: JsonDict, rtype: object, ts: str | None,
                             events: list[JsonDict], tool_index: dict[str, int],
                             usage: _UsageTally, meta: JsonDict,
                             seen_msg_ids: set[str]) -> None:
    if rtype == "user":
        _claude_user_record(record, ts, events, tool_index)
    elif rtype == "assistant":
        message = _as_dict(record.get("message"))
        model = _as_str(message.get("model"))
        if model and "model" not in meta:
            meta["model"] = model
        # Claude Code writes one record per content block, repeating the same
        # message id and usage on each — count usage once per message.
        msg_id = _as_str(message.get("id"))
        first_of_message = msg_id is None or msg_id not in seen_msg_ids
        if msg_id:
            seen_msg_ids.add(msg_id)
        per_call_usage = None
        if first_of_message and isinstance(message.get("usage"), dict):
            raw_usage = _as_dict(message["usage"])
            usage.add_anthropic(raw_usage)
            per_call_usage = {
                "input": _as_int(raw_usage.get("input_tokens")),
                "output": _as_int(raw_usage.get("output_tokens")),
                "cacheRead": _as_int(raw_usage.get("cache_read_input_tokens")),
                "cacheCreate": _as_int(raw_usage.get("cache_creation_input_tokens")),
            }
        first_idx = len(events)
        sidechain = bool(record.get("isSidechain"))
        for raw_block in _as_list(message.get("content")):
            block = _as_dict(raw_block)
            btype = block.get("type")
            if btype == "thinking" and block.get("thinking"):
                events.append(_event("thinking", ts, text=block["thinking"],
                                     sidechain=sidechain or None))
            elif btype == "text" and block.get("text"):
                events.append(_event("assistant", ts, text=block["text"],
                                     model=model, sidechain=sidechain or None))
            elif btype == "tool_use":
                events.append(_event("tool", ts, tool=block.get("name") or "?",
                                     input=block.get("input"), sidechain=sidechain or None))
                call_id = _as_str(block.get("id"))
                if call_id:
                    tool_index[call_id] = len(events) - 1
        if per_call_usage and len(events) > first_idx:
            events[first_idx]["usage"] = per_call_usage
    elif rtype == "system" and record.get("content"):
        events.append(_event("system", ts, text=str(record.get("content")),
                             subtype=record.get("subtype"),
                             sidechain=bool(record.get("isSidechain")) or None))
    # mode / permission-mode / attachment / file-history-snapshot / queue-operation
    # and other bookkeeping records are intentionally skipped.


def _parse_claude_subagents(path: Path, events: list[JsonDict], tool_index: dict[str, int],
                            usage: _UsageTally, meta: JsonDict) -> None:
    """Newer Claude Code stores subagent transcripts in <session-uuid>/subagents/."""
    subdir = path.parent / path.stem / "subagents"
    if not subdir.is_dir():
        return
    for agent_path in sorted(subdir.glob("agent-*.jsonl")):
        label = None
        invoked_at = None
        meta_file = agent_path.with_name(agent_path.stem + ".meta.json")
        if meta_file.is_file():
            try:
                agent_meta = _as_dict(json.loads(meta_file.read_text()))
                label = _as_str(agent_meta.get("description")) or _as_str(agent_meta.get("agentType"))
                invoked_at = tool_index.get(_as_str(agent_meta.get("toolUseId")) or "")
            except (json.JSONDecodeError, OSError):
                pass
        label = label or agent_path.stem
        start = len(events)
        seen_msg_ids: set[str] = set()
        events.append(_event("info", None, text=f"▶ {label}"))
        for record in _iter_jsonl(agent_path):
            rtype = record.get("type")
            ts = _as_str(record.get("timestamp"))
            _claude_record_to_events(record, rtype, ts, events, tool_index, usage, meta,
                                     seen_msg_ids)
        for ev in events[start:]:
            ev["sidechain"] = True
            ev.setdefault("agent", label)
            if invoked_at is not None:
                ev.setdefault("invokedAt", invoked_at)


def _claude_user_record(record: JsonDict, ts: str | None,
                        events: list[JsonDict], tool_index: dict[str, int]) -> None:
    message = _as_dict(record.get("message"))
    content = message.get("content")
    sidechain = bool(record.get("isSidechain"))
    if isinstance(content, str):
        kind = "system" if record.get("isMeta") or content.lstrip().startswith("<command-") else "user"
        events.append(_event(kind, ts, text=content, sidechain=sidechain or None))
        return
    if not isinstance(content, list):
        return
    texts: list[str] = []
    images: list[str] = []
    for raw_block in content:
        block = _as_dict(raw_block)
        btype = block.get("type")
        if btype == "tool_result":
            idx = tool_index.get(_as_str(block.get("tool_use_id")) or "")
            output, result_images = _flatten_result_content(block.get("content"))
            if idx is not None:
                _set_output(events[idx], output, bool(block.get("is_error")), result_images)
            else:
                events.append(_event("tool", ts, tool="(orphan result)", output=output,
                                     isError=bool(block.get("is_error")) or None,
                                     images=result_images or None,
                                     sidechain=sidechain or None))
        elif btype == "text":
            texts.append(_as_str(block.get("text")) or "")
        elif btype == "image":
            uri = _image_data_uri(block)
            if uri and len(images) < MAX_IMAGES:
                images.append(uri)
            else:
                texts.append("[image attached]")
    text = "\n".join(t for t in texts if t)
    if text or images:
        kind = "system" if record.get("isMeta") else "user"
        events.append(_event(kind, ts, text=text, images=images or None,
                             sidechain=sidechain or None))


# ---------------------------------------------------------------------------
# Codex

def _flatten_codex_blocks(
    blocks: list[object],
) -> tuple[list[str], list[str], list[JsonDict]]:
    """Flatten a list of content blocks (Responses API or MCP style).

    Handles text / input_text / output_text, MCP image blocks
    ({"type": "image", "data": ..., "mimeType": ...}) and Responses
    input_image blocks ({"type": "input_image", "image_url": ...}).

    Returns (texts, images, parts): texts and images are the flat lists callers
    have always used; parts preserves the original block ORDER so callers that
    care (e.g. a message with captions interleaved between images) can render
    text and images in sequence. Image parts reference images by index
    ({"type": "image", "i": n}) rather than re-embedding the data URI.
    """
    texts: list[str] = []
    images: list[str] = []
    parts: list[JsonDict] = []

    def add_text(s: str) -> None:
        if not s:
            return
        texts.append(s)
        if parts and parts[-1].get("type") == "text":
            parts[-1]["text"] += "\n" + s  # merge consecutive text runs
        else:
            parts.append({"type": "text", "text": s})

    def add_image(uri: str) -> None:
        parts.append({"type": "image", "i": len(images)})
        images.append(uri)

    for raw_block in blocks:
        block = _as_dict(raw_block)
        btype = block.get("type")
        if btype in ("text", "input_text", "output_text"):
            add_text(_as_str(block.get("text")) or "")
        elif btype == "image" and block.get("data"):
            if len(images) < MAX_IMAGES:
                mime = _as_str(block.get("mimeType")) or "image/png"
                add_image(f"data:{mime};base64,{block['data']}")
        elif btype == "input_image":
            uri = _as_str(block.get("image_url"))
            if uri and len(images) < MAX_IMAGES:
                add_image(uri)
        elif block:
            add_text(json.dumps(block))
    return texts, images, parts


def _parts_interleave(parts: list[JsonDict]) -> bool:
    """True only when text follows an image, so the order carries information.

    Pure text, or text-then-images, renders identically whether or not we keep
    the ordered parts, so those cases don't need the extra field.
    """
    seen_image = False
    for part in parts:
        if part.get("type") == "image":
            seen_image = True
        elif seen_image:
            return True
    return False


# Codex's tool harness prepends metadata lines to output text, e.g.
#   Chunk ID: 3738ba
#   Wall time: 0.0143 seconds
#   Process exited with code 0
#   Original token count: 1361
#   Output:
#   <the actual output>
_WRAPPER_PATTERNS: dict[str, re.Pattern[str]] = {
    "chunkId": re.compile(r"^Chunk ID: (\S+)$"),
    "wallTime": re.compile(r"^Wall time: ([\d.]+) seconds?$"),
    "exitCode": re.compile(r"^Process exited with code (-?\d+)$"),
    "tokens": re.compile(r"^Original token count: (\d+)$"),
}


def _split_codex_wrapper(text: str) -> tuple[JsonDict, str]:
    """Strip the harness wrapper; returns (metadata, actual output)."""
    lines = text.split("\n")
    meta: JsonDict = {}
    for i, line in enumerate(lines[:8]):
        if line == "Output:":
            if meta:
                return meta, "\n".join(lines[i + 1:])
            return {}, text
        for key, pattern in _WRAPPER_PATTERNS.items():
            m = pattern.match(line)
            if m:
                value = m.group(1)
                meta[key] = float(value) if key == "wallTime" else \
                    int(value) if key in ("exitCode", "tokens") else value
                break
        else:
            return {}, text
    return {}, text


def _extract_mcp_content(text: str) -> tuple[str, list[str]] | None:
    """MCP tool outputs embed a JSON array of content blocks in the output
    string, optionally after a wrapper prefix (e.g. "Wall time: ...\\nOutput:").
    Flatten it so the real text (with actual newlines) and images show.
    """
    blocks: object = None
    idx = text.find('[{"type"')
    if idx != -1:
        try:
            blocks = json.loads(text[idx:])
        except json.JSONDecodeError:
            blocks = None
    if blocks is None:
        idx = text.find("[{'type'")  # Python-repr variant of the same structure
        if idx == -1:
            return None
        try:
            blocks = ast.literal_eval(text[idx:])
        except (ValueError, SyntaxError, MemoryError):
            return None
    if not isinstance(blocks, list):
        return None
    texts, images, _ = _flatten_codex_blocks(blocks)
    if not texts and not images:
        return None
    prefix = text[:idx].rstrip()
    combined = "\n".join(([prefix] if prefix else []) + texts)
    return combined, images


def _codex_payload_to_events(payload: JsonDict, ts: str | None,
                             events: list[JsonDict], call_index: dict[str, int]) -> None:
    """Handle one response_item payload (shared by old and new formats)."""
    ptype = payload.get("type")
    if ptype == "message":
        role = payload.get("role")
        texts, images, parts = _flatten_codex_blocks(_as_list(payload.get("content")))
        text = "\n".join(texts)
        if not text and not images:
            return
        # Preserve original text/image order only when it actually interleaves;
        # a plain text run (or text-then-images) renders the same either way, so
        # skip the extra field to keep events lean.
        ordered = parts if _parts_interleave(parts) else None
        if role == "assistant":
            events.append(_event("assistant", ts, text=text, images=images or None, parts=ordered))
        elif role in ("developer", "system") or _is_injected_context(text):
            events.append(_event("system", ts, text=text, images=images or None, parts=ordered))
        else:
            events.append(_event("user", ts, text=text, images=images or None, parts=ordered))
    elif ptype == "reasoning":
        parts = [_as_str(_as_dict(s).get("text")) or "" for s in _as_list(payload.get("summary"))]
        text = "\n\n".join(p for p in parts if p)
        if text:
            events.append(_event("thinking", ts, text=text))
    elif ptype in ("function_call", "custom_tool_call", "local_shell_call"):
        raw_args = payload.get("arguments") or payload.get("input") or ""
        parsed_input: object = raw_args
        if isinstance(raw_args, str):
            try:
                parsed_input = json.loads(raw_args)
            except json.JSONDecodeError:
                parsed_input = {"raw": raw_args}
        name = _as_str(payload.get("name")) or str(ptype)
        if ptype == "local_shell_call":
            parsed_input = payload.get("action") or parsed_input
            name = "shell"
        events.append(_event("tool", ts, tool=name, input=parsed_input,
                             namespace=payload.get("namespace")))
        call_id = _as_str(payload.get("call_id"))
        if call_id:
            call_index[call_id] = len(events) - 1
    elif ptype in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
        output = payload.get("output", "")
        is_error = False
        out_images: list[str] = []
        out_meta: JsonDict = {}
        if isinstance(output, str):
            try:  # many outputs are JSON like {"output": "...", "metadata": {"exit_code": 1}}
                parsed = json.loads(output)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and "output" in parsed:
                metadata = _as_dict(parsed.get("metadata"))
                if metadata.get("exit_code") is not None:
                    out_meta["exitCode"] = metadata["exit_code"]
                if metadata.get("duration_seconds") is not None:
                    out_meta["wallTime"] = metadata["duration_seconds"]
                output = parsed["output"]
            elif isinstance(parsed, list):
                output = parsed
        elif isinstance(output, dict):
            output = output.get("output") or json.dumps(output)
        if isinstance(output, list):  # content blocks, possibly with images
            out_texts, out_images, _ = _flatten_codex_blocks(output)
            output = "\n".join(out_texts)
        if isinstance(output, str):
            # harness wrapper lines (Wall time / exit code / ...) -> structured meta
            wrapper_meta, output = _split_codex_wrapper(output)
            out_meta.update(wrapper_meta)
            mcp = _extract_mcp_content(output)  # MCP results embed content-block JSON
            if mcp:
                output = mcp[0]
                out_images = out_images + mcp[1]
                # some variants carry the wrapper inside the first block
                wrapper_meta, output = _split_codex_wrapper(output)
                out_meta.update(wrapper_meta)
        if out_meta.get("exitCode") not in (0, None):
            is_error = True
        idx = call_index.get(_as_str(payload.get("call_id")) or "")
        if idx is not None:
            _set_output(events[idx], str(output), is_error, out_images, out_meta)
        else:
            events.append(_event("tool", ts, tool="(orphan result)", output=str(output),
                                 isError=is_error or None, images=out_images or None,
                                 outputMeta=out_meta or None))
    elif ptype == "web_search_call":
        events.append(_event("tool", ts, tool="web_search", input=payload.get("action") or {}))


def _codex_spawn_info(session_meta_payload: JsonDict) -> tuple[str | None, str | None]:
    """Returns (parent_thread_id, agent_nickname) for a spawned-subagent rollout."""
    spawn = _as_dict(_as_dict(_as_dict(
        session_meta_payload.get("source")).get("subagent")).get("thread_spawn"))
    parent = _as_str(session_meta_payload.get("parent_thread_id")) or \
        _as_str(spawn.get("parent_thread_id"))
    return parent, _as_str(spawn.get("agent_nickname"))


def _merge_codex_subagents(path: Path, session_id: str | None, events: list[JsonDict],
                           usage: _UsageTally, depth: int) -> None:
    """Codex subagents (spawn_agent tool) land in sibling rollout files that
    point back via session_meta.source.subagent.thread_spawn.parent_thread_id."""
    if not session_id:
        return
    for sibling in sorted(path.parent.glob("rollout-*.jsonl")):
        if sibling.name == path.name:
            continue
        head = next(_iter_jsonl(sibling), None)
        if not head or head.get("type") != "session_meta":
            continue
        payload = _as_dict(head.get("payload"))
        parent, nickname = _codex_spawn_info(payload)
        if parent != session_id:
            continue
        label = nickname or f"subagent …{str(payload.get('id'))[-6:]}"
        child = parse_codex_jsonl(sibling, _depth=depth + 1)
        start = len(events)
        # the spawn_agent tool output names the child's agent_id
        child_id = _as_str(payload.get("id"))
        invoked_at = None
        if child_id:
            for i, prior in enumerate(events[:start]):
                if prior.get("kind") == "tool" and child_id in str(prior.get("output") or ""):
                    if invoked_at is None:
                        invoked_at = i
                    if prior.get("tool") == "spawn_agent":
                        invoked_at = i
                        break
        events.append(_event("info", None, text=f"▶ {label}"))
        for raw_ev in _as_list(child["events"]):
            if isinstance(raw_ev, dict) and raw_ev.get("subtype") != "base_instructions":
                events.append(raw_ev)
        for ev in events[start:]:
            ev["sidechain"] = True
            ev.setdefault("agent", label)
            if invoked_at is not None:
                ev.setdefault("invokedAt", invoked_at)
        usage.add_totals(_as_dict(_as_dict(child["meta"]).get("usage")))


def parse_codex_jsonl(path: Path, _depth: int = 0) -> JsonDict:
    events: list[JsonDict] = []
    call_index: dict[str, int] = {}
    usage = _UsageTally()
    meta: JsonDict = {"source": "codex", "path": str(path)}
    first_ts: str | None = None
    last_ts: str | None = None
    prev_total: JsonDict | None = None  # dedupes re-emitted token_count events
    mcp_calls: dict[str, JsonDict] = {}  # call_id -> {server, wallTime} from mcp_tool_call_end

    for record in _iter_jsonl(path):
        rtype = record.get("type")
        ts = _as_str(record.get("timestamp"))
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        payload = _as_dict(record.get("payload"))
        if not payload:
            continue
        if rtype == "session_meta":
            meta.setdefault("id", payload.get("id"))
            meta.setdefault("cwd", payload.get("cwd"))
            meta.setdefault("version", payload.get("cli_version"))
            git = _as_dict(payload.get("git"))
            if git.get("branch"):
                meta.setdefault("gitBranch", git["branch"])
            instructions: object = payload.get("instructions") or payload.get("base_instructions")
            if isinstance(instructions, dict):
                instructions = instructions.get("text")
            if instructions:
                events.append(_event("system", ts, text=str(instructions),
                                     subtype="base_instructions"))
        elif rtype == "turn_context":
            if payload.get("model"):
                meta["model"] = payload["model"]
            meta.setdefault("cwd", payload.get("cwd"))
        elif rtype == "response_item":
            _codex_payload_to_events(payload, ts, events, call_index)
        elif rtype == "event_msg":
            etype = payload.get("type")
            if etype == "token_count":
                info = _as_dict(payload.get("info"))
                total = _as_dict(info.get("total_token_usage") or info.get("last_token_usage"))
                if total and total != prev_total:  # re-emitted duplicates carry same totals
                    usage.set_openai_total(total)
                    last = _as_dict(info.get("last_token_usage"))
                    if last and events:  # per-request usage -> most recent event
                        events[-1].setdefault("usage", {
                            "input": _as_int(last.get("input_tokens")),
                            "output": _as_int(last.get("output_tokens")),
                            "cacheRead": _as_int(last.get("cached_input_tokens")),
                        })
                if total:
                    prev_total = total
            elif etype == "task_complete":
                duration = payload.get("duration_ms")
                if isinstance(duration, (int, float)):
                    events.append(_event("info", ts, text=f"turn complete in {duration / 1000:.1f}s"))
            elif etype == "turn_aborted":
                reason = _as_str(payload.get("reason")) or "unknown"
                duration = payload.get("duration_ms")
                suffix = f" after {duration / 1000:.1f}s" if isinstance(duration, (int, float)) else ""
                events.append(_event("info", ts, text=f"⚠ turn interrupted ({reason}){suffix}"))
            elif etype == "task_started":
                window = payload.get("model_context_window")
                if isinstance(window, int):
                    meta.setdefault("contextWindow", window)
            elif etype == "mcp_tool_call_end":
                # MCP calls are also logged as function_call/output response_items (rendered
                # above); this event is the only place the serving MCP server is named.
                call_id = _as_str(payload.get("call_id"))
                if call_id:
                    invocation = _as_dict(payload.get("invocation"))
                    span = _as_dict(payload.get("duration"))
                    wall = _as_int(span.get("secs")) + _as_int(span.get("nanos")) / 1e9
                    mcp_calls[call_id] = {
                        "server": _as_str(invocation.get("server")),
                        "wallTime": round(wall, 4) if span else None,
                    }
            # user_message / agent_message duplicate response_item records; skipped.

    for call_id, info in mcp_calls.items():  # attribute MCP calls to their server (+ wall time)
        idx = call_index.get(call_id)
        if idx is None:
            continue
        ev = events[idx]
        if info.get("server"):
            ev.setdefault("namespace", info["server"])
        if info.get("wallTime") is not None:
            out_meta = ev.get("outputMeta")
            if not isinstance(out_meta, dict):
                out_meta = {}
            out_meta.setdefault("wallTime", info["wallTime"])
            if out_meta:
                ev["outputMeta"] = out_meta

    if _depth < 3:
        _merge_codex_subagents(path, _as_str(meta.get("id")), events, usage, _depth)
    meta.update(_finish_meta(events, None, first_ts, last_ts, usage))
    return {"meta": meta, "events": events}


def parse_codex_flat_json(path: Path) -> JsonDict:
    with open(path, encoding="utf-8", errors="replace") as f:
        doc = _as_dict(json.load(f))
    events: list[JsonDict] = []
    call_index: dict[str, int] = {}
    meta: JsonDict = {"source": "codex", "path": str(path)}
    session = _as_dict(doc.get("session"))
    meta["id"] = session.get("id")
    first_ts = _as_str(session.get("timestamp"))
    if session.get("instructions"):
        events.append(_event("system", first_ts, text=session["instructions"],
                             subtype="base_instructions"))
    for item in _as_list(doc.get("items")):
        if isinstance(item, dict):
            _codex_payload_to_events(item, None, events, call_index)
    meta.update(_finish_meta(events, None, first_ts, None, _UsageTally()))
    return {"meta": meta, "events": events}


def _finish_meta(events: list[JsonDict], title: str | None, first_ts: str | None,
                 last_ts: str | None, usage: _UsageTally) -> JsonDict:
    if not title:
        first_user = next((e for e in events if e["kind"] == "user"), None)
        if first_user:
            title = " ".join((_as_str(first_user.get("text")) or "").split())[:120]
    counts: dict[str, int] = {}
    for ev in events:
        kind = str(ev["kind"])
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "title": title or "(empty session)",
        "started": first_ts,
        "ended": last_ts,
        "usage": usage.as_dict(),
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Discovery

def _claude_preview(path: Path) -> dict[str, str | None]:
    """Cheap metadata pass: scan up to 200 records for cwd + first human message."""
    cwd = preview = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i > 200 or (preview and cwd):
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            cwd = cwd or _as_str(record.get("cwd"))
            if preview is None and record.get("type") == "user" and not record.get("isMeta") \
                    and not record.get("isSidechain"):
                content = _as_dict(record.get("message")).get("content")
                if isinstance(content, str) and not content.lstrip().startswith("<"):
                    preview = content
                elif isinstance(content, list):
                    for raw_block in content:
                        block = _as_dict(raw_block)
                        if block.get("type") == "text":
                            text = _as_str(block.get("text")) or ""
                            if text and not text.lstrip().startswith("<"):
                                preview = text
                                break
    return {"cwd": cwd, "preview": preview}


def _codex_jsonl_preview(path: Path) -> dict[str, str | None]:
    cwd = preview = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i > 100 or (preview and cwd):
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            payload = _as_dict(record.get("payload"))
            if not payload:
                continue
            if record.get("type") == "session_meta":
                if _codex_spawn_info(payload)[0]:
                    # spawned-subagent rollout; rendered inside its parent session
                    return {"cwd": None, "preview": None, "subagent": "1"}
                cwd = _as_str(payload.get("cwd"))
            elif record.get("type") == "event_msg" and payload.get("type") == "user_message":
                message = _as_str(payload.get("message")) or ""
                if message and not _is_injected_context(message):
                    preview = preview or message
            elif preview is None and record.get("type") == "response_item" \
                    and payload.get("type") == "message" and payload.get("role") == "user":
                for raw_block in _as_list(payload.get("content")):
                    block = _as_dict(raw_block)
                    if block.get("type") == "input_text":
                        text = _as_str(block.get("text")) or ""
                        if text and not _is_injected_context(text):
                            preview = text
                            break
    return {"cwd": cwd, "preview": preview}


def _codex_flat_preview(path: Path) -> dict[str, str | None]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            doc = _as_dict(json.load(f))
    except (json.JSONDecodeError, OSError):
        return {"cwd": None, "preview": None}
    for raw_item in _as_list(doc.get("items")):
        item = _as_dict(raw_item)
        if item.get("type") == "message" and item.get("role") == "user":
            for raw_block in _as_list(item.get("content")):
                block = _as_dict(raw_block)
                if block.get("type") == "input_text":
                    text = _as_str(block.get("text")) or ""
                    if text and not _is_injected_context(text):
                        return {"cwd": None, "preview": text}
    return {"cwd": None, "preview": None}


_list_cache: dict[str, tuple[float, int, dict[str, str | None]]] = {}


def _cached_preview(path: Path, kind: str) -> dict[str, str | None]:
    stat = path.stat()
    key = str(path)
    hit = _list_cache.get(key)
    if hit and hit[0] == stat.st_mtime and hit[1] == stat.st_size:
        return hit[2]
    fn = {"claude": _claude_preview, "codex_jsonl": _codex_jsonl_preview,
          "codex_flat": _codex_flat_preview}[kind]
    info = fn(path)
    _list_cache[key] = (stat.st_mtime, stat.st_size, info)
    return info


def list_sessions() -> list[JsonDict]:
    sessions: list[JsonDict] = []

    if CLAUDE_ROOT.is_dir():
        for project_dir in sorted(CLAUDE_ROOT.iterdir()):
            if not project_dir.is_dir():
                continue
            for path in project_dir.glob("*.jsonl"):
                try:
                    info = _cached_preview(path, "claude")
                    cwd = info["cwd"] or project_dir.name.replace("-", "/")
                    sessions.append(_session_entry("claude", path, cwd, info["preview"]))
                except OSError:
                    continue  # dangling symlink, unreadable file, etc.

    if CODEX_ROOT.is_dir():
        for path in CODEX_ROOT.rglob("rollout-*.jsonl"):
            try:
                info = _cached_preview(path, "codex_jsonl")
                if info.get("subagent"):
                    continue
                sessions.append(_session_entry("codex", path, info["cwd"], info["preview"]))
            except OSError:
                continue
        for path in CODEX_ROOT.glob("rollout-*.json"):
            try:
                info = _cached_preview(path, "codex_flat")
                sessions.append(_session_entry("codex", path, info["cwd"], info["preview"]))
            except OSError:
                continue

    sessions.sort(key=lambda s: -float(str(s["mtime"])))
    return sessions


def _session_entry(source: str, path: Path, cwd: str | None, preview: str | None) -> JsonDict:
    stat = path.stat()
    project = Path(cwd).name if cwd else "?"
    return {
        "id": encode_session_id(source, path),
        "source": source,
        "project": project,
        "cwd": cwd,
        "preview": " ".join((preview or "").split())[:160] or "(no user message)",
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "filename": path.name,
    }


def load_session(session_id: str) -> JsonDict:
    source, path = decode_session_id(session_id)
    if source == "claude":
        return parse_claude_session(path)
    if path.suffix == ".jsonl":
        return parse_codex_jsonl(path)
    return parse_codex_flat_json(path)

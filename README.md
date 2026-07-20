# Local Trace Visualizer

A local web UI for browsing Claude Code and Codex session traces.

```sh
./serve.sh   # starts on http://localhost:3331/ (kills any previous instance)
```

It reads, live from disk (no copies, no network):

- **Claude Code**: `~/.claude/projects/<project>/<session>.jsonl`
- **Codex**: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (current) and
  `~/.codex/sessions/rollout-*.json` (pre-2025-05 flat format)

Both formats are normalized into one event stream — user / assistant /
thinking / tool (call + result paired) / system — rendered as a conversation
timeline with token usage, model, duration, and a single-select detail level
(`chat` → `+ tools` → `+ thinking` → `all`). Subagent transcripts — Claude
Code's `<session>/subagents/agent-*.jsonl` and Codex's spawned-thread rollouts
(linked via `session_meta.source.subagent.thread_spawn.parent_thread_id`) —
are merged into the parent session and shown as per-agent tab panels on the
right; spawned rollouts are hidden from the session list. JSON tool inputs render as key/value rows with real newlines; images in
tool results and messages render inline (click to zoom). All UI state — source
filter, search, detail level, open session, and the anchored event (click any
bubble to anchor it) — lives in a readable query string
(`?s=claude:<project>/<session>.jsonl&e=8`), so any view is a
shareable/bookmarkable URL. The sidebar lists every session across
both agents, newest first, filterable by source and free text.

For Codex traces, the header shows server-reported last/peak API request tokens
and marks compacted sessions. Rollouts do not expose Codex's separate internal
active-context counter, so the visualizer does not invent one.

- `visualizer/trace_parsers.py` — discovery + format normalization (stdlib only)
- `visualizer/server.py` — `/api/sessions`, `/api/session?id=…`, static frontend
- `visualizer/static/` — dependency-free single-page UI
- `visualizer/test_trace_parsers.py` — parser tests (`uv run pytest visualizer/`)

---

# Claude Code Template

A template for configuring Claude Code hooks to deliver instructions at the most relevant points in the agentic coding lifecycle, instead of overloading CLAUDE.md.

This template covers:

- **UserPromptSubmit** — inject context based on the user's prompt
- **PreToolUse (Bash)** — gate or rewrite shell commands before they run
- **PreToolUse (WebFetch)** — gate web fetches before they run
- **PostToolUse (Bash)** — react to command output (e.g. flag chained `&&` commands)
- **PostToolUse (Edit/Write)** — enforce code-quality rules on edits
- **Notification** — handle Claude Code notifications
- **Stop** — validate the agent's response before it's finalized
- **Skills** — bundled skill (`kaggle`), shared via the `skills/` directory
- **Puppeteer MCP** — preconfigured `.mcp.json` for browser automation

## Layout

The hook business logic lives in `hooks/` at the repository root and is shared
by both agents. `.claude/hooks/` and `.codex/hooks/` contain only thin
wrappers: each parses its agent's hook payload and dispatches to the shared
checks in `hooks/`.

- `hooks/` — per-event checks, notification TTS, and their tests (dependency-free
  where Codex's system python needs them)
- `.claude/hooks/` — Claude Code dispatcher (`process_hooks.py`) and dataclass
  payload models, wired up in `.claude/settings.json`
- `.codex/hooks/` — Codex dispatcher (`process_hooks.py`) and dataclass
  payload models, wired up in `.codex/hooks.json`
- `skills/` — shared skills; `.claude/skills` and `.codex/skills` are symlinks
  to this directory

## Notifications

Notification and Stop events are spoken aloud via Kokoro TTS
(`hooks/notify_kokoro.py`), played through `afplay` on macOS and
`pw-play`/`aplay` on Linux. Loading torch and the Kokoro model costs ~5s, so
the first notification spawns a daemon that keeps the model in memory and
listens on a Unix socket; later notifications forward their message to it in
under 0.2s. The daemon exits after 30 minutes without a message to release
its ~500MB of memory.

To silence a notification mid-speech, add a shush alias to your shell rc
(killing only the player leaves the daemon warm):

```zsh
alias ss='pkill afplay; pkill pw-play; pkill aplay'
```

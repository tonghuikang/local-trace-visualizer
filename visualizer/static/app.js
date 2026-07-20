/* Local trace visualizer frontend. No dependencies. */
"use strict";

const state = {
  sessions: [],
  source: "all",
  query: "",
  view: "all", // chat < tools < thinking < all
  activeId: null,
  eventIdx: null, // anchored event, kept in ?e=
  agentTab: null, // selected subagent panel, kept in ?a=
  expandAll: false, // expand-all toggle, kept in ?x=
  session: null,
  sideGroups: null, // Map<agent label, event elements>
  listLimit: 300,
};

const $ = (sel) => document.querySelector(sel);

// ---------------------------------------------------------------------------
// URL <-> state: every piece of UI state lives in the query string.

function syncUrl(push = false) {
  const p = new URLSearchParams();
  if (state.source !== "all") p.set("source", state.source);
  if (state.query) p.set("q", state.query);
  if (state.view !== "all") p.set("view", state.view);
  if (state.activeId) p.set("s", state.activeId);
  if (state.eventIdx !== null) p.set("e", state.eventIdx);
  if (state.agentTab) p.set("a", state.agentTab);
  if (state.expandAll) p.set("x", "1");
  // Keep ':' and '/' literal so session ids stay readable in the address bar.
  const qs = p.toString().replace(/%3A/gi, ":").replace(/%2F/gi, "/");
  const url = qs ? "?" + qs : location.pathname;
  (push ? history.pushState : history.replaceState).call(history, null, "", url);
}

function readUrl() {
  const p = new URLSearchParams(location.search);
  state.source = p.get("source") || "all";
  state.query = p.get("q") || "";
  state.view = p.get("view") || "all";
  state.eventIdx = p.get("e");
  state.agentTab = p.get("a");
  state.expandAll = p.get("x") === "1";
  return p.get("s");
}

function syncControls() {
  document.querySelectorAll("#filters .chip").forEach(c =>
    c.classList.toggle("active", c.dataset.source === state.source));
  document.querySelectorAll("#view-levels .chip").forEach(c =>
    c.classList.toggle("active", c.dataset.view === state.view));
  $("#search").value = state.query;
}

// ---------------------------------------------------------------------------
// Utilities

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtDay(ts) {
  const d = new Date(ts * 1000);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((today - day) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "short", year: "numeric", month: "short", day: "numeric" });
}

function fmtTokens(n) {
  return (n || 0).toLocaleString("en-US"); // 32,123 — never abbreviated
}

function fmtDuration(startIso, endIso) {
  if (!startIso || !endIso) return null;
  const ms = new Date(endIso) - new Date(startIso);
  if (ms <= 0) return null;
  const m = Math.floor(ms / 60000), s = Math.round((ms % 60000) / 1000);
  if (m >= 60) return `${Math.floor(m / 60)}h ${m % 60}m`;
  return m ? `${m}m ${s}s` : `${s}s`;
}

// Tiny safe markdown renderer: escape first, then transform.
function renderMarkdown(src) {
  const lines = String(src).split("\n");
  const out = [];
  let inCode = false, codeBuf = [], para = [], listStack = null;

  const flushPara = () => {
    if (para.length) { out.push("<p>" + inline(para.join("\n")) + "</p>"); para = []; }
  };
  const flushList = () => {
    if (listStack) { out.push(listStack === "ul" ? "</ul>" : "</ol>"); listStack = null; }
  };
  const inline = (s) => esc(s)
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|\W)\*([^*\n]+)\*(?=\W|$)/g, "$1<i>$2</i>")
    .replace(/\[([^\]\n]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\n/g, "<br>");

  for (const raw of lines) {
    if (raw.startsWith("```") || raw.trim().startsWith("```")) {
      if (inCode) { out.push("<pre><code>" + esc(codeBuf.join("\n")) + "</code></pre>"); codeBuf = []; }
      else { flushPara(); flushList(); }
      inCode = !inCode;
      continue;
    }
    if (inCode) { codeBuf.push(raw); continue; }
    const h = raw.match(/^(#{1,4})\s+(.*)$/);
    const li = raw.match(/^\s*[-*]\s+(.*)$/);
    const ol = raw.match(/^\s*\d+[.)]\s+(.*)$/);
    const bq = raw.match(/^>\s?(.*)$/);
    if (h) { flushPara(); flushList(); out.push(`<h${h[1].length}>` + inline(h[2]) + `</h${h[1].length}>`); }
    else if (li) {
      flushPara();
      if (listStack !== "ul") { flushList(); out.push("<ul>"); listStack = "ul"; }
      out.push("<li>" + inline(li[1]) + "</li>");
    } else if (ol) {
      flushPara();
      if (listStack !== "ol") { flushList(); out.push("<ol>"); listStack = "ol"; }
      out.push("<li>" + inline(ol[1]) + "</li>");
    } else if (bq) { flushPara(); flushList(); out.push("<blockquote>" + inline(bq[1]) + "</blockquote>"); }
    else if (!raw.trim()) { flushPara(); flushList(); }
    else para.push(raw);
  }
  if (inCode && codeBuf.length) out.push("<pre><code>" + esc(codeBuf.join("\n")) + "</code></pre>");
  flushPara(); flushList();
  return out.join("");
}

// Render JSON as labeled key/value rows, recursively: nested objects become
// nested row groups, and multi-line string values get real newlines instead
// of a \n-escaped JSON blob.
function renderJsonBlock(value, depth = 0) {
  if (typeof value === "string") return `<pre>${esc(value)}</pre>`;
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return `<pre>${esc(JSON.stringify(value, null, 2))}</pre>`;
  }
  const rows = Object.entries(value).map(([k, v]) => {
    let valHtml, inline;
    if (v && typeof v === "object" && !Array.isArray(v) && depth < 4 && Object.keys(v).length) {
      inline = false;
      valHtml = `<div class="kv-val">${renderJsonBlock(v, depth + 1)}</div>`;
    } else {
      const text = typeof v === "string" ? v : JSON.stringify(v, null, 2);
      inline = !text.includes("\n") && text.length <= 100;
      valHtml = `<pre class="kv-val">${esc(text)}</pre>`;
    }
    return `<div class="kv ${inline ? "kv-inline" : "kv-block"}">` +
      `<span class="kv-key">${esc(k)}</span>${valHtml}</div>`;
  });
  return `<div class="kv-table${depth ? " kv-nested" : ""}">${rows.join("")}</div>`;
}

// Pretty-print tool output when it is a JSON document.
function renderOutputBlock(output) {
  const trimmed = output.trim();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      return renderJsonBlock(JSON.parse(trimmed));
    } catch { /* not JSON after all */ }
  }
  return `<pre>${esc(output)}</pre>`;
}

// One-line summary of a tool call for the collapsed header.
function toolSummary(ev) {
  const inp = ev.input || {};
  if (typeof inp === "string") return inp.slice(0, 200);
  const cand = inp.command || inp.cmd || inp.file_path || inp.path || inp.pattern ||
    inp.query || inp.url || inp.prompt || inp.description || inp.skill;
  if (Array.isArray(cand)) return cand.join(" ").slice(0, 200);
  if (cand) return String(cand).split("\n")[0].slice(0, 200);
  const keys = Object.keys(inp);
  return keys.length ? keys.map(k => `${k}=${JSON.stringify(inp[k]).slice(0, 40)}`).join(" ").slice(0, 200) : "";
}

// ---------------------------------------------------------------------------
// Session list

async function loadSessions() {
  try {
    const res = await fetch("/api/sessions");
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    state.sessions = data.sessions || [];
    renderSessionList();
  } catch (err) {
    $("#session-list").innerHTML =
      `<div class="empty" style="height:120px;padding:0 16px">Failed to load sessions: ${esc(String(err.message || err))}</div>`;
  }
}

function filteredSessions() {
  const q = state.query.toLowerCase();
  return state.sessions.filter(s => {
    if (state.source !== "all" && s.source !== state.source) return false;
    if (q && !(s.project + " " + s.preview + " " + s.filename + " " + (s.cwd || ""))
      .toLowerCase().includes(q)) return false;
    return true;
  });
}

function renderSessionList() {
  const list = $("#session-list");
  const all = filteredSessions();
  if (!all.length) {
    list.innerHTML = '<div class="empty" style="height:120px">No sessions found.</div>';
    return;
  }
  const sessions = all.slice(0, state.listLimit);
  const frag = document.createDocumentFragment();
  let lastDay = null;
  for (const s of sessions) {
    const day = fmtDay(s.mtime);
    if (day !== lastDay) {
      const dh = document.createElement("div");
      dh.className = "day-header";
      dh.textContent = day;
      frag.appendChild(dh);
      lastDay = day;
    }
    const el = document.createElement("div");
    el.className = "session" + (s.id === state.activeId ? " active" : "");
    el.dataset.id = s.id;
    el.innerHTML = `
      <div class="session-top">
        <span class="badge ${s.source}">${s.source === "claude" ? "CC" : "CX"}</span>
        <span class="session-project" title="${esc(s.cwd || "")}">${esc(s.project)}</span>
        <span class="session-time">${new Date(s.mtime * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
      </div>
      <div class="session-preview">${esc(s.preview)}</div>`;
    el.addEventListener("click", () => openSession(s.id));
    frag.appendChild(el);
  }
  if (all.length > sessions.length) {
    const more = document.createElement("button");
    more.className = "chip";
    more.style.cssText = "display:block;margin:12px auto;";
    more.textContent = `Show more (${(all.length - sessions.length).toLocaleString()} hidden)`;
    more.addEventListener("click", () => { state.listLimit += 300; renderSessionList(); });
    frag.appendChild(more);
  }
  list.innerHTML = "";
  list.appendChild(frag);
}

// ---------------------------------------------------------------------------
// Trace view

async function openSession(id, push = true) {
  state.activeId = id;
  if (push) state.eventIdx = null; // fresh navigation drops the anchor
  syncUrl(push);
  document.querySelectorAll(".session").forEach(el =>
    el.classList.toggle("active", el.dataset.id === id));
  $("#placeholder").hidden = true;
  const trace = $("#trace");
  trace.hidden = false;
  $("#events-main").innerHTML = '<div class="empty" style="height:120px">Parsing trace&hellip;</div>';
  $("#events-side").innerHTML = "";
  $("#side-lane").hidden = true;
  $("#trace-head").innerHTML = "";
  const res = await fetch("/api/session?id=" + encodeURIComponent(id));
  const data = await res.json();
  if (data.error) {
    $("#events-main").innerHTML = `<div class="empty" style="height:120px">Error: ${esc(data.error)}</div>`;
    return;
  }
  state.session = data;
  renderHeader(data.meta);
  renderEvents(data);
}

function renderHeader(meta) {
  const u = meta.usage || {};
  const c = meta.counts || {};
  const parts = [];
  if (meta.cwd) parts.push(`<span class="cwd-link" title="filter sessions to this project">&#128193; <b>${esc(meta.cwd)}</b></span>`);
  if (meta.model) parts.push(`<span>model <b>${esc(meta.model)}</b></span>`);
  if (meta.contextWindow) parts.push(`<span title="model context window">window <b>${fmtTokens(meta.contextWindow)}</b></span>`);
  if (meta.version) parts.push(`<span>v<b>${esc(meta.version)}</b></span>`);
  if (meta.gitBranch) parts.push(`<span>&#8963; <b>${esc(meta.gitBranch)}</b></span>`);
  const dur = fmtDuration(meta.started, meta.ended);
  if (meta.started) parts.push(`<span>${new Date(meta.started).toLocaleString()}${dur ? " &middot; " + dur : ""}</span>`);
  const tok = [];
  // Anthropic reports input EXCLUSIVE of cache reads (additive buckets); OpenAI
  // reports input INCLUSIVE of them (cache-read is a subset). Subtract for codex
  // so "in / cache-read" means the same additive thing for both providers.
  const cacheRead = u.cacheRead || 0;
  const shownInput = meta.source === "codex" ? (u.input || 0) - cacheRead : u.input;
  if (u.input || u.output) tok.push(`in <b>${fmtTokens(shownInput)}</b> / out <b>${fmtTokens(u.output)}</b>`);
  if (meta.source === "codex") {
    if (meta.activeContext) tok.push(
      `<span title="Codex active context used by compaction accounting, including retained encrypted-reasoning estimates">active ctx <b>${fmtTokens(meta.activeContext)}</b></span>`);
    if (meta.peakActiveContext && meta.peakActiveContext !== meta.activeContext) tok.push(
      `<span title="largest Codex active-context count observed before${meta.contextCompacted ? " or during" : ""} compaction">peak ctx <b>${fmtTokens(meta.peakActiveContext)}</b></span>`);
    if (meta.lastRequestTokens) tok.push(
      `<span title="server-reported tokens for the last API request; this is not Codex's compaction counter">request <b>${fmtTokens(meta.lastRequestTokens)}</b></span>`);
  } else {
    // Claude prunes prior-turn thinking; this remains a provider usage summary,
    // not a Codex-style compaction counter.
    const finalTotal = meta.finalTokens ||
      (meta.finalContext ? meta.finalContext + (u.output || 0) : 0);
    if (finalTotal) tok.push(
      `<span title="final request context (${fmtTokens(meta.finalContext)}) + total output (${fmtTokens(u.output)})">final <b>${fmtTokens(finalTotal)}</b></span>`);
  }
  if (cacheRead) tok.push(`cache-read <b>${fmtTokens(cacheRead)}</b>`);
  if (u.cacheCreate) tok.push(`cache-write <b>${fmtTokens(u.cacheCreate)}</b>`);
  if (tok.length) parts.push(`<span title="token usage">&#9679; ${tok.join(" &middot; ")}</span>`);
  parts.push(`<span>${c.user || 0} user &middot; ${c.assistant || 0} assistant &middot; ${c.tool || 0} tools</span>`);
  $("#trace-head").innerHTML =
    `<div class="title">${esc(meta.title || "")}</div><div class="meta-row">${parts.join("")}</div>`;
  const cwdLink = $("#trace-head .cwd-link");
  if (cwdLink) {
    cwdLink.addEventListener("click", () => {
      state.query = meta.cwd;
      state.listLimit = 300;
      syncUrl();
      syncControls();
      renderSessionList();
    });
  }
}

function renderEvents(data) {
  const main = $("#events-main");
  main.innerHTML = "";
  const source = data.meta.source;
  const mainFrag = document.createDocumentFragment();
  const groups = new Map(); // one panel per subagent
  let lastUserTs = null; // non-user events show time relative to the latest user message
  data.events.forEach((ev, i) => {
    if (ev.kind === "user" && ev.ts) {
      lastUserTs = ev.ts;
      ev.tsDisplay = null;
    } else if (ev.ts && lastUserTs) {
      ev.tsDisplay = "+" + (fmtDuration(lastUserTs, ev.ts) || "0s");
    }
    const el = renderEvent(ev, i, source);
    if (state.expandAll) el.classList.add("open");
    if (ev.sidechain) {
      if (ev.kind === "info" && (ev.text || "").startsWith("▶")) return; // tabs replace dividers
      const key = ev.agent || "subagent";
      if (!groups.has(key)) groups.set(key, { els: [], invokedAt: null });
      const g = groups.get(key);
      g.els.push(el);
      if (ev.invokedAt !== undefined) g.invokedAt = ev.invokedAt;
    } else {
      mainFrag.appendChild(el);
    }
  });
  main.appendChild(mainFrag);
  state.sideGroups = groups;
  renderSideLane();
  applyVisibility();
  main.scrollTop = 0;
  if (state.eventIdx !== null) {
    const anchored = document.querySelector(`#trace .event[data-idx="${CSS.escape(String(state.eventIdx))}"]`);
    if (anchored) {
      anchored.classList.add("selected", "open");
      anchored.scrollIntoView({ block: "center" });
    }
  }
}

// One tab per subagent in the side lane; the selected one is kept in ?a=.
function renderSideLane() {
  const groups = state.sideGroups || new Map();
  const lane = $("#side-lane");
  lane.hidden = groups.size === 0;
  if (!groups.size) return;
  const names = [...groups.keys()];
  const total = names.reduce((n, k) => n + groups.get(k).els.length, 0);
  $("#side-lane-title").textContent = `subagents (${names.length}${names.length > 1 ? ` · ${total} events` : ""})`;
  const active = names.includes(state.agentTab) ? state.agentTab : names[0];
  const tabs = $("#side-tabs");
  tabs.innerHTML = "";
  if (names.length > 1) {
    for (const name of names) {
      const b = document.createElement("button");
      b.className = "chip" + (name === active ? " active" : "");
      b.textContent = name;
      b.title = `${groups.get(name).els.length} events`;
      b.addEventListener("click", () => {
        state.agentTab = name;
        syncUrl();
        renderSideLane();
        applyVisibility();
      });
      tabs.appendChild(b);
    }
  }
  const side = $("#events-side");
  side.innerHTML = "";
  const frag = document.createDocumentFragment();
  const group = groups.get(active);
  if (group.invokedAt !== null) {
    const link = document.createElement("button");
    link.className = "invoked-link";
    link.innerHTML = `&#8598; invoked in main thread &middot; jump to call`;
    link.addEventListener("click", () => {
      const target = document.querySelector(
        `#events-main .event[data-idx="${CSS.escape(String(group.invokedAt))}"]`);
      if (!target) return;
      target.classList.add("open");
      if (!target.classList.contains("selected")) selectEvent(target);
      target.scrollIntoView({ block: "center", behavior: "smooth" });
    });
    frag.appendChild(link);
  }
  group.els.forEach(el => frag.appendChild(el));
  side.appendChild(frag);
  side.scrollTop = 0;
}

// Clicking a bubble anchors it: highlighted + linkable via ?e=<index>.
function selectEvent(el) {
  const prev = document.querySelector("#trace .event.selected");
  if (prev && prev !== el) prev.classList.remove("selected");
  const selected = el.classList.toggle("selected");
  state.eventIdx = selected ? el.dataset.idx : null;
  syncUrl();
}

function head(who, ev, summary, extra = "") {
  const side = ev.sidechain && !ev.agent ? '<span class="sidechain-tag">subagent</span>' : "";
  const err = ev.isError ? '<span class="tool-error-tag">error</span>' : "";
  let tok = "";
  if (ev.usage) {
    const u = ev.usage;
    // Badge is "cache-write -> output".
    // Anthropic reports cache writes explicitly; OpenAI never does, so the
    // uncached input (which is what gets newly cached) stands in for it.
    const isClaude = "cacheCreate" in u;
    const written = isClaude ? (u.cacheCreate || 0) : u.input - (u.cacheRead || 0);
    const parts = [];
    if (isClaude) {
      parts.push(`${fmtTokens(u.input)} fresh input`);
      if (u.cacheCreate) parts.push(`${fmtTokens(u.cacheCreate)} cache-write`);
      if (u.cacheRead) parts.push(`${fmtTokens(u.cacheRead)} cache-read`);
    } else {
      parts.push(`${fmtTokens(u.input - (u.cacheRead || 0))} uncached input`);
      if (u.cacheRead) parts.push(`${fmtTokens(u.cacheRead)} cached input`);
    }
    parts.push(`${fmtTokens(u.output)} output`);
    if (u.requestTokens) parts.push(`${fmtTokens(u.requestTokens)} request total`);
    if (u.activeContext) parts.push(`${fmtTokens(u.activeContext)} active context`);
    // comma, not an arrow: write and output are separate counts, not a flow
    tok = `<span class="tok" title="${esc(parts.join(" · "))}">` +
      `${fmtTokens(written)}, ${fmtTokens(u.output)}</span>`;
  }
  const tsTitle = ev.ts ? esc(new Date(ev.ts).toLocaleString()) : "";
  return `<div class="event-head"><span class="who">${who}</span>${extra}${side}${err}` +
    (summary ? `<span class="head-summary">${esc(summary)}</span>` : "") +
    `${tok}<span class="ts" title="${tsTitle}">${esc(ev.tsDisplay || fmtTime(ev.ts))}</span></div>`;
}

// Codex marks MCP calls with a namespace field; Claude Code names them mcp__server__tool.
function mcpInfo(ev) {
  if (ev.namespace) {
    return { name: ev.tool, tag: String(ev.namespace).replace(/^mcp__/, "") };
  }
  if (/^mcp__/.test(ev.tool || "")) {
    const parts = ev.tool.split("__");
    return { name: parts.slice(2).join("__") || ev.tool, tag: parts[1] || "mcp" };
  }
  return null;
}

function renderImages(ev) {
  if (!ev.images || !ev.images.length) return "";
  return ev.images.map(u => `<img class="ev-img" src="${esc(u)}" loading="lazy">`).join("");
}

// Render a message body preserving the original text/image order when the
// source interleaved them (ev.parts); otherwise fall back to text-then-images.
function renderMessageBody(ev) {
  if (!ev.parts || !ev.parts.length) {
    return renderMarkdown(ev.text || "") + renderImages(ev);
  }
  const imgs = ev.images || [];
  return ev.parts.map(p => {
    if (p.type === "image") {
      const u = imgs[p.i];
      return u ? `<img class="ev-img" src="${esc(u)}" loading="lazy">` : "";
    }
    return renderMarkdown(p.text || "");
  }).join("");
}

function renderEvent(ev, i, source) {
  const el = document.createElement("div");
  el.className = `event ${ev.kind}` + (source === "codex" ? " codex" : "") +
    (ev.sidechain ? " sidechain" : "");
  el.dataset.idx = i;

  if (ev.kind === "info") {
    el.innerHTML = `&#9432; ${esc(ev.text || "")}`;
    return el;
  }

  let inner = "";
  if (ev.kind === "user") {
    inner = head("user", ev) +
      `<div class="event-body md">${renderMessageBody(ev)}</div>`;
  } else if (ev.kind === "assistant") {
    const who = source === "codex" ? "codex" : "claude";
    inner = head(who, ev) +
      `<div class="event-body md">${renderMessageBody(ev)}</div>`;
  } else if (ev.kind === "thinking") {
    el.classList.add("collapsible");
    const summary = (ev.text || "").split("\n")[0]
      .replace(/\*\*|__|`/g, "").slice(0, 160);
    inner = head("thinking", ev, summary) +
      `<div class="event-body md">${renderMarkdown(ev.text || "")}</div>`;
  } else if (ev.kind === "system") {
    el.classList.add("collapsible");
    const summary = (ev.subtype ? `[${ev.subtype}] ` : "") + (ev.text || "").split("\n")[0].slice(0, 160);
    inner = head("system", ev, summary) +
      `<div class="event-body">${esc(ev.text || "")}</div>`;
  } else if (ev.kind === "tool") {
    el.classList.add("collapsible");
    const summary = toolSummary(ev);
    let body = "";
    if (ev.input !== undefined && ev.input !== null && Object.keys(ev.input).length !== 0) {
      body += `<div class="tool-section"><div class="tool-label">input</div>${renderJsonBlock(ev.input)}</div>`;
    }
    if (ev.output !== undefined) {
      const m = ev.outputMeta || {};
      const bits = [];
      if (m.wallTime !== undefined) bits.push(`${m.wallTime}s`);
      if (m.exitCode !== undefined) bits.push(`exit ${m.exitCode}`);
      if (m.tokens !== undefined) bits.push(`${fmtTokens(m.tokens)} tokens`);
      if (m.chunkId) bits.push(`chunk ${esc(String(m.chunkId))}`);
      const metaStr = bits.length ? ` &middot; ${bits.join(" &middot; ")}` : "";
      body += `<div class="tool-section${ev.isError ? " is-error" : ""}">` +
        `<div class="tool-label">output${ev.isError ? " (error)" : ""}${metaStr}</div>` +
        renderOutputBlock(ev.output) + `</div>`;
    }
    if (ev.images && ev.images.length) {
      body += `<div class="tool-section"><div class="tool-label">images</div>${renderImages(ev)}</div>`;
    }
    if (ev.truncated) body += `<div class="truncated-note">&#9888; truncated for display</div>`;
    const mcp = mcpInfo(ev);
    const tag = mcp ? `<span class="mcp-tag">MCP&thinsp;&middot;&thinsp;${esc(mcp.tag)}</span>` : "";
    inner = head(esc(mcp ? mcp.name : (ev.tool || "tool")), ev, summary, tag) +
      `<div class="event-body">${body}</div>`;
  }

  el.innerHTML = `<div class="bubble">${inner}</div>`;
  if (el.classList.contains("collapsible")) {
    el.querySelector(".event-head").addEventListener("click", () => el.classList.toggle("open"));
  }
  el.querySelectorAll(".ev-img").forEach(img => {
    const downscaled = () => img.naturalWidth > img.getBoundingClientRect().width + 1;
    img.addEventListener("load", () => { img.style.cursor = downscaled() ? "zoom-in" : "default"; });
    img.addEventListener("click", () => {
      if (!downscaled()) return; // already shown at full resolution
      // data: URIs can't be opened directly in a new tab; go through a blob URL
      fetch(img.src).then(res => res.blob()).then(blob => {
        window.open(URL.createObjectURL(blob), "_blank");
      });
    });
  });
  el.querySelector(".bubble").addEventListener("click", (evt) => {
    if (evt.target.closest("a, .ev-img") || window.getSelection().toString()) return;
    selectEvent(el);
  });
  return el;
}

// ---------------------------------------------------------------------------
// Controls

const VIEW_LEVELS = { chat: 0, tools: 1, thinking: 2, all: 3 };

function applyVisibility() {
  const level = VIEW_LEVELS[state.view] ?? 3;
  document.querySelectorAll("#trace .event").forEach(el => {
    let hide = false;
    if (el.classList.contains("tool") && level < 1) hide = true;
    if (el.classList.contains("thinking") && level < 2) hide = true;
    if (el.classList.contains("system") && level < 3) hide = true;
    el.classList.toggle("hidden-kind", hide);
  });
}

function init() {
  document.querySelectorAll("#filters .chip").forEach(chip => {
    chip.addEventListener("click", () => {
      state.source = chip.dataset.source;
      state.listLimit = 300;
      syncUrl();
      syncControls();
      renderSessionList();
    });
  });
  $("#search").addEventListener("input", (e) => {
    state.query = e.target.value;
    state.listLimit = 300;
    syncUrl();
    renderSessionList();
  });
  document.querySelectorAll("#view-levels .chip").forEach(chip => {
    chip.addEventListener("click", () => {
      state.view = chip.dataset.view;
      syncUrl();
      syncControls();
      applyVisibility();
    });
  });
  $("#expand-all").addEventListener("click", () => {
    state.expandAll = true;
    syncUrl();
    document.querySelectorAll("#trace .collapsible").forEach(el => el.classList.add("open"));
  });
  $("#collapse-all").addEventListener("click", () => {
    state.expandAll = false;
    syncUrl();
    document.querySelectorAll("#trace .collapsible").forEach(el => el.classList.remove("open"));
  });
  window.addEventListener("popstate", () => {
    const sid = readUrl();
    syncControls();
    renderSessionList();
    if (sid && sid !== state.activeId) openSession(sid, false);
    applyVisibility();
  });

  const sid = readUrl();
  syncControls();
  loadSessions().then(() => { if (sid) openSession(sid, false); });
}

init();

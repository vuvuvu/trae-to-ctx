"""Export Trae ai-agent sessions from a decrypted database.db to Markdown or to ctx-history-jsonl-v2.

Usage:
  trae-to-ctx export <decrypted.db> <out_dir> --sample
  trae-to-ctx export <decrypted.db> <out_dir> --session <session_id> [--session ...]
  trae-to-ctx export <decrypted.db> <out.jsonl> --jsonl (--all | --sample | --session <id>) [--fingerprint sha256:<hex>]
  trae-to-ctx markdown <export.jsonl> <out_dir>

Source of truth is history_v2 (server_history_info is a synced duplicate of it).
Inline chats without history_v2 rows come from chat_message_general / chat_message_chat.
Markdown: tool results are left out, tool calls become one compact line per assistant step,
and all text passes through redact(). JSONL: full, unredacted content (see docs/ctx-mapping.md).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sqlite3

SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{20,}|gh[ousr]_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9\-]{10,}"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|\b[A-Fa-f0-9]{32,}\b"
    # long opaque strings: must mix upper, lower and digits (spares snake_case file and test names)
    r"|\b(?=[A-Za-z0-9+_\-]*[A-Z])(?=[A-Za-z0-9+_\-]*[a-z])(?=[A-Za-z0-9+_\-]*[0-9])[A-Za-z0-9+_\-]{32,}={0,2})"
)
SECRET_KV = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:api[_-]?key|token|secret|password|passwd|bearer|authorization|client[_-]?secret)[A-Z0-9_]*)"
    r"(\s*[:=]\s*['\"]?)(?!\d+\b)([^\s'\"`,;]{6,})"
)
USER_INPUT = re.compile(r"<user_input>([\s\S]*?)</user_input>")
INJECTED = re.compile(r"<(system-reminder|rules|user_rules|available_terminal|ide_context)[^>]*>[\s\S]*?</\1>")
ARG_KEYS = ("file_path", "path", "target_file", "files", "dir_path", "command", "pattern", "query",
            "information_request", "url", "preview_url", "description", "skill", "agent_id", "prompt", "name")
SKIP_ARGS = {"TodoWrite", "todo_write"}
# Trae's inline reference tags, e.g. <mcfile name="x" path="/a/x"></mcfile> or <mcfile>a/x</mcfile>
MC_ATTR = re.compile(r"<(mc\w+)\b([^>]*)>(.*?)</\1>", re.S)


def mc_tags(s):
    def rep(m):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2)))
        val = attrs.get("path") or attrs.get("url") or attrs.get("name") or m.group(3).strip()
        if m.group(1) == "mcreference" and not val:
            return ""
        return f"`{val}`" if val else ""
    return MC_ATTR.sub(rep, s)


def _looks_like_name(v):
    """True for slugs like AD-2026-03-02-some-plan-name: every -/_ segment is uniform case or digits."""
    segs = [x for x in re.split(r"[-_]", v) if x]
    return len(segs) >= 3 and all(x.isdigit() or x.islower() or x.isupper() for x in segs)


def redact(s):
    # "*_tokens" keys (prompt_tokens, max_tokens) are counts, not credentials
    s = SECRET_KV.sub(lambda m: m.group(0) if m.group(1).lower().endswith("tokens")
                      else m.group(1) + m.group(2) + "[REDACTED]", s)
    return SECRET.sub(lambda m: m.group(0) if _looks_like_name(m.group(0)) else "[REDACTED]", s)


def ts(sec):
    if not sec:
        return "?"
    sec = int(sec)
    return dt.datetime.fromtimestamp(sec / (1000 if sec > 1e11 else 1)).strftime("%Y-%m-%d %H:%M")


def slug(s, n=50):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s or "untitled").strip("-").lower()
    return (s[:n].rstrip("-")) or "untitled"


def text_parts(content):
    if isinstance(content, str):
        return [content], 0
    out, images = [], 0
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text" and c.get("text"):
            out.append(c["text"])
        elif c.get("type") == "image_url":
            images += 1
    return out, images


def user_text(content):
    parts, images = text_parts(content)
    joined = "\n".join(parts)
    found = USER_INPUT.findall(joined)
    if found:
        txt = "\n\n".join(f.strip() for f in found)
    else:
        txt = INJECTED.sub("", joined).strip()
        if txt.startswith("<"):  # some other injected wrapper we don't know; drop it
            txt = ""
    if images:
        txt = (txt + "\n\n" if txt else "") + " ".join(["[image]"] * images)
    return txt.strip()


def tool_label(t):
    fn = t.get("function_call") or t.get("function") or {}
    name = fn.get("name", "?")
    if name in SKIP_ARGS:
        return name
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = None
    if isinstance(args, dict):
        if isinstance(args.get("command"), str) and isinstance(args.get("args"), list):
            args = dict(args, command=" ".join([args["command"]] + [str(x) for x in args["args"]]))
        for k in ARG_KEYS:
            v = args.get(k)
            if isinstance(v, list) and v:  # e.g. view_files {"files": [{"file_path": ...}, ...]}
                first = v[0]
                if isinstance(first, dict):
                    first = first.get("file_path") or first.get("path") or ""
                v = f"{first}" + (f" (+{len(args[k]) - 1})" if len(args[k]) > 1 else "") if first else ""
            if isinstance(v, str) and v.strip():
                v = v.strip().split("\n", 1)[0]
                return f"{name} `{v[:90]}{'…' if len(v) > 90 else ''}`"
    return name


class Exporter:
    def __init__(self, db):
        self.con = sqlite3.connect(pathlib.Path(os.path.abspath(db)).as_uri() + "?mode=ro&immutable=1", uri=True)
        self.con.row_factory = sqlite3.Row
        q = self.con.execute
        self.projects = {r["project_id"]: r["absolute_path"] for r in q("SELECT project_id, absolute_path FROM project")}
        agents = {r["agent_id"]: r["name"] for r in q("SELECT agent_id, name FROM agent")}
        self.child_runs = {r["agent_run_id"]: agents.get(r["agent_id"]) or "sub-agent"
                           for r in q("SELECT agent_run_id, agent_id FROM agent_run "
                                      "WHERE parent_run_id IS NOT NULL AND parent_run_id != ''")}

    def session(self, sid):
        r = self.con.execute("SELECT * FROM chat_session WHERE session_id=?", (sid,)).fetchone()
        if r:
            return dict(r)
        # history rows whose chat_session row is gone (session deleted in the UI)
        lo, hi = self.con.execute("SELECT min(created_at), max(created_at) FROM history_v2 WHERE session_id=?",
                                  (sid,)).fetchone()
        return {"session_id": sid, "session_title": "(orphaned: no chat_session row)", "project_id": None,
                "created_at": lo, "updated_at": hi, "session_type": "orphan"}

    def blocks(self, sid):
        """Yield (kind, text, meta) in order. kind: user | assistant | tools | sub_start | sub_end."""
        rows = self.con.execute(
            "SELECT messages, agent_run_id, deleted_at, message_id, agent_type FROM history_v2 "
            "WHERE session_id=? ORDER BY id", (sid,)
        ).fetchall()
        stats = {"rows": len(rows), "deleted_rows": 0, "models": set(), "tool_calls": 0, "sub_agents": 0,
                 "handoffs": 0, "turn_status": {}}
        if not rows:
            yield from self.inline_blocks(sid, stats)
            yield "stats", stats, None
            return
        turn_status = {r["reply_to_message_id"]: (r["turn_status"], r["error_message"]) for r in self.con.execute(
            "SELECT reply_to_message_id, turn_status, error_message FROM chat_turn WHERE session_id=?", (sid,))}
        current_sub, last_user, last_task, handoff_pending = None, None, None, False
        for r in rows:
            if r["deleted_at"]:
                stats["deleted_rows"] += 1
                continue
            sub = r["agent_run_id"] in self.child_runs
            if sub != bool(current_sub):
                if current_sub:
                    yield "sub_end", "", None
                    current_sub = None
                if sub:
                    stats["sub_agents"] += 1
                    current_sub = last_task or "sub-agent"
                    yield "sub_start", current_sub, None
            try:
                raw = json.loads(r["messages"]).get("raw_messages", [])
            except Exception:
                continue
            for m in raw:
                role = m.get("role")
                if role == "user":
                    if sub:
                        # a sub-agent's "user" message is the parent's task prompt
                        t = user_text(m.get("content"))
                        if t:
                            yield "sub_task", t, None
                        continue
                    t = user_text(m.get("content"))
                    if not t:
                        continue
                    if t == last_user and handoff_pending:
                        # old builder flow: run_agent re-sends the same prompt to the next internal agent.
                        # A repeated prompt without a preceding run_agent is a real repeat (e.g. "Continue").
                        handoff_pending = False
                        stats["handoffs"] += 1
                        yield "handoff", r["agent_type"] or "agent", None
                        continue
                    handoff_pending = False
                    last_user = t
                    st = turn_status.get(r["message_id"])
                    yield "user", t, st
                elif role == "assistant":
                    model = (m.get("extra_info") or {}).get("model_name") or (m.get("extra_info") or {}).get("model")
                    if model:
                        stats["models"].add(model)
                    parts, _ = text_parts(m.get("content"))
                    t = "\n\n".join(p.strip() for p in parts if p.strip())
                    if t:
                        yield "assistant", t, None
                    tcs = m.get("tool_calls") or []
                    if tcs:
                        stats["tool_calls"] += len(tcs)
                        labels = [tool_label(tc) for tc in tcs]
                        for lb in labels:
                            if lb.startswith(("Task ", "run_agent ")):
                                last_task = lb
                            if lb.split(" ", 1)[0] == "run_agent":
                                handoff_pending = True
                        yield "tools", labels, None
        if current_sub:
            yield "sub_end", "", None
        yield "stats", stats, None

    def inline_blocks(self, sid, stats):
        rows = self.con.execute(
            "SELECT cm.message_role, cm.message_type, g.content AS gc, c.content AS cc FROM chat_message cm "
            "LEFT JOIN chat_message_general g ON g.message_id = cm.message_id "
            "LEFT JOIN chat_message_chat c ON c.message_id = cm.message_id "
            "WHERE cm.session_id=? AND (cm.deleted_at IS NULL OR cm.deleted_at = 0) ORDER BY cm.message_index, cm.id",
            (sid,)).fetchall()
        stats["rows"] = len(rows)
        stats["source"] = "chat_message (inline chat)"
        for r in rows:
            if r["message_role"] == "user" and r["gc"]:
                try:
                    items = json.loads(r["gc"])
                    t = "\n".join(i.get("text_content", "") for i in items if isinstance(i, dict))
                except Exception:
                    t = r["gc"]
                t = user_text(t) if "<" in t else t.strip()
                if t:
                    yield "user", t, None
            elif r["message_role"] == "assistant" and r["cc"]:
                try:
                    t = json.loads(r["cc"]).get("content") or ""
                except Exception:
                    t = r["cc"]
                if t.strip():
                    yield "assistant", t.strip(), None

    def render(self, sid):
        s = self.session(sid)
        return render_md(s, self.projects.get(s["project_id"], "(unknown project)"), self.blocks(sid))

    def write(self, sid, out_dir):
        md, turns, project, title = self.render(sid)
        return write_md(out_dir, self.session(sid), md, turns, project, title)

    def sample_ids(self):
        """A deliberately varied sample: different eras, agent types, sizes and edge cases."""
        q = lambda sql: [r[0] for r in self.con.execute(sql).fetchall()]
        picks = {
            "oldest session": q("SELECT session_id FROM chat_session WHERE session_id IN (SELECT session_id FROM history_v2) ORDER BY created_at LIMIT 1"),
            "largest session": q("SELECT session_id FROM history_v2 GROUP BY session_id ORDER BY count(*) DESC LIMIT 1"),
            "has sub-agents": q("SELECT h.session_id FROM history_v2 h JOIN agent_run a ON a.agent_run_id=h.agent_run_id WHERE a.parent_run_id!='' GROUP BY h.session_id ORDER BY count(*) LIMIT 1"),
            "has deleted rows": q("SELECT session_id FROM history_v2 WHERE deleted_at IS NOT NULL AND deleted_at!=0 GROUP BY session_id LIMIT 1"),
            "inline chat": q("SELECT DISTINCT session_id FROM chat_message WHERE session_id NOT IN (SELECT session_id FROM history_v2) LIMIT 1"),
            "has image input": q("SELECT session_id FROM history_v2 WHERE messages LIKE '%\"image_url\"%' LIMIT 1"),
            "uses MCP tools": q("SELECT session_id FROM history_v2 WHERE messages LIKE '%\"name\":\"mcp_%' GROUP BY session_id ORDER BY count(*) LIMIT 1"),
            "solo / ui_builder agent": q("SELECT session_id FROM history_v2 WHERE agent_type IN ('solo_coder','ui_builder_coding') GROUP BY session_id ORDER BY count(*) LIMIT 1"),
            "small chat-mode session": q("SELECT session_id FROM history_v2 WHERE agent_type IN ('chat','chat_v3') GROUP BY session_id HAVING count(*) BETWEEN 4 AND 30 LIMIT 1"),
            "most recent session": q("SELECT session_id FROM history_v2 GROUP BY session_id ORDER BY max(created_at) DESC LIMIT 1"),
            "mid-size custom_v3 (2026)": q("SELECT session_id FROM history_v2 WHERE agent_type='custom_v3' GROUP BY session_id HAVING count(*) BETWEEN 100 AND 400 ORDER BY session_id LIMIT 1"),
        }
        seen, out = set(), []
        for why, ids in picks.items():
            for sid in ids:
                if sid and sid not in seen:
                    seen.add(sid)
                    out.append((why, sid))
        return out


def write_md(out_dir, s, md, turns, project, title):
    if turns == 0:
        return None
    d = os.path.join(out_dir, slug(os.path.basename(project.rstrip("/")) or "unknown", 40))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{ts(s['created_at'])[:10]}_{slug(title)}_{s['session_id'][-6:]}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path


def render_md(s, project, blocks):
    """Render one session from a block stream (from the database or from the JSONL export)."""
    sid = s["session_id"]
    body, tools_buf, stats = [], [], {}
    in_sub = False
    turns = 0

    def q(text):  # indent sub-agent content as a blockquote
        return "\n".join(("> " + ln) if ln else ">" for ln in text.split("\n")) if in_sub else text

    def flush_tools():
        if tools_buf:
            shown = tools_buf[:12]
            more = f" … +{len(tools_buf) - 12} more" if len(tools_buf) > 12 else ""
            add(f"_Tools ({len(tools_buf)}): " + " · ".join(shown) + more + "_", "")
            tools_buf.clear()

    pending_status = None
    failed = 0

    def flush_status():
        nonlocal pending_status, failed
        if pending_status and pending_status[0] not in (None, "completed"):
            status, err = pending_status
            msg = ""
            if err:
                try:
                    msg = json.loads(err).get("message") or ""
                except Exception:
                    msg = err
            failed += status == "failed"
            body.append(f"_Turn {status}" + (f": {msg[:160]}" if msg else "") + "_")
            body.append("")
        pending_status = None

    def add(*lines):  # blank lines stay inside the quote when in a sub-agent block
        body.extend(q(ln) if in_sub else ln for ln in lines)

    for kind, val, meta in blocks:
        if kind == "tools":
            tools_buf.extend(val)
            continue
        if kind == "stats":
            stats = val
            continue
        flush_tools()
        if kind == "user":
            flush_status()
            turns += 1
            body += [f"## User · turn {turns}", "", mc_tags(val), ""]
            pending_status = meta
        elif kind == "handoff":
            body += [f"_(prompt handed to internal agent: {val})_", ""]
        elif kind == "assistant":
            add("**Assistant**", "", mc_tags(val), "")
        elif kind == "sub_start":
            in_sub = True
            add(f"**Sub-agent** ({val})", "")
        elif kind == "sub_task":
            t = re.sub(r"\s+", " ", val)
            add("_Task:_ " + t[:300] + ("…" if len(t) > 300 else ""), "")
        elif kind == "sub_end":
            in_sub = False
            body.append("")
    flush_tools()
    flush_status()

    title = s["session_title"] or "(untitled)"
    fm = [
        "---",
        f"title: {json.dumps(title)}",
        f"session_id: {sid}",
        f"project: {json.dumps(project)}",
        f"created: {ts(s['created_at'])}",
        f"updated: {ts(s['updated_at'])}",
        f"session_type: {s['session_type']}",
        f"user_turns: {turns}",
        f"tool_calls: {stats.get('tool_calls', 0)}",
        f"sub_agent_runs: {stats.get('sub_agents', 0)}",
        f"failed_turns: {failed}",
        f"agent_handoffs: {stats.get('handoffs', 0)}",
        f"models: {json.dumps(sorted(stats.get('models', [])))}",
        f"source: {stats.get('source', 'history_v2')} ({stats.get('rows', 0)} rows, {stats.get('deleted_rows', 0)} deleted skipped)",
        "---",
        "",
        f"# {title}",
        "",
    ]
    return redact("\n".join(fm + body)), turns, project, title


# ---------------------------------------------------------------------------
# ctx-history-jsonl-v2 export (see docs/ctx-mapping.md)
# ---------------------------------------------------------------------------

from . import __version__

JSONL_PRODUCER = f"trae-to-ctx/{__version__}"
MAPPING_REVISION = 1
LIVE_DB = "~/Library/Application Support/Trae/ModularData/ai-agent/database.db"
TOOL_STATUS = re.compile(r"<toolcall_status>([\s\S]*?)</toolcall_status>")
TOOL_ERROR = re.compile(r"<toolcall_error_message>([\s\S]*?)</toolcall_error_message>")
# tool-call argument keys that name files (directories are not file touches)
FILE_ARG_KEYS = ("file_path", "filePath", "path", "rename_file_path", "product_document_content_path")
FILE_LIST_KEYS = ("files", "file_paths", "document_paths")
DIR_TOOLS = {"list_dir", "LS", "ls"}
# history_v2 columns kept as row metadata (messages is mapped; summaries become events)
ROW_META = ("id", "history_v2_id", "message_id", "agent_type", "content_source", "agent_run_id", "token_usage",
            "summarized_above", "created_at", "updated_at", "compress_token_usage", "micro_compacted",
            "micro_compact_strategy", "truncated_above")


def iso(sec):
    sec = int(sec or 0)
    if sec > 1e11:
        sec //= 1000
    return dt.datetime.fromtimestamp(sec, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def jline(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def nonempty(d, skip=()):
    return {k: v for k, v in d.items() if k not in skip and v not in (None, "", [], {})}


def joined_text(content):
    """Text parts joined by newlines, plus a part layout that lets the original list be rebuilt."""
    if isinstance(content, str):
        return content, 0, None
    texts, layout, images = [], [], 0
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text":
            texts.append(c.get("text") or "")
            layout.append(len(c.get("text") or ""))
        elif isinstance(c, dict) and c.get("type") == "image_url":
            images += 1
            layout.append("image_url")
        else:
            layout.append(c.get("type") if isinstance(c, dict) else type(c).__name__)
    simple = layout == [len(texts[0])] if len(texts) == 1 else not layout
    return "\n".join(texts), images, (None if simple else layout)


def file_refs(name, args):
    if not isinstance(args, dict):
        return []
    out = []
    for k in FILE_ARG_KEYS:
        if k == "path" and name in DIR_TOOLS:
            continue
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v)
    for k in FILE_LIST_KEYS:
        for v in args.get(k) or []:
            if isinstance(v, dict):
                v = v.get("file_path") or v.get("path")
            if isinstance(v, str) and v.strip():
                out.append(v)
    return list(dict.fromkeys(out))


class SessionBuf:
    """Events and file references for one ctx session, with its own event_index counter."""

    def __init__(self, record):
        self.record = record
        self.events, self.refs = [], []

    def add(self, event, files=()):
        idx = len(self.events)
        event["event_index"] = idx
        self.events.append(event)
        for v in files:
            self.refs.append({"reference_index": len(self.refs), "event_index": idx, "value": v,
                              "occurred_at": event["occurred_at"]})
        return event


class JsonlExporter(Exporter):
    def __init__(self, db, source_id):
        super().__init__(db)
        self.source_id = source_id
        q = self.con.execute
        self.run_parent = {r["agent_run_id"]: r["parent_run_id"] for r in q(
            "SELECT agent_run_id, parent_run_id FROM agent_run WHERE parent_run_id IS NOT NULL AND parent_run_id != ''")}
        self.runs = {r["agent_run_id"]: dict(r) for r in q("SELECT * FROM agent_run")}
        self.report = {"sessions": {}, "messages": {}, "events": {}, "skipped": {}}

    def _count(self, section, key, n=1):
        d = self.report[section]
        d[key] = d.get(key, 0) + n

    def _event(self, sid, eid, etype, at, payload, role=None, meta=None):
        e = {"record_type": "event", "source_id": self.source_id, "provider_session_id": sid,
             "event_index": None, "event_id": eid, "event_type": etype}
        if role:
            e["role"] = role
        e["occurred_at"] = iso(at)
        e["payload"] = payload
        if meta:
            e["metadata"] = meta
        self._count("events", etype)
        return e

    def session_ids(self):
        q = self.con.execute
        ids = [r[0] for r in q("SELECT session_id FROM (SELECT session_id, min(created_at) c FROM history_v2 "
                               "GROUP BY session_id UNION SELECT session_id, min(created_at) FROM chat_message "
                               "WHERE session_id NOT IN (SELECT session_id FROM history_v2) GROUP BY session_id) "
                               "ORDER BY c, session_id")]
        return ids

    def build(self, sid):
        """Return the list of SessionBuf for one Trae session (primary first), or None if it has no user turn."""
        s = self.session(sid)
        cwd = self.projects.get(s.get("project_id"))
        smeta = {"trae_chat_session": nonempty(s)} if s.get("session_type") != "orphan" else {
            "trae_chat_session": None, "orphaned": True}
        rec = {"record_type": "session", "source_id": self.source_id, "provider_session_id": sid}
        if cwd:
            rec["cwd"] = cwd
        rec.update({"started_at": iso(s.get("created_at")), "agent_scope": "primary", "session_relationship": "root"})
        primary = SessionBuf(rec)
        before = json.loads(json.dumps(self.report))
        rows = self.con.execute("SELECT * FROM history_v2 WHERE session_id=? ORDER BY id", (sid,)).fetchall()
        if rows:
            bufs, user_turns, models, last_at = self._history(sid, rows, primary, cwd)
        else:
            bufs, user_turns, models, last_at = self._inline(sid, primary)
        if not user_turns:
            # nothing from this session is written: undo its counts and report them as one skip reason
            n = sum(self.report["messages"].values()) - sum(before["messages"].values())
            self.report = before
            self._count("skipped", "session with no user turn")
            self._count("skipped", "messages in sessions with no user turn", n)
            return None
        rec["ended_at"] = iso(max(last_at, s.get("updated_at") or 0))
        if rows:
            smeta["trae_row_counts"] = {"table": "history_v2", "rows": len(rows),
                                        "deleted": sum(1 for r in rows if r["deleted_at"])}
        else:
            n, d = self.con.execute("SELECT count(*), sum(CASE WHEN deleted_at THEN 1 ELSE 0 END) FROM chat_message "
                                    "WHERE session_id=?", (sid,)).fetchone()
            smeta["trae_row_counts"] = {"table": "chat_message", "rows": n, "deleted": d or 0}
        smeta["mapping_revision"] = MAPPING_REVISION
        rec["metadata"] = smeta
        info = nonempty({"kind": "session_info", "title": s.get("session_title"), "session_type": s.get("session_type"),
                         "project": cwd, "models": sorted(models)})
        head = self._event(sid, "session:info", "notice", s.get("created_at") or 0, info)
        primary.events.insert(0, head)
        for i, e in enumerate(primary.events):  # re-number after inserting the header
            e["event_index"] = i
        for r in primary.refs:
            r["event_index"] += 1
        self._count("sessions", "primary")
        self._count("sessions", "subagent", len(bufs) - 1)
        return bufs

    def _turn_meta(self, t):
        """The chat_turn row, with its context JSON parsed and private or oversized parts omitted (see the plan doc)."""
        t = nonempty(t)
        try:
            ctx = json.loads(t.get("context") or "")
        except Exception:
            return t
        if not isinstance(ctx, dict):
            return t
        omitted = {}

        def snap(f):
            # IDE editor snapshot of a workspace file: keep path and identity, not the file body
            if isinstance(f, dict) and isinstance(f.get("content"), str):
                body = f.pop("content").encode("utf-8")
                f["content_omitted"] = {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                omitted["ide_file_snapshots"] = omitted.get("ide_file_snapshots", 0) + 1
                self._count("skipped", "IDE file snapshot body in turn context (path, size and sha256 kept)")

        rc = ctx.get("render_context")
        if isinstance(rc, dict):
            snap(rc.get("current_file"))
            for f in rc.get("hash_files") or []:
                snap(f)
        ui = ctx.get("user_info")
        if isinstance(ui, dict) and ui.pop("token", None):
            omitted["user_info.token"] = "trae account credential"
            self._count("skipped", "Trae account token in turn context")
        mi = (ctx.get("persist_user_message_context") or {}).get("model_info")
        if isinstance(mi, dict) and mi.pop("encrypted_model_params", None):
            omitted["model_info.encrypted_model_params"] = "encrypted provider blob"
            self._count("skipped", "encrypted model params in turn context")
        t["context"] = ctx
        if omitted:
            t["context_omitted"] = omitted
        return t

    def _history(self, sid, rows, primary, cwd):
        turns = {r["reply_to_message_id"]: dict(r) for r in self.con.execute(
            "SELECT * FROM chat_turn WHERE session_id=?", (sid,))}
        bufs, children = [primary], {}
        models, user_turns, last_at = set(), 0, 0
        pending_turn = None  # (turn row, at) waiting for a status notice at the end of the turn
        last_user, handoff_pending = None, False
        last_acc = {}
        status_written = set()  # a few turns are linked from two history rows that share a message_id

        def close_turn():
            nonlocal pending_turn
            if (pending_turn and pending_turn[0].get("turn_status") not in (None, "", "completed")
                    and pending_turn[0]["turn_id"] not in status_written):
                status_written.add(pending_turn[0]["turn_id"])
                t, at = pending_turn
                err = t.get("error_message") or ""
                try:
                    err = json.loads(err).get("message") or err
                except Exception:
                    pass
                primary.add(self._event(sid, f"turn:{t['turn_id']}:status", "notice", t.get("updated_at") or at,
                                        nonempty({"kind": "turn_status", "turn_status": t["turn_status"],
                                                  "error": err})))
            pending_turn = None

        for r in rows:
            r = dict(r)
            if r["deleted_at"]:
                self._count("skipped", "history_v2 row deleted in Trae")
                continue
            at = r["created_at"] or 0
            last_at = max(last_at, r["updated_at"] or at)
            run = r["agent_run_id"]
            if run in self.run_parent:
                buf = children.get(run)
                if buf is None:
                    parent_run = self.run_parent[run]
                    parent_sid = f"{sid}:run:{parent_run}" if parent_run in self.run_parent else sid
                    csid = f"{sid}:run:{run}"
                    crec = {"record_type": "session", "source_id": self.source_id, "provider_session_id": csid,
                            "parent_provider_session_id": parent_sid, "root_provider_session_id": sid,
                            "session_relationship": "delegated"}
                    if cwd:
                        crec["cwd"] = cwd
                    crec.update({"started_at": iso(self.runs[run].get("created_at") or at), "agent_scope": "subagent",
                                 "metadata": {"trae_agent_run": nonempty(self.runs[run]),
                                              "agent_name": self.child_runs.get(run),
                                              "mapping_revision": MAPPING_REVISION}})
                    buf = children[run] = SessionBuf(crec)
                    buf.edge = {"record_type": "edge", "source_id": self.source_id,
                                "from_provider_session_id": parent_sid, "to_provider_session_id": csid,
                                "edge_id": f"run:{run}", "relationship": "delegated", "occurred_at": crec["started_at"]}
                    bufs.append(buf)
            else:
                buf = primary
            try:
                msgs = json.loads(r["messages"])
            except Exception:
                self._count("skipped", "history_v2 row with unreadable messages JSON")
                continue
            row_meta = nonempty({k: r[k] for k in ROW_META})
            row_meta.update(nonempty({k: v for k, v in msgs.items() if k != "raw_messages"}))
            first = True

            def emit(e, files=()):
                nonlocal first
                if first:
                    e.setdefault("metadata", {})["trae_row"] = row_meta
                    first = False
                return buf.add(e, files)

            h = r["history_v2_id"]
            for i, m in enumerate(msgs.get("raw_messages") or []):
                role = m.get("role")
                self._count("messages", role or "?")
                mmeta = nonempty({k: v for k, v in m.items()
                                  if k not in ("role", "content", "tool_calls", "extra_info", "tool_call_id", "name")})
                text, images, layout = joined_text(m.get("content"))
                if layout:
                    mmeta["content_layout"] = layout
                if role == "user":
                    if not text.strip() and not images:
                        self._count("skipped", "user message with no text or image")
                        continue
                    payload = {"text": text}
                    if images:
                        payload["images_omitted"] = images
                        self._count("skipped", "image bytes (count kept in payload)", images)
                    if buf is primary:
                        typed = user_text(m.get("content"))
                        if typed and typed == last_user and handoff_pending:
                            mmeta["builder_handoff_repeat"] = True
                            handoff_pending = False
                        elif typed:
                            close_turn()
                            user_turns += 1
                            last_user = typed
                            handoff_pending = False
                            t = turns.get(r["message_id"])
                            if t:
                                mmeta["trae_chat_turn"] = self._turn_meta(t)
                                pending_turn = (t, at)
                    emit(self._event(buf.record["provider_session_id"], f"{h}:{i}", "message", at, payload, "user",
                                     mmeta or None))
                elif role == "assistant":
                    extra = m.get("extra_info") or {}
                    model = extra.get("model_name") or extra.get("model")
                    if model:
                        models.add(model)
                    emeta = nonempty(extra, skip=("content",))
                    if extra.get("content"):
                        emeta["reasoning_omitted"] = "encrypted"
                    if emeta:
                        mmeta["extra_info"] = emeta
                    tcs = m.get("tool_calls") or []
                    if not text.strip() and not tcs:
                        self._count("skipped", "assistant message with no text or tool calls")
                        continue
                    if text.strip():
                        emit(self._event(buf.record["provider_session_id"], f"{h}:{i}", "message", at,
                                         nonempty({"text": text, "model": model}), "assistant", mmeta or None))
                        mmeta = None
                    for k, tc in enumerate(tcs):
                        fn = tc.get("function_call") or tc.get("function") or {}
                        name, raw_args = fn.get("name"), fn.get("arguments")
                        args, fmt = raw_args, "raw"
                        if isinstance(raw_args, str):
                            try:
                                args, fmt = json.loads(raw_args), "json"
                            except Exception:
                                pass
                        if name == "run_agent" and buf is primary:
                            handoff_pending = True
                        tmeta = nonempty({k2: v for k2, v in tc.items() if k2 not in ("function_call", "function", "id")})
                        if fmt == "raw":
                            tmeta["arguments_format"] = "raw"
                        if mmeta:
                            tmeta["message"] = mmeta
                            mmeta = None
                        emit(self._event(buf.record["provider_session_id"], f"{h}:{i}:call:{k}", "tool_call", at,
                                         nonempty({"call_id": tc.get("id"), "name": name, "arguments": args}),
                                         "assistant", tmeta or None), file_refs(name, args))
                elif role == "tool":
                    st = TOOL_STATUS.search(text)
                    er = TOOL_ERROR.search(text)
                    payload = nonempty({"call_id": m.get("tool_call_id"), "name": m.get("name"),
                                        "status": st.group(1).strip() if st else None,
                                        "error": er.group(1).strip() if er else None})
                    payload["text"] = text
                    extra = nonempty(m.get("extra_info") or {})
                    if extra:
                        mmeta["extra_info"] = extra
                    emit(self._event(buf.record["provider_session_id"], f"{h}:{i}", "tool_output", at, payload, "tool",
                                     mmeta or None))
                else:
                    self._count("skipped", f"message with unknown role {role!r}")
            if r.get("summary"):
                emit(self._event(buf.record["provider_session_id"], f"{h}:summary", "summary", r["updated_at"] or at,
                                 {"kind": "step_summary", "text": r["summary"]}))
            acc = r.get("accumulated_summary")
            if acc:
                try:
                    accj = json.loads(acc)
                except Exception:
                    accj = {"summary": acc}
                key = buf.record["provider_session_id"]
                if accj.get("summary") and accj["summary"] != last_acc.get(key):
                    last_acc[key] = accj["summary"]
                    emit(self._event(key, f"{h}:accumulated_summary", "summary", r["updated_at"] or at,
                                     {"kind": "compaction", "text": accj["summary"]},
                                     meta={"accumulated_summary": nonempty(accj, skip=("summary",))}))
                elif accj.get("summary"):
                    self._count("skipped", "accumulated_summary repeated unchanged from an earlier row")
            if first and r.get("messages"):
                self._count("skipped", "history_v2 row with nothing to export")
        close_turn()
        return bufs, user_turns, models, last_at

    def _inline(self, sid, primary):
        rows = self.con.execute(
            "SELECT cm.*, g.content AS gc, c.content AS cc FROM chat_message cm "
            "LEFT JOIN chat_message_general g ON g.message_id = cm.message_id "
            "LEFT JOIN chat_message_chat c ON c.message_id = cm.message_id "
            "WHERE cm.session_id=? ORDER BY cm.message_index, cm.id", (sid,)).fetchall()
        user_turns, last_at = 0, 0
        for r in rows:
            r = dict(r)
            if r["deleted_at"]:
                self._count("skipped", "chat_message row deleted in Trae")
                continue
            at = r["created_at"] or 0
            last_at = max(last_at, r["updated_at"] or at)
            role = r["message_role"]
            self._count("messages", f"inline {role}")
            meta = {"trae_chat_message": nonempty(r, skip=("gc", "cc"))}
            if role == "user" and r["gc"]:
                try:
                    items = json.loads(r["gc"])
                    text = "\n".join(i.get("text_content", "") for i in items if isinstance(i, dict))
                    extra = [nonempty(i, skip=("text_content",)) for i in items if isinstance(i, dict)]
                    if any(extra):
                        meta["content_items"] = extra
                except Exception:
                    text = r["gc"]
                if not text.strip():
                    self._count("skipped", "user message with no text or image")
                    continue
                user_turns += 1
                primary.add(self._event(sid, f"cm:{r['message_id']}", "message", at, {"text": text}, "user", meta))
            elif role == "assistant" and r["cc"]:
                try:
                    j = json.loads(r["cc"])
                    payload = nonempty({"text": j.get("content") or "", "reasoning": j.get("reasoning_content")})
                    rest = nonempty(j, skip=("content", "reasoning_content"))
                    if rest:
                        meta["content_extra"] = rest
                except Exception:
                    payload = {"text": r["cc"]}
                if not payload.get("text", "").strip() and not payload.get("reasoning"):
                    self._count("skipped", "assistant message with no text or tool calls")
                    continue
                primary.add(self._event(sid, f"cm:{r['message_id']}", "message", at, payload, "assistant", meta))
            else:
                self._count("skipped", "chat_message row with no content")
        return [primary], user_turns, set(), last_at

    def write_jsonl(self, sids, path, fingerprint=None):
        tmp = path + ".partial"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(jline({"record_type": "manifest", "schema_version": "ctx-history-jsonl-v2",
                           "lineage_contract": "provider_native_v1", "producer": JSONL_PRODUCER,
                           "metadata": {"mapping_revision": MAPPING_REVISION,
                                        "mapping_doc": "trae-to-ctx docs/ctx-mapping.md"}}) + "\n")
            src = {"record_type": "source", "source_id": self.source_id, "provider_key": "trae",
                   "source_format": "trae_ai_agent_sqlcipher_v1", "raw_source_path": LIVE_DB}
            if fingerprint:
                src["fingerprint"] = fingerprint
            src.update({"trust": "provider_export", "fidelity": "full",
                        "metadata": {"exported_from": "decrypted copy of the ai-agent database"}})
            f.write(jline(src) + "\n")
            for sid in sids:
                bufs = self.build(sid)
                if bufs is None:
                    continue
                for b in bufs:
                    f.write(jline(b.record) + "\n")
                    if hasattr(b, "edge"):
                        f.write(jline(b.edge) + "\n")
                    for e in b.events:
                        f.write(jline(e) + "\n")
                    for r in b.refs:
                        f.write(jline(dict({"record_type": "file_reference", "source_id": self.source_id,
                                            "provider_session_id": b.record["provider_session_id"]}, **r)) + "\n")
        os.replace(tmp, path)
        self.report["file_bytes"] = os.path.getsize(path)
        return self.report


# ---------------------------------------------------------------------------
# Markdown from the JSONL export (the reading view no longer needs the database)
# ---------------------------------------------------------------------------

def jsonl_groups(path):
    """Stream one root session at a time: {"root": record, "sessions": {id: record}, "events": {id: [event]}}."""
    cur = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            rt = r.get("record_type")
            if rt == "session":
                sid = r["provider_session_id"]
                if r.get("session_relationship") == "root":
                    if cur:
                        yield cur
                    cur = {"root": r, "sessions": {}, "events": {}}
                cur["sessions"][sid] = r
                cur["events"][sid] = []
            elif rt == "event":
                cur["events"][r["provider_session_id"]].append(r)
    if cur:
        yield cur


def split_parts(text, layout):
    """Undo joined_text(): the non-empty text parts and the image count of the original content list."""
    if layout is None:
        return ([text] if text else []), 0
    parts, pos, images = [], 0, 0
    for item in layout:
        if isinstance(item, int):
            parts.append(text[pos:pos + item])
            pos += item + 1
        elif item == "image_url":
            images += 1
    return [p for p in parts if p], images


def jsonl_session_dict(group):
    root = group["root"]
    meta = root.get("metadata") or {}
    s = {"session_id": root["provider_session_id"], "session_title": None, "project_id": None,
         "created_at": None, "updated_at": None, "session_type": None}
    if meta.get("trae_chat_session"):
        s.update(meta["trae_chat_session"])
    else:
        epoch = lambda v: int(dt.datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp())
        s.update({"session_title": "(orphaned: no chat_session row)", "session_type": "orphan",
                  "created_at": epoch(root["started_at"]), "updated_at": epoch(root.get("ended_at") or root["started_at"])})
    return s


def jsonl_blocks(group):
    """The same block stream as Exporter.blocks, rebuilt from one exported session tree."""
    root = group["root"]
    sid = root["provider_session_id"]
    counts = (root.get("metadata") or {}).get("trae_row_counts") or {}
    stats = {"rows": counts.get("rows", 0), "deleted_rows": counts.get("deleted", 0), "models": set(),
             "tool_calls": 0, "sub_agents": 0, "handoffs": 0}
    primary = group["events"][sid]
    for e in primary:
        if e["event_id"] == "session:info":
            stats["models"].update(e["payload"].get("models") or [])
    if counts.get("table") == "chat_message":
        stats["rows"] -= stats.pop("deleted_rows")
        stats["source"] = "chat_message (inline chat)"
        for e in primary:
            if e["event_type"] != "message":
                continue
            t = e["payload"].get("text") or ""
            if e.get("role") == "user":
                t = user_text(t) if "<" in t else t.strip()
                if t:
                    yield "user", t, None
            elif e.get("role") == "assistant" and t.strip():
                yield "assistant", t.strip(), None
        yield "stats", stats, None
        return

    # Merge the primary and sub-agent sessions back into history_v2 row order.
    rows = {}
    for evs in group["events"].values():
        for e in evs:
            tr = (e.get("metadata") or {}).get("trae_row")
            if tr:
                rows[str(tr["history_v2_id"])] = tr
    order = {s: n for n, s in enumerate(group["sessions"])}
    merged = []
    for s, evs in group["events"].items():
        for e in evs:
            row = rows.get(e["event_id"].split(":", 1)[0])
            if row:  # session and turn notices have no row; their content is carried in metadata instead
                merged.append((row["id"], order[s], e["event_index"], e, row))
    merged.sort(key=lambda x: x[:3])

    current_sub, last_task = None, None
    for _, _, _, e, row in merged:
        sub = e["provider_session_id"] != sid
        if sub != bool(current_sub):
            if current_sub:
                yield "sub_end", "", None
                current_sub = None
            if sub:
                stats["sub_agents"] += 1
                current_sub = last_task or "sub-agent"
                yield "sub_start", current_sub, None
        p, meta = e["payload"], e.get("metadata") or {}
        if e["event_type"] == "message":
            parts, images = split_parts(p.get("text") or "", meta.get("content_layout"))
            if e.get("role") == "user":
                content = [{"type": "text", "text": x} for x in parts] + [{"type": "image_url"}] * images
                t = user_text(content)
                if sub:
                    if t:
                        yield "sub_task", t, None
                elif not t:
                    continue
                elif meta.get("builder_handoff_repeat"):
                    stats["handoffs"] += 1
                    yield "handoff", row.get("agent_type") or "agent", None
                else:
                    turn = meta.get("trae_chat_turn")
                    yield "user", t, (turn.get("turn_status"), turn.get("error_message")) if turn else None
            elif e.get("role") == "assistant":
                t = "\n\n".join(x.strip() for x in parts if x.strip())
                if t:
                    yield "assistant", t, None
        elif e["event_type"] == "tool_call":
            fn = {k: p[k] for k in ("name", "arguments") if k in p}
            label = tool_label({"function_call": fn})
            stats["tool_calls"] += 1
            if label.startswith(("Task ", "run_agent ")):
                last_task = label
            yield "tools", [label], None
    if current_sub:
        yield "sub_end", "", None
    yield "stats", stats, None


def markdown_from_jsonl(path, out_dir):
    for group in jsonl_groups(path):
        root = group["root"]
        s = jsonl_session_dict(group)
        project = root.get("cwd") or "(unknown project)"
        md, turns, project, title = render_md(s, project, jsonl_blocks(group))
        yield s["session_id"], write_md(out_dir, s, md, turns, project, title)


def default_source_id():
    import socket
    host = re.sub(r"[^a-z0-9.-]+", "-", socket.gethostname().split(".")[0].lower()).strip("-")
    return f"trae-intl-{host or 'mac'}"


def export_jsonl(db, out, source_id=None, everything=True, sample=False, sessions=(), fingerprint=None):
    """Write <out> and <out>.report.json; return the report."""
    base = source_id or default_source_id()
    # A ctx source is replaced as a whole on import, so partial exports must never share the real source_id.
    source_id = base if everything else base + "-sample"
    ex = JsonlExporter(db, source_id)
    sids = ex.session_ids() if everything else [s for _, s in ex.sample_ids()] if sample else list(sessions)
    try:
        report = ex.write_jsonl(sids, out, fingerprint)
    finally:
        ex.con.close()
    report["source_id"] = source_id
    report["requested_sessions"] = len(sids)
    with open(out + ".report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    return report


def main(argv=None, prog=None):
    ap = argparse.ArgumentParser(prog=prog, description="Export sessions as Markdown or ctx-history-jsonl-v2.")
    ap.add_argument("db")
    ap.add_argument("out", help="markdown: output directory; --jsonl: output .jsonl file")
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--session", action="append", default=[])
    ap.add_argument("--jsonl", action="store_true", help="write ctx-history-jsonl-v2 instead of markdown")
    ap.add_argument("--all", action="store_true", help="with --jsonl: every session (the canonical export)")
    ap.add_argument("--source-id", default=None, help="default trae-intl-<host>; partial exports add -sample")
    ap.add_argument("--fingerprint", default=None, help="e.g. sha256:<hex> of the encrypted database")
    ap.add_argument("--from-jsonl", action="store_true",
                    help="first argument is a --jsonl export; render every session in it as markdown")
    a = ap.parse_args(argv)
    if a.from_jsonl:
        os.makedirs(a.out, exist_ok=True)
        for sid, p in markdown_from_jsonl(a.db, a.out):
            size = os.path.getsize(p) if p else 0
            print(f"{sid}  {size:>8d} B  {p or '(no user turns, skipped)'}")
        return
    if a.jsonl:
        if not (a.all or a.sample or a.session):
            ap.error("--jsonl needs --all, --sample or --session")
        report = export_jsonl(a.db, a.out, a.source_id, a.all, a.sample, a.session, a.fingerprint)
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    if not (a.sample or a.session):
        ap.error("Markdown needs --sample or --session; for every session, export --jsonl --all and run "
                 "`trae-to-ctx markdown` on it (or use `trae-to-ctx run`)")
    ex = Exporter(a.db)
    os.makedirs(a.out, exist_ok=True)
    try:
        targets = ex.sample_ids() if a.sample else [("requested", s) for s in a.session]
        for why, sid in targets:
            p = ex.write(sid, a.out)
            size = os.path.getsize(p) if p else 0
            print(f"{why:28s} {sid}  {size:>8d} B  {p or '(no user turns, skipped)'}")
    finally:
        ex.con.close()


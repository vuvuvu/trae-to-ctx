"""Build a small synthetic Trae ai-agent database (decrypted form) for the tests.

The tables and columns match the real database; the content is invented. Each session
exercises one or more cases the exporter has to handle.
"""
import json
import sqlite3
import struct

SCHEMA = """
CREATE TABLE project (id INTEGER PRIMARY KEY, project_id TEXT, source TEXT, user_id TEXT, name TEXT, description TEXT,
  absolute_path TEXT, biz_project_id TEXT, created_at bigint, updated_at bigint, deleted_at bigint,
  workspace_status TEXT, transient_fallback_project_id TEXT, remote_project_id TEXT, last_active_at bigint,
  work_mode TEXT);
CREATE TABLE agent (id INTEGER PRIMARY KEY, agent_id TEXT, user_id TEXT, agent_type TEXT, name TEXT, description TEXT,
  built_in_tool_list TEXT, prompt TEXT, created_at bigint, updated_at bigint, avatar_id TEXT, unique_name TEXT,
  can_be_sub_agent boolean, builtin_tool_list TEXT, enterprise_agent_id TEXT, enterprise_tenant_id TEXT,
  enterprise_user_id TEXT, enterprise_version INTEGER, workspace TEXT);
CREATE TABLE agent_run (id INTEGER PRIMARY KEY, agent_run_id TEXT, parent_run_id TEXT, agent_id TEXT,
  created_at bigint, updated_at bigint, deleted_at bigint, session_id TEXT, agent_call_id TEXT);
CREATE TABLE chat_session (id INTEGER PRIMARY KEY, session_id TEXT, project_id TEXT, created_at bigint,
  updated_at bigint, deleted_at bigint, session_type TEXT, agent_process_support TEXT, session_title TEXT,
  worktree_id TEXT, context TEXT, work_mode TEXT, session_icon TEXT, hidden_status TEXT, is_pinned boolean,
  pinned_at bigint, extra TEXT);
CREATE TABLE history_v2 (id INTEGER PRIMARY KEY, history_v2_id TEXT, session_id TEXT, message_id TEXT, messages TEXT,
  token_usage INTEGER, summary TEXT, summarized_above boolean, created_at bigint, updated_at bigint,
  deleted_at bigint, agent_type TEXT, content_source TEXT, agent_run_id TEXT, accumulated_summary TEXT,
  compress_token_usage INTEGER, micro_compacted boolean, micro_compact_strategy TEXT, truncated_above boolean);
CREATE TABLE chat_turn (id INTEGER PRIMARY KEY, session_id TEXT, turn_id TEXT, reply_to_message_id TEXT,
  response_message_id TEXT, requirement_id TEXT, rewritten_user_message TEXT, turn_status TEXT, error_message TEXT,
  context TEXT, created_at bigint, updated_at bigint, deleted_at bigint, agent_type TEXT, agent_id TEXT,
  agent_name TEXT, additional_context TEXT, agent_avatar_id TEXT, is_worktree boolean, products TEXT,
  merged_commit TEXT);
CREATE TABLE chat_message (id INTEGER PRIMARY KEY, session_id TEXT, message_id TEXT, message_type TEXT,
  message_role TEXT, message_index INTEGER, is_archived INTEGER, reply_to_message_id TEXT, user_message_context TEXT,
  created_at bigint, updated_at bigint, deleted_at bigint, revertible INTEGER);
CREATE TABLE chat_message_general (id INTEGER PRIMARY KEY, message_id TEXT, content TEXT, created_at bigint,
  updated_at bigint, deleted_at bigint);
CREATE TABLE chat_message_chat (id INTEGER PRIMARY KEY, message_id TEXT, content TEXT, created_at bigint,
  updated_at bigint, deleted_at bigint);
"""

FAKE_TOKEN = "fixture-account-token-0123456789abcdefABCDEF"
FAKE_FILE_BODY = "SECRET_IN_OPEN_FILE=fixture\n" * 50
PASTED_SECRET = "sk-fixtureFIXTUREfixture1234567890"
T0 = 1750000000


def user(text, injected=True):
    parts = []
    if injected:
        parts.append({"type": "text", "text": "<system-reminder>injected rules</system-reminder>"})
    parts.append({"type": "text", "text": f"<user_input>{text}</user_input>"})
    return {"role": "user", "content": parts}


def assistant(text="", calls=(), model="fixture-model"):
    m = {"role": "assistant", "content": [{"type": "text", "text": text}] if text else [],
         "extra_info": {"model_name": model, "content": "ENCRYPTED-REASONING"}}
    if calls:
        m["tool_calls"] = [{"id": cid, "index": i, "type": "function",
                            "function_call": {"name": name, "arguments": args}}
                           for i, (cid, name, args) in enumerate(calls)]
    return m


def tool(cid, name, result, status="success"):
    return {"role": "tool", "tool_call_id": cid, "name": name,
            "content": [{"type": "text", "text": f"<toolcall_status>{status}</toolcall_status>"
                                                 f"<toolcall_result>{result}</toolcall_result>"}]}


def empty_db_with_reserve(path, reserve):
    """Write an empty SQLite file whose pages keep `reserve` bytes free at the end, like Trae's."""
    page = 4096
    hdr = bytearray(page)
    hdr[0:16] = b"SQLite format 3\x00"
    struct.pack_into(">H", hdr, 16, page)
    hdr[18:24] = bytes([1, 1, reserve, 64, 32, 32])
    struct.pack_into(">I", hdr, 24, 1)    # file change counter
    struct.pack_into(">I", hdr, 28, 1)    # size in pages
    struct.pack_into(">I", hdr, 44, 4)    # schema format
    struct.pack_into(">I", hdr, 56, 1)    # UTF-8
    struct.pack_into(">I", hdr, 92, 1)    # version-valid-for
    struct.pack_into(">I", hdr, 96, 3045000)
    hdr[100] = 0x0D                        # empty table b-tree leaf (sqlite_schema)
    struct.pack_into(">H", hdr, 105, page - reserve)
    with open(path, "wb") as f:
        f.write(hdr)


def build(path, reserve=0):
    """Build the fixture. reserve=80 gives Trae's page layout, so the file can be encrypted for tests."""
    if reserve:
        empty_db_with_reserve(path, reserve)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    q = con.execute
    q("INSERT INTO project (project_id, absolute_path) VALUES ('p1', '/workspace/fixture-app')")
    q("INSERT INTO agent (agent_id, name) VALUES ('a-main', 'Builder'), ('a-sub', 'Researcher')")
    q("INSERT INTO agent_run (agent_run_id, parent_run_id, agent_id, created_at, session_id) VALUES "
      "('run-main', '', 'a-main', ?, 's1'), ('run-sub', 'run-main', 'a-sub', ?, 's1')", (T0, T0 + 20))

    def session(sid, title, t, stype="side_chat", project="p1"):
        q("INSERT INTO chat_session (session_id, project_id, created_at, updated_at, session_type, session_title) "
          "VALUES (?,?,?,?,?,?)", (sid, project, t, t + 500, stype, title))

    rid = [0]

    def row(sid, msgs, t, run="run-main", mid=None, deleted=None, summary=None, acc=None, agent_type="builder"):
        rid[0] += 1
        q("INSERT INTO history_v2 (history_v2_id, session_id, message_id, messages, created_at, updated_at, deleted_at,"
          " agent_type, agent_run_id, summary, accumulated_summary, token_usage) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
          (f"h{rid[0]:03d}", sid, mid or f"m{rid[0]:03d}", json.dumps({"mode": "agent", "raw_messages": msgs}),
           t, t + 1, deleted, agent_type, run, summary, acc, 100))

    # s1: tools, a sub-agent, an unparsable argument string, a deleted row, summaries, a pasted secret
    session("s1", "Fix the login page", T0)
    row("s1", [user(f"Fix the login page, my key is {PASTED_SECRET}")], T0, mid="m-turn1")
    row("s1", [assistant("Looking at the code.", [("c1", "view_files", json.dumps({"files": [{"file_path": "src/login.ts"}]})),
                                                    ("c2", "Task", json.dumps({"description": "research auth"}))])],
        T0 + 1, summary="Viewed login.ts and delegated research.")
    row("s1", [user("Research how auth works", injected=False)], T0 + 20, run="run-sub")
    row("s1", [assistant("", [("c3", "search_by_regex", json.dumps({"query": "authenticate"}))]),
               tool("c3", "search_by_regex", "src/auth.ts:12 authenticate()")], T0 + 21, run="run-sub")
    row("s1", [assistant("Auth lives in src/auth.ts.")], T0 + 22, run="run-sub")
    row("s1", [tool("c1", "view_files", "export function login() {}"), tool("c2", "Task", "done")], T0 + 30)
    row("s1", [assistant("This row was deleted.")], T0 + 31, deleted=T0 + 99)
    row("s1", [assistant("", [("c4", "finish", "{not json")]), tool("c4", "finish", "ok", status="failed")], T0 + 32,
        acc=json.dumps({"summary": "Compacted: fixed login.", "accumulated_summary_used": True}))
    row("s1", [assistant("Fixed the login page.")], T0 + 33,
        acc=json.dumps({"summary": "Compacted: fixed login.", "accumulated_summary_used": True}))
    q("INSERT INTO chat_turn (session_id, turn_id, reply_to_message_id, turn_status, created_at, updated_at) "
      "VALUES ('s1', 't1', 'm-turn1', 'completed', ?, ?)", (T0, T0 + 33))

    # s2: a failed turn whose context holds an account token and an IDE file snapshot; a builder handoff
    session("s2", "Build a todo app", T0 + 1000)
    row("s2", [user("Build a todo app")], T0 + 1000, mid="m-turn2")
    row("s2", [assistant("", [("c5", "run_agent", json.dumps({"agent_id": "ui_builder_coding"}))]),
               tool("c5", "run_agent", "handed off")], T0 + 1001)
    row("s2", [user("Build a todo app")], T0 + 1002, agent_type="ui_builder_coding")
    row("s2", [assistant("Created index.html.", [("c6", "write_to_file", json.dumps({"file_path": "index.html"}))])],
        T0 + 1003, agent_type="ui_builder_coding")
    row("s2", [user("Continue")], T0 + 1010, mid="m-turn3")
    ctx = {"user_info": {"token": FAKE_TOKEN, "user_id": "u1"},
           "render_context": {"current_file": {"file_path": "/workspace/fixture-app/.env", "content": FAKE_FILE_BODY},
                              "hash_files": [{"file_path": "a.txt", "content": "alpha"}]},
           "persist_user_message_context": {"model_info": {"encrypted_model_params": "ENC", "name": "m"}}}
    q("INSERT INTO chat_turn (session_id, turn_id, reply_to_message_id, turn_status, error_message, context, "
      "created_at, updated_at) VALUES ('s2', 't3', 'm-turn3', 'failed', ?, ?, ?, ?)",
      (json.dumps({"message": "exceeded maximum number of turns"}), json.dumps(ctx), T0 + 1010, T0 + 1020))

    # s3: inline chat (chat_message tables only)
    session("s3", "Explain regex", T0 + 2000, stype="inline_chat")
    for i, (mid, role) in enumerate((("cm1", "user"), ("cm2", "assistant"))):
        q("INSERT INTO chat_message (session_id, message_id, message_type, message_role, message_index, created_at, "
          "updated_at) VALUES ('s3', ?, 'text', ?, ?, ?, ?)", (mid, role, i, T0 + 2000 + i, T0 + 2000 + i))
    q("INSERT INTO chat_message_general (message_id, content) VALUES ('cm1', ?)",
      (json.dumps([{"text_content": "What does ^a.*b$ match?", "type": "text"}]),))
    q("INSERT INTO chat_message_chat (message_id, content) VALUES ('cm2', ?)",
      (json.dumps({"content": "Strings that start with a and end with b.", "reasoning_content": "simple"}),))

    # s4: no user turn at all (only injected context), so it is skipped
    session("s4", "Empty", T0 + 3000)
    row("s4", [{"role": "user", "content": [{"type": "text", "text": "<system-reminder>only</system-reminder>"}]}],
        T0 + 3000)
    con.commit()
    con.close()


if __name__ == "__main__":
    import sys
    build(sys.argv[1])

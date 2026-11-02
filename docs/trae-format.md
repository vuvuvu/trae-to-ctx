# Trae's local history format

What we learned by reading the international Trae build on macOS (2025 builds).
Other builds and other operating systems may differ. If yours does, an issue with
the `probe` output (it prints no content) helps.

## Where it is

```
~/Library/Application Support/Trae/ModularData/ai-agent/database.db
```

Quit Trae and work on a copy. On APFS, `cp -c` makes an instant clone. Updating
or launching Trae may migrate or re-encrypt the file, so copy it first.

Trae has no history export feature that we could find, and ctx dropped its
Trae importer in v1.0.0. That is why this project exists.

## Encryption

The file is a SQLCipher-style encrypted SQLite database.

- The key is a fixed raw AES-256 key, the same on every install of this build.
  It is `DEFAULT_KEY` in `src/trae_to_ctx/decrypt.py`. It was first published in
  [xitula/trae-session-export](https://github.com/xitula/trae-session-export).
  This project's decryptor was written from that documented layout; none of that
  repo's code is used or needed.
- Pages are 4096 bytes. The last 80 bytes of each page are reserved:
  - `[0:4016]` AES-256-CBC ciphertext
  - `[4016:4032]` IV
  - `[4032:4096]` HMAC-SHA512
- On page 1, bytes `[0:16]` are the salt. After decryption they become
  `SQLite format 3\0`, and only `[16:4016]` is ciphertext.
- The HMAC follows SQLCipher 4 defaults. The HMAC key is
  PBKDF2-HMAC-SHA512(key, salt XOR 0x3a, 2 iterations, 32 bytes). The message is
  `page[start:4032]` plus the page number as a little-endian u32, where `start`
  is 16 on page 1 and 0 otherwise. Only `probe` checks it; decryption doesn't
  need it.
- The decrypted file is a normal SQLite database. The reserved bytes are zeroed
  and the header keeps its reserved-space byte (80), which SQLite accepts.

## Tables that matter

| Table | Role |
|---|---|
| `chat_session` | One row per conversation. `session_type` is `side_chat` (the agent panel) or `inline_chat`. `project_id` joins `project`. |
| `project` | Project paths, used as the session's working directory. |
| `history_v2` | The source of truth for side chats. Each row is a delta: the `messages` column is JSON `{mode, raw_messages}`. Rows with `deleted_at` set were deleted by the user. |
| `server_history_info` | A synced duplicate of `history_v2` (`client_history_id` = `history_v2_id`). Ignored. |
| `chat_turn` | One row per user turn. `reply_to_message_id` is the user row's `message_id`. `turn_status` is completed, failed or canceled; `error_message` is JSON `{message}`. `context` is a JSON string with IDE state (see below). |
| `agent_run` | Agent runs. A run with a `parent_run_id` is a sub-agent. `history_v2.agent_run_id` links messages to runs. |
| `chat_message`, `chat_message_general`, `chat_message_chat` | Inline chats. User text is a JSON list of `{text_content}`; assistant text is JSON `{content, reasoning_content}`. |

Timestamps are Unix seconds. `history_v2` also has `token_usage`, `summary`
(a model-written summary of one step), `accumulated_summary` (the
context-compaction summary, repeated on many rows), `agent_type`,
`content_source`, `truncated_above` and `micro_compacted`.

## Messages in `raw_messages`

- Roles are `user`, `assistant` and `tool`. Content is a list of `{type: text}`
  or `{type: image_url}` parts.
- What you typed is inside `<user_input>…</user_input>`. Everything else in a
  user message was injected by Trae: `<system-reminder>`, `<rules>`,
  `<user_rules>`, `<available_terminal>`, `<ide_context>`.
- Assistant `extra_info.model_name` names the model. `extra_info.content` is
  encrypted reasoning; we can't read it.
- `tool_calls[].function_call` is `{name, arguments}`, and `arguments` is a JSON
  string (occasionally not valid JSON). Older tools use `command` plus an `args`
  list, `files` as a list of `{file_path}`, or `information_request`.
- Tool results have `role: tool`, `tool_call_id` and `name`. The content is
  wrapped in `<toolcall_status>…</toolcall_status><toolcall_result>…</toolcall_result>`.
- Assistant text uses Trae's reference tags: `<mcfile name= path=>`,
  `<mcreference>`, `<mcsymbol>`, `<mcfolder>`, `<mcurl>`, `<mccoremem>`.
- The old Trae Solo builder (`run_agent`) sends the same user prompt to each
  internal agent in turn (`ui_builder_requirement`, then
  `ui_builder_project_preparation`, then `ui_builder_coding`). Those repeats are
  handoffs, not new turns. A repeated "Continue" with no `run_agent` call before
  it is a real turn.

## Sensitive data in `chat_turn.context`

`context` is a JSON string, so a plain text scan of the database won't show
what's inside. It holds:

- `user_info.token`: your Trae account credential.
- `persist_user_message_context.model_info.encrypted_model_params`: an
  encrypted provider blob.
- `render_context.current_file` and `render_context.hash_files[]`: full copies
  of files open in the editor. These can be tens of MB per turn and contain
  whatever those files held, such as `.env` files or API keys in MCP configs.

The exporter drops all three and records what it dropped. See
[ctx-mapping.md](ctx-mapping.md#left-out-on-purpose).

Conversation text and tool output can also contain secrets you pasted or that a
command printed. The Markdown view redacts the common shapes; the JSONL export
keeps them, as ctx does for every other provider.

## Markdown rendering notes

- Injected context is stripped, tool calls become one line per assistant step,
  tool results are left out, and sub-agents are quoted inline under the Task
  that started them.
- Redaction deliberately over-matches: long hex strings such as git SHAs look
  like some API keys and get redacted too. `*_tokens = <number>` keys, snake_case
  file names and slugs like `AD-2026-03-02-some-plan` are left alone.
- Known quirks left as they are: nested code fences in inline chats can close
  early, Trae's `[]: # <｜placeholder_replace_marker｜>` line shows up, and
  Trae truncates some titles or leaves them empty.

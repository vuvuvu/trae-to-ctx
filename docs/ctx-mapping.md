# Trae to ctx-history-jsonl-v2 mapping

`trae-to-ctx export --jsonl` (and `trae-to-ctx run`) writes one file in ctx's public import format,
`ctx-history-jsonl-v2`. This page says what goes where, what is left out, and
how ctx treats the result. Tested with ctx 2.3.0. ctx's own docs are the
reference:

```bash
ctx docs show custom-history-import-format
ctx docs show provider-import-policy
```

The goal is a file complete enough that you can delete the decrypted database
afterwards. The JSONL alone regenerates the Markdown (`--from-jsonl`).

## Format rules that shaped the design

- Record types are `manifest`, `source`, `session`, `event`, `file_reference`
  and `edge`. ctx rejects unknown top-level fields. `metadata` and `payload`
  accept any JSON.
- ctx searches only a projection of the file: source and session identity,
  `cwd`, `agent_scope`, event type, role and `payload`, and file reference
  values. Session times and status, event `metadata`, `preview` and artifacts
  are stored but not searchable. So anything worth searching (title, model,
  turn failure) goes in `payload`. Fields kept only so nothing is lost go in
  `metadata`.
- Event IDs are deterministic, so exporting again and re-importing replaces
  data instead of duplicating it. A re-export is byte-identical.
- `lineage_contract: provider_native_v1` lets the file claim parent and child
  sessions. Trae records those links in `agent_run.parent_run_id`, so the claim
  holds. `copied_from` is never used: matching text doesn't prove a copy under
  ctx's rules.
- ctx rejects records over 16 MiB.

## Mapping (revision 1)

| Trae | v2 record |
|---|---|
| (none) | `manifest`: `schema_version: ctx-history-jsonl-v2`, `lineage_contract: provider_native_v1`, `producer: trae-to-ctx/<version>`, the mapping revision in metadata. No export timestamp, so re-exports are byte-identical. |
| The database | `source`: `provider_key: trae`, `source_id` (default `trae-intl-<hostname>`), `source_format: trae_ai_agent_sqlcipher_v1`, the default Trae path, and the encrypted file's fingerprint if you pass `--fingerprint`. Never the key. |
| `chat_session` | `session`: `provider_session_id` is the session id, `cwd` the project path, `agent_scope: primary`. The whole `chat_session` row and `trae_row_counts` go in metadata. |
| (none) | The first event of each session is a `notice` with payload `{kind: session_info, title, session_type, project, models}`, so these are searchable. |
| `agent_run` with a parent | A child `session` with id `<sid>:run:<run_id>`, `session_relationship: delegated`, `agent_scope: subagent`, the `agent_run` row in metadata, plus an `edge` from the parent. |
| User message | `message`, role `user`. Payload `{text}` holds the full text including Trae's injected context, as ctx does for `<system-reminder>` text in Claude sessions. Images become a count, `images_omitted`. The `chat_turn` row goes in metadata with its `context` parsed, minus the omissions below. Builder handoff repeats are flagged `builder_handoff_repeat`. |
| Assistant text | `message`, role `assistant`. Payload `{text, model}`. `extra_info` minus the encrypted reasoning goes in metadata. |
| Each tool call | `tool_call`. Payload `{call_id, name, arguments}`, with arguments parsed from JSON when possible and kept as the raw string otherwise. |
| Each tool result | `tool_output`. Payload `{call_id, name, status, error, text}` with the full text. |
| Failed or canceled turn | `notice` `turn:<turn_id>:status` at the end of the turn, payload `{kind: turn_status, turn_status, error}`. One per `turn_id`, even when two history rows link to the same turn. |
| `summary` | `summary` event `<id>:summary`, payload `{kind: step_summary, text}`. |
| `accumulated_summary` | `summary` event `<id>:accumulated_summary`, payload `{kind: compaction, text}`, only when the text changed since the previous row. |
| File arguments of tool calls | `file_reference`. Directories from `list_dir` or `LS` aren't files and are skipped. |
| Inline chat (`chat_message` only) | A `session` whose events have ids `cm:<message_id>`. The assistant payload is `{text, reasoning}`. |
| Every other column and message key | `metadata.trae_row` on the row's first event, or the event's own metadata. |

Event IDs are `<history_v2_id>:<message index>`, plus `:call:<n>` for each tool
call in an assistant message. `event_index` counts 0, 1, 2 within a session and
is stable across re-exports. A `file_reference` points at its event by
`event_index`.

`<mcfile>` tags in prose don't become file references. They name files the
model mentioned, not files a tool touched.

## Left out on purpose

The export report (`<out>.jsonl.report.json`) counts every one of these:

- rows the user deleted in Trae;
- sessions with no user turn;
- user messages with no text or image, and assistant messages with no text or
  tool calls;
- an `accumulated_summary` repeated unchanged from the previous row;
- `server_history_info`, which duplicates `history_v2`;
- `chat_message` rows of sessions that also have `history_v2` rows (duplicate
  prompts);
- image bytes (the count stays in the payload);
- encrypted reasoning (marked `reasoning_omitted: encrypted`);
- from `chat_turn.context`, listed under `context_omitted` on the turn:
  - IDE file snapshot bodies (`render_context.current_file` and
    `hash_files[]`), replaced by `content_omitted: {bytes, sha256}` with the
    path kept. A single turn can carry tens of MB of them, which breaks the
    16 MiB limit, and they copy whatever the open files contained;
  - `user_info.token`, the Trae account credential;
  - `model_info.encrypted_model_params`, an encrypted provider blob.

This follows ctx's provider import policy: omit provider-private material
explicitly, don't copy sensitive tokens, and drop only the oversized field
rather than the record. Conversation text and tool input and output stay
unredacted, as they do for every other ctx provider. The Markdown view redacts.

## How ctx treats the file

From throwaway imports with ctx 2.3.0:

| Check | Result |
|---|---|
| `payload` text of every event type | Searchable |
| Event `metadata` | Stored, not searchable (as documented) |
| `tool_call` | Searchable in the `calls` scope |
| `tool_output` | Searchable in the `outputs` scope only |
| User and assistant messages | `transcript` scope |
| Sub-agent sessions | `agent_scope: subagent`, `delegated`, parent linked |
| `file_reference` | Becomes a `file` fact; `ctx search --file` finds it |
| `command` facts | Not derived from custom imports. Commands are searchable as text in the `tool_call` payload |
| Same file imported again | No change |
| Changed file imported again | Old text replaced, nothing duplicated |
| A different file with the same `source_id` | **Replaces the whole source.** Sessions missing from the new file are removed |

The last row is why `--jsonl` without `--all` appends `-sample` to the source
id. Never import a partial export under your real source id.

## Verifying an export

```bash
trae-to-ctx check trae.jsonl
```

This checks the closed record envelopes, ordering, references, the 16 MiB
limit, and that every Trae message counted in the report is either an event or
a named skip. It prints counts only.

Then try a throwaway import before touching your real ctx data:

```bash
mkdir -p /private/tmp/ctx-trial            # not /tmp: ctx rejects paths through a symlink
export CTX_DATA_ROOT=/private/tmp/ctx-trial
ctx index mode manual                       # no daemon; the import uses a short-lived worker
ctx import --input-format ctx-history-jsonl-v2 --path "$PWD/trae.jsonl" --no-blame --progress
ctx search "something you remember asking Trae"
unset CTX_DATA_ROOT; rm -rf /private/tmp/ctx-trial
```

Setting `CTX_DAEMON_ENABLED=false` makes the import fail ("finite Core worker
did not become ready"); use manual mode as above.

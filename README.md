# trae-to-ctx

Get your agent chat history out of the Trae IDE. Trae keeps it in an encrypted
local database and has no export feature. This project decrypts a copy of that
database and exports every session as:

- **Markdown**, one readable file per session, with tool calls condensed and
  secrets redacted; or
- **JSONL** in `ctx-history-jsonl-v2`, the import format of
  ctx (`ctxrs/ctx`), a local search tool for agent history.
  ctx dropped its built-in Trae importer in v1.0.0; this replaces it.

The Markdown can be rendered from the database or from the JSONL file, with
identical output, so the JSONL is enough to keep.

Tested on the international Trae build for macOS (2025) with ctx 2.3.0. Other
builds and operating systems are untested; see [docs/trae-format.md](docs/trae-format.md)
for what to check.

Not affiliated with Trae, ByteDance or ctx.

## Privacy first

The decrypted database is your full chat history in plain text, including any
API keys or passwords you pasted into chats or that commands printed. So:

- Keep the decrypted file, exports and Markdown outside any git repo or synced
  folder. The decryptor creates its output with mode 600 and never overwrites a
  file.
- The JSONL export is **not redacted**, like every other ctx import. It does
  drop your Trae account token, encrypted blobs and copies of files that were
  open in the editor ([details](docs/ctx-mapping.md#left-out-on-purpose)).
- The Markdown is redacted with pattern matching. Patterns miss things, so
  read it before you share it.
- Delete the decrypted database once you have what you need.

## Requirements

- macOS with the Trae IDE (other systems untested)
- [uv](https://docs.astral.sh/uv/). It fetches Python 3.10+ and the one
  dependency (`cryptography`) for you
- ctx (`ctxrs/ctx`) 2.3 or newer, only if you want to import into it

## Quick start

Quit Trae, then:

```bash
uvx --from git+https://github.com/vuvuvu/trae-to-ctx trae-to-ctx run
```

That's it. `run` copies Trae's database, decrypts the copy, exports every
session, checks the export, and renders Markdown. It writes everything into a
new `./trae-export` folder (mode 700):

| Path | What it is |
|---|---|
| `trae.jsonl` | Every session, for ctx. Keep this one. |
| `trae.jsonl.report.json` | Counts of sessions, events, and everything left out |
| `markdown/<project>/<date>_<title>_<session_id>.md` | One readable file per session |
| `decrypted.db` | Your full history as a plain SQLite file. Delete it when you're done |

`run` never touches Trae's own files. It refuses to start if Trae looks like it
is running (`database.db-wal` is not empty) or if the output folder already has
files in it. Options: `--out DIR`, `--db PATH` if your database is elsewhere,
`--key HEX` if your build uses a different key, `--no-markdown`, and
`--source-id`.

To install the command instead of fetching it each time:

```bash
uv tool install git+https://github.com/vuvuvu/trae-to-ctx
trae-to-ctx run
```

## Importing into ctx

Do a throwaway import into a temporary root first
([how](docs/ctx-mapping.md#verifying-an-export)). Then:

```bash
ctx import --input-format ctx-history-jsonl-v2 --path "$PWD/trae-export/trae.jsonl" --no-blame --progress
```

**One source id is one complete snapshot.** Importing a different file under
the same `source_id` replaces everything ctx had from the earlier one. So only
import full exports (`run`, or `export --jsonl --all`) into your real ctx root.
Partial exports (`--sample`, `--session`) get a `-sample` suffix on the source
id for that reason. The default source id is `trae-intl-<hostname>`; set your
own with `--source-id` and use the same one every time.

## Single steps

`trae-to-ctx <command> --help` shows every option.

```bash
# Check that the key fits a copy of the database. Prints header facts only, no content.
trae-to-ctx probe encrypted.db

# Write the decrypted copy (mode 600; never overwrites)
trae-to-ctx decrypt encrypted.db decrypted.db

# Markdown for a few sessions of different kinds, or for specific ones
trae-to-ctx export decrypted.db md --sample
trae-to-ctx export decrypted.db md --session <session_id>

# JSONL with every session, then validate it and render Markdown from it
trae-to-ctx export decrypted.db trae.jsonl --jsonl --all --fingerprint sha256:<hash of encrypted.db>
trae-to-ctx check trae.jsonl          # must report "errors": []
trae-to-ctx markdown trae.jsonl md
```

Markdown rendered from the JSONL is identical to Markdown rendered from the
database, so the JSONL is all you need to keep.

## What's in the box

| File | What it does |
|---|---|
| `src/trae_to_ctx/cli.py` | The `trae-to-ctx` command and `run` |
| `src/trae_to_ctx/decrypt.py` | Decrypts the database page by page |
| `src/trae_to_ctx/export.py` | Markdown and JSONL export. Opens the database read-only |
| `src/trae_to_ctx/check.py` | Validates a JSONL export and accounts for every Trae message against the report |
| `docs/trae-format.md` | Trae's encryption, tables and message format |
| `docs/ctx-mapping.md` | How Trae maps onto ctx records, what's left out, and how ctx treats the import |
| `tests/` | Synthetic database and fixtures; no real history |

## Development

```bash
git clone https://github.com/vuvuvu/trae-to-ctx && cd trae-to-ctx
uv run trae-to-ctx --help
uv run python -m unittest discover -s tests
```

The tests build a small synthetic Trae database, encrypt it in Trae's page
layout, and run the whole pipeline on it: decryption, export, checks,
determinism, Markdown parity, and that tokens and file snapshots never leak.

## Credits

The database layout and key were first documented in
[xitula/trae-session-export](https://github.com/xitula/trae-session-export).
The code here was written independently from that description.

## License

[MIT](LICENSE)

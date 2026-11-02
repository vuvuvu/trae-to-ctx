"""The trae-to-ctx command."""
import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys

from . import __version__, check, decrypt, export

USAGE = f"""trae-to-ctx {__version__}: get your agent history out of the Trae IDE.

Usage: trae-to-ctx <command> [options]      (trae-to-ctx <command> --help for details)

The easy way:
  run        copy Trae's database, decrypt it, export JSONL for ctx, check it, render Markdown

Single steps:
  probe      check that the key fits a copy of the encrypted database (prints no content)
  decrypt    write a decrypted copy of the database
  export     export Markdown or JSONL from a decrypted database
  markdown   render Markdown for every session in a JSONL export
  check      validate a JSONL export against its report
"""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_db(src, dst):
    """Clone on APFS when possible (instant, no extra space), otherwise copy."""
    if sys.platform == "darwin" and subprocess.run(["cp", "-c", src, dst], capture_output=True).returncode == 0:
        return
    shutil.copyfile(src, dst)


def run_check(path):
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        rc = check.main(path)
    return rc, json.loads(buf.getvalue())


def cmd_run(argv, prog):
    ap = argparse.ArgumentParser(prog=prog, description=(
        "Do everything in one go. Writes into OUT: decrypted.db (your history in plain text), trae.jsonl "
        "and its report, and markdown/. Quit Trae first."))
    ap.add_argument("--db", default=os.path.expanduser(export.LIVE_DB),
                    help="Trae's encrypted database (default: the macOS location)")
    ap.add_argument("--out", default="trae-export", help="new or empty output directory (default: ./trae-export)")
    ap.add_argument("--key", default=decrypt.DEFAULT_KEY, help="64 hex characters (default: the published Trae key)")
    ap.add_argument("--source-id", default=None, help="ctx source id (default: trae-intl-<hostname>)")
    ap.add_argument("--no-markdown", action="store_true", help="skip the Markdown rendering")
    a = ap.parse_args(argv)
    key = decrypt.parse_key(a.key)
    out = os.path.abspath(a.out)

    if not os.path.isfile(a.db):
        sys.exit(f"no Trae database at {a.db}\npass --db with the path to ai-agent/database.db")
    wal = a.db + "-wal"
    if os.path.exists(wal) and os.path.getsize(wal) > 0:
        sys.exit("Trae has unsaved changes in database.db-wal, so it is probably running. Quit Trae and try again.")
    if os.path.exists(out) and os.listdir(out):
        sys.exit(f"{out} is not empty; pass --out with a new directory")
    os.makedirs(out, mode=0o700, exist_ok=True)
    os.chmod(out, 0o700)

    enc = os.path.join(out, "encrypted.db")
    dec = os.path.join(out, "decrypted.db")
    jsonl = os.path.join(out, "trae.jsonl")
    md = os.path.join(out, "markdown")

    print(f"1/5 copying {a.db}", flush=True)
    copy_db(a.db, enc)
    os.chmod(enc, 0o600)
    fingerprint = "sha256:" + sha256_file(enc)

    print("2/5 decrypting", flush=True)
    decrypt.full(enc, key, dec)
    os.remove(enc)  # our own copy; only needed for decryption

    print("3/5 exporting JSONL", flush=True)
    report = export.export_jsonl(dec, jsonl, a.source_id, fingerprint=fingerprint)

    print("4/5 checking the export", flush=True)
    rc, result = run_check(jsonl)
    if rc:
        print("\n".join(result["errors"]), file=sys.stderr)
        sys.exit("the export failed its checks; please open an issue with the errors above (they contain no content)")

    n_md = 0
    if not a.no_markdown:
        print("5/5 rendering Markdown", flush=True)
        os.makedirs(md, exist_ok=True)
        n_md = sum(1 for _, p in export.markdown_from_jsonl(jsonl, md) if p)

    sessions = report["sessions"]
    print(f"""
Done. {sessions.get('primary', 0)} sessions and {sessions.get('subagent', 0)} sub-agent runs, \
{sum(report['events'].values())} events.
  {jsonl}   ctx import file (source id {report['source_id']})
  {jsonl}.report.json   counts, including everything left out""")
    if n_md:
        print(f"  {md}/   {n_md} Markdown files (redacted, but check before sharing)")
    print(f"""  {dec}   your full history in plain text

Import into ctx (a throwaway import first is wise, see docs/ctx-mapping.md):
  ctx import --input-format ctx-history-jsonl-v2 --path "{jsonl}" --no-blame --progress

The JSONL holds everything; delete decrypted.db once you no longer need it.""")
    return 0


def cmd_markdown(argv, prog):
    ap = argparse.ArgumentParser(prog=prog, description="Render Markdown for every session in a JSONL export.")
    ap.add_argument("jsonl")
    ap.add_argument("out", help="output directory")
    a = ap.parse_args(argv)
    return export.main([a.jsonl, a.out, "--from-jsonl"], prog=prog)


def cmd_check(argv, prog):
    ap = argparse.ArgumentParser(prog=prog, description=(
        "Validate a JSONL export. Reads <file>.report.json when present. Prints counts only, no content."))
    ap.add_argument("jsonl")
    a = ap.parse_args(argv)
    if not os.path.isfile(a.jsonl):
        ap.error(f"no such file: {a.jsonl}")
    return check.main(a.jsonl)


COMMANDS = {
    "run": cmd_run,
    "probe": decrypt.main_probe,
    "decrypt": decrypt.main_decrypt,
    "export": export.main,
    "markdown": cmd_markdown,
    "check": cmd_check,
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] in ("-V", "--version"):
        print(__version__)
        return 0
    cmd = COMMANDS.get(argv[0])
    if cmd is None:
        print(USAGE, file=sys.stderr)
        return f"unknown command: {argv[0]}"
    return cmd(argv[1:], prog=f"trae-to-ctx {argv[0]}") or 0

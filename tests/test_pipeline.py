"""End-to-end tests on a synthetic database. Run from the repo root: uv run python -m unittest discover -s tests"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

import make_fixture_db  # noqa: E402
from trae_to_ctx import cli  # noqa: E402


def run(*args):
    """Run the trae-to-ctx command in-process; return (exit code, stdout)."""
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        try:
            rc = cli.main(list(args))
        except SystemExit as e:
            rc = e.code
    return rc, buf.getvalue()


def check(path):
    rc, out = run("check", path)
    return rc, json.loads(out)


def read_tree(d):
    out = {}
    for base, _, files in os.walk(d):
        for f in files:
            p = os.path.join(base, f)
            with open(p, encoding="utf-8") as fh:
                out[os.path.relpath(p, d)] = fh.read()
    return out


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = cls.tmp.name
        cls.db = os.path.join(t, "fixture.db")
        make_fixture_db.build(cls.db)
        cls.jsonl = os.path.join(t, "export.jsonl")
        run("export", cls.db, cls.jsonl, "--jsonl", "--all", "--source-id", "trae-test")
        with open(cls.jsonl, encoding="utf-8") as f:
            cls.raw = f.read()
        cls.records = [json.loads(line) for line in cls.raw.splitlines()]
        with open(cls.jsonl + ".report.json", encoding="utf-8") as f:
            cls.report = json.load(f)
        cls.md_db = os.path.join(t, "md-db")
        run("export", cls.db, cls.md_db, *[x for s in ("s1", "s2", "s3", "s4") for x in ("--session", s)])
        cls.md_jsonl = os.path.join(t, "md-jsonl")
        run("markdown", cls.jsonl, cls.md_jsonl)
        cls.md = read_tree(cls.md_db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def of(self, rt, **kw):
        return [r for r in self.records if r["record_type"] == rt and all(r.get(k) == v for k, v in kw.items())]

    def test_structure_and_accounting(self):
        rc, out = check(self.jsonl)
        self.assertEqual(out["errors"], [])
        self.assertEqual(rc, 0)
        self.assertEqual(self.report["sessions"], {"primary": 3, "subagent": 1})
        self.assertEqual(self.report["source_id"], "trae-test")

    def test_sessions_and_subagent_edge(self):
        self.assertEqual({r["provider_session_id"] for r in self.of("session", parent_provider_session_id=None)},
                         {"s1", "s2", "s3"})
        self.assertEqual(len(self.of("edge")), 1)
        child = [r for r in self.of("session") if r.get("parent_provider_session_id")]
        self.assertEqual([c["parent_provider_session_id"] for c in child], ["s1"])

    def test_skips_are_named(self):
        sk = self.report["skipped"]
        self.assertEqual(sk["history_v2 row deleted in Trae"], 1)
        self.assertEqual(sk["session with no user turn"], 1)
        self.assertEqual(sk["Trae account token in turn context"], 1)
        self.assertEqual(sk["IDE file snapshot body in turn context (path, size and sha256 kept)"], 2)

    def test_credentials_and_snapshots_not_exported(self):
        for leaked in (make_fixture_db.FAKE_TOKEN, "SECRET_IN_OPEN_FILE", "ENCRYPTED-REASONING", '"ENC"',
                       "This row was deleted"):
            self.assertNotIn(leaked, self.raw)
        self.assertIn("content_omitted", self.raw)

    def test_jsonl_keeps_user_text_unredacted(self):
        # raw records follow ctx: user-typed text stays as typed
        self.assertIn(make_fixture_db.PASTED_SECRET, self.raw)

    def test_markdown_redacts(self):
        text = "\n".join(self.md.values())
        self.assertNotIn(make_fixture_db.PASTED_SECRET, text)
        self.assertIn("[REDACTED]", text)
        for leaked in (make_fixture_db.FAKE_TOKEN, "SECRET_IN_OPEN_FILE", "injected rules"):
            self.assertNotIn(leaked, text)

    def test_markdown_content(self):
        self.assertEqual(len(self.md), 3)  # s4 has no user turn
        s1 = next(v for k, v in self.md.items() if k.endswith("_s1.md"))
        self.assertIn("> **Sub-agent** (Task `research auth`)", s1)
        self.assertIn("view_files `src/login.ts`", s1)
        s2 = next(v for k, v in self.md.items() if k.endswith("_s2.md"))
        self.assertIn("_Turn failed: exceeded maximum number of turns_", s2)
        self.assertIn("handed to internal agent: ui_builder_coding", s2)
        s3 = next(v for k, v in self.md.items() if k.endswith("_s3.md"))
        self.assertIn("Strings that start with a and end with b.", s3)

    def test_markdown_from_jsonl_matches_database(self):
        self.assertEqual(read_tree(self.md_jsonl), self.md)

    def test_deterministic(self):
        again = os.path.join(self.tmp.name, "again.jsonl")
        run("export", self.db, again, "--jsonl", "--all", "--source-id", "trae-test")
        with open(again, encoding="utf-8") as f:
            a = [json.loads(x) for x in f]
        for r in a + self.records:
            if r["record_type"] == "manifest":
                r.pop("exported_at", None)
        self.assertEqual(a, self.records)

    def test_partial_export_gets_sample_source_id(self):
        out = os.path.join(self.tmp.name, "one.jsonl")
        run("export", self.db, out, "--jsonl", "--session", "s1", "--source-id", "trae-test")
        with open(out + ".report.json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["source_id"], "trae-test-sample")


class Fixture(unittest.TestCase):
    def test_handwritten_fixture_is_valid(self):
        rc, out = check(os.path.join(HERE, "fixtures", "handwritten-v2.jsonl"))
        self.assertEqual(out["errors"], [])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()

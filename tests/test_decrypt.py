"""Decryption round trips: encrypt in Trae's page layout, then decrypt with the trae-to-ctx command."""
import os
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)

import make_fixture_db  # noqa: E402
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402
from test_pipeline import read_tree, run  # noqa: E402
from trae_to_ctx import decrypt as td  # noqa: E402

KEY = bytes(range(32))


def plain_pages(n):
    """n random pages whose page 1 carries a valid header for 4096-byte pages with 80 reserved bytes."""
    pages = []
    for pgno in range(1, n + 1):
        body = bytearray(os.urandom(td.IV_OFF)) + bytearray(td.RESERVE)
        if pgno == 1:
            body[:16] = td.SQLITE_MAGIC
            body[16:18] = td.PAGE.to_bytes(2, "big")
            body[18], body[19], body[20] = 1, 1, td.RESERVE
            body[21:24] = b"\x40\x20\x20"
        pages.append(bytes(body))
    return pages


def encrypt(key, plain):
    out = bytearray()
    for i in range(0, len(plain), td.PAGE):
        p = plain[i:i + td.PAGE]
        iv = os.urandom(16)
        e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        if i == 0:
            out += os.urandom(16) + e.update(p[16:td.IV_OFF]) + e.finalize()  # salt replaces the magic
        else:
            out += e.update(p[:td.IV_OFF]) + e.finalize()
        out += iv + os.urandom(64)  # HMAC is only reported, never required
    return bytes(out)


class Decrypt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plain = b"".join(plain_pages(3))
        self.enc = os.path.join(self.tmp.name, "database.db")
        with open(self.enc, "wb") as f:
            f.write(encrypt(KEY, self.plain))
        self.out = os.path.join(self.tmp.name, "plain.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        rc, _ = run("decrypt", self.enc, self.out, "--key", KEY.hex())
        self.assertEqual(rc, 0)
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(), self.plain)
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o600)

    def test_refuses_overwrite(self):
        open(self.out, "wb").close()
        rc, _ = run("decrypt", self.enc, self.out, "--key", KEY.hex())
        self.assertNotEqual(rc, 0)
        self.assertEqual(os.path.getsize(self.out), 0)

    def test_wrong_key_writes_nothing(self):
        rc, _ = run("decrypt", self.enc, self.out)  # the default key does not fit this file
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.exists(self.out))

    def test_probe(self):
        rc, out = run("probe", self.enc, "--key", KEY.hex())
        self.assertEqual(rc, 0)
        self.assertIn("header valid: True", out)


class RunCommand(unittest.TestCase):
    """`trae-to-ctx run` on an encrypted copy of the synthetic database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.plain_db = os.path.join(t, "plain.db")
        make_fixture_db.build(self.plain_db, reserve=td.RESERVE)
        os.makedirs(os.path.join(t, "trae"))
        self.enc = os.path.join(t, "trae", "database.db")
        with open(self.plain_db, "rb") as f, open(self.enc, "wb") as g:
            g.write(encrypt(KEY, f.read()))
        self.out = os.path.join(t, "out")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cmd(self, *extra):
        return run("run", "--db", self.enc, "--out", self.out, "--key", KEY.hex(), "--source-id", "trae-test", *extra)

    def test_run_matches_the_single_steps(self):
        rc, out = self.run_cmd()
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(os.listdir(self.out)), ["decrypted.db", "markdown", "trae.jsonl", "trae.jsonl.report.json"])
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o700)
        # the decrypted copy equals the database we encrypted
        with open(self.plain_db, "rb") as f, open(os.path.join(self.out, "decrypted.db"), "rb") as g:
            self.assertEqual(f.read(), g.read())
        # and the markdown equals rendering the plain database directly
        direct = os.path.join(self.tmp.name, "direct")
        run("export", self.plain_db, direct, *[x for s in ("s1", "s2", "s3") for x in ("--session", s)])
        self.assertEqual(read_tree(os.path.join(self.out, "markdown")), read_tree(direct))
        self.assertIn("3 sessions and 1 sub-agent runs", out)

    def test_refuses_nonempty_out(self):
        os.makedirs(self.out)
        open(os.path.join(self.out, "keep.txt"), "w").close()
        rc, _ = self.run_cmd()
        self.assertNotEqual(rc, 0)
        self.assertEqual(os.listdir(self.out), ["keep.txt"])

    def test_refuses_while_trae_has_a_wal(self):
        with open(self.enc + "-wal", "wb") as f:
            f.write(b"x")
        rc, _ = self.run_cmd()
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.exists(self.out))


if __name__ == "__main__":
    unittest.main()

"""Decrypt Trae's ai-agent database.db into a plain SQLite file.

Layout (see docs/trae-format.md): page size 4096, 80 reserved bytes per page =
IV(16) + HMAC-SHA512(64), AES-256-CBC per page, raw 32-byte key, page 1 bytes
[0:16] = salt.

Usage:
  trae-to-ctx probe   <encrypted.db> [--key HEX]
  trae-to-ctx decrypt <encrypted.db> <out.db> [--key HEX]

probe prints only structural facts (header fields, page-type bytes, HMAC check),
never page content. full checks the same header first and refuses to write if
the key does not fit, and never overwrites an existing file.
"""
import argparse
import hashlib
import hmac
import os
import struct
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE = 4096
RESERVE = 80
IV_OFF = PAGE - RESERVE          # 4016
HMAC_OFF = IV_OFF + 16           # 4032
SQLITE_MAGIC = b"SQLite format 3\x00"
# The fixed key used by the international Trae build on macOS (2025), the same on every
# install. Published in xitula/trae-session-export; pass --key if your build differs.
DEFAULT_KEY = "3605f6691095a993f03d5009c918352ef5be31ae31e8f000212b81ff058da773"


def aes_cbc_decrypt(key, iv, data):
    d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return d.update(data) + d.finalize()


def decrypt_page(key, page, pgno):
    iv = page[IV_OFF:HMAC_OFF]
    if pgno == 1:
        return SQLITE_MAGIC + aes_cbc_decrypt(key, iv, page[16:IV_OFF]) + b"\x00" * RESERVE
    return aes_cbc_decrypt(key, iv, page[:IV_OFF]) + b"\x00" * RESERVE


def hmac_candidates(key, salt):
    """SQLCipher-style HMAC key guesses. Only used to report integrity, never required."""
    hsalt = bytes(b ^ 0x3A for b in salt)
    out = {"raw-key": key}
    for it in (2, 1):
        for alg in ("sha512", "sha256", "sha1"):
            out[f"pbkdf2-{alg}-{it}"] = hashlib.pbkdf2_hmac(alg, key, hsalt, it, 32)
    return out


def hmac_ok(hkey, page, pgno, le):
    start = 16 if pgno == 1 else 0
    msg = page[start:HMAC_OFF] + struct.pack("<I" if le else ">I", pgno)
    return hmac.compare_digest(hmac.new(hkey, msg, hashlib.sha512).digest(), page[HMAC_OFF:])


def header_ok(plain):
    page_size = struct.unpack(">H", plain[16:18])[0]
    return (page_size == PAGE and plain[18] in (1, 2) and plain[19] in (1, 2)
            and plain[20] == RESERVE and plain[21:24] == b"\x40\x20\x20")


def probe(path, key):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        p1 = f.read(PAGE)
        plain = decrypt_page(key, p1, 1)
        hdr = plain[16:24]
        print("file bytes:", size, "pages:", size / PAGE)
        print("header[16:24]:", hdr.hex(" "))
        page_size = struct.unpack(">H", plain[16:18])[0]
        print("  page_size:", page_size, "write/read ver:", plain[18], plain[19],
              "reserved:", plain[20], "fractions:", plain[21:24].hex())
        ok = header_ok(plain)
        print("  header valid:", ok)
        if ok:
            ncount = struct.unpack(">I", plain[28:32])[0]
            print("  header page count:", ncount, "(file has", size // PAGE, ")")
            print("  text encoding:", struct.unpack(">I", plain[56:60])[0],
                  "schema format:", struct.unpack(">I", plain[44:48])[0])
        # b-tree page type byte: page 1 at offset 100, others at 0
        types = {}
        npages = size // PAGE
        sample = sorted({p for p in [1, *range(2, 202), npages // 2, npages] if 1 <= p <= npages})
        for pgno in sample:
            f.seek((pgno - 1) * PAGE)
            pg = f.read(PAGE)
            d = decrypt_page(key, pg, pgno)
            t = d[100] if pgno == 1 else d[0]
            types[t] = types.get(t, 0) + 1
        print("page-type byte histogram over", len(sample), "sampled pages:",
              {hex(k): v for k, v in sorted(types.items())},
              "(0x02/0x05/0x0a/0x0d are b-tree pages; 0x00 is overflow/freelist)")
        # optional HMAC check
        salt = p1[:16]
        found = None
        for name, hk in hmac_candidates(key, salt).items():
            for le in (True, False):
                if hmac_ok(hk, p1, 1, le):
                    found = (name, "le" if le else "be")
        print("HMAC scheme matched on page 1:", found)
    return ok


def full(path, key, out_path):
    if os.path.exists(out_path):
        sys.exit(f"refusing to overwrite {out_path}")
    size = os.path.getsize(path)
    npages = size // PAGE
    with open(path, "rb") as f:
        if not header_ok(decrypt_page(key, f.read(PAGE), 1)):
            sys.exit("page 1 does not decrypt to a valid SQLite header: wrong key or not a Trae database")
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # private from the first byte
    with open(path, "rb") as f, os.fdopen(fd, "wb") as out:
        for pgno in range(1, npages + 1):
            pg = f.read(PAGE)
            if len(pg) < PAGE:
                break
            out.write(decrypt_page(key, pg, pgno))
            if pgno % 100000 == 0:
                print(f"  {pgno}/{npages}", flush=True)
    print("wrote", out_path, npages, "pages")


def parse_key(hexkey):
    try:
        key = bytes.fromhex(hexkey)
    except ValueError:
        key = b""
    if len(key) != 32:
        sys.exit("--key must be 64 hex characters")
    return key


def _parser(prog, description):
    ap = argparse.ArgumentParser(prog=prog, description=description)
    ap.add_argument("db", help="a copy of Trae's encrypted database.db")
    return ap


def _key_arg(ap):
    ap.add_argument("--key", default=DEFAULT_KEY, help="64 hex characters (default: the published Trae key)")


def main_probe(argv=None, prog=None):
    ap = _parser(prog, "Check the key and page layout. Prints header facts only, no content.")
    _key_arg(ap)
    a = ap.parse_args(argv)
    return 0 if probe(a.db, parse_key(a.key)) else 1


def main_decrypt(argv=None, prog=None):
    ap = _parser(prog, "Write a decrypted copy (mode 600, never overwrites).")
    ap.add_argument("out", help="path for the plain SQLite file")
    _key_arg(ap)
    a = ap.parse_args(argv)
    full(a.db, parse_key(a.key), a.out)
    return 0

#!/usr/bin/env python3
"""V2 .dat round-trip and XOR discovery (no WeChat process)."""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

try:  # optional dep; decrypt-roundtrip tests skip without it
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
except ImportError:
    AES = None
    pad = None

import image_dat as _image_dat

_needs_crypto = unittest.skipUnless(
    _image_dat.HAS_CRYPTO, "pycryptodome not installed (optional for image layer)"
)

from image_dat import (  # noqa: E402
    V1_AES_ASCII,
    V2_MAGIC,
    _brute_chunk,
    _uin_key,
    aligned_aes_size,
    decrypt_v2,
    discover_xor,
    looks_image,
    parse_header,
    try_aes_key,
)


def _fake_jpeg(n: int = 64) -> bytes:
    # SOI + padding + EOI
    body = b"\xff\xd8\xff\xe0" + b"\x00" * (n - 6) + b"\xff\xd9"
    return body


def _pack_v2(plain: bytes, aes_key: bytes, xor_key: int, aes_size: int = 16) -> bytes:
    xor_size = 8
    aes_part = plain[:aes_size]
    raw = plain[aes_size:-xor_size]
    xor_part = bytes(b ^ xor_key for b in plain[-xor_size:])
    enc_aes = AES.new(aes_key, AES.MODE_ECB).encrypt(pad(aes_part, 16))
    header = V2_MAGIC + struct.pack("<LLB", aes_size, xor_size, 1)
    return header + enc_aes + raw + xor_part


class TestV2RoundTrip(unittest.TestCase):
    def test_align_pkcs7_extra_block(self):
        self.assertEqual(aligned_aes_size(1024), 1040)
        self.assertEqual(aligned_aes_size(16), 32)

    @_needs_crypto
    def test_decrypt_known_jpeg(self):
        xor = 0xB0
        plain = _fake_jpeg(80)
        dat = _pack_v2(plain, V1_AES_ASCII, xor, aes_size=16)
        sig, aes, xsz = parse_header(dat)
        self.assertEqual(sig, V2_MAGIC)
        out, fmt = decrypt_v2(dat, V1_AES_ASCII, xor)
        self.assertEqual(fmt, "jpg")
        self.assertTrue(out.startswith(b"\xff\xd8\xff"))
        self.assertTrue(out.endswith(b"\xff\xd9"))

    @_needs_crypto
    def test_try_aes_key_detects_jpeg(self):
        ct = AES.new(V1_AES_ASCII, AES.MODE_ECB).encrypt(pad(b"\xff\xd8\xff\xe0" + b"\x00" * 12, 16))[:16]
        self.assertEqual(try_aes_key(V1_AES_ASCII, ct), "jpg")
        self.assertIsNone(try_aes_key(b"0" * 16, ct))

    @_needs_crypto
    def test_discover_xor_from_thumbs(self):
        xor = 0xB0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sess" / "2022-11" / "Img"
            root.mkdir(parents=True)
            plain = _fake_jpeg(80)
            dat = _pack_v2(plain, V1_AES_ASCII, xor, aes_size=16)
            (root / "abc_t.dat").write_bytes(dat)
            self.assertEqual(discover_xor(Path(tmp), sample=4), xor)

    @_needs_crypto
    def test_brute_chunk_finds_planted_uin(self):
        uin = 4242
        key = _uin_key(uin, 0)
        ct = AES.new(key, AES.MODE_ECB).encrypt(pad(b"\xff\xd8\xff\xe0" + b"\x00" * 12, 16))[:16]
        hit = _brute_chunk(4200, 4300, 0, ct)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], uin)

    def test_looks_image(self):
        self.assertEqual(looks_image(b"\xff\xd8\xff\xe0"), "jpg")
        self.assertIsNone(looks_image(b"\xff\xd8\xff\x00"))  # 3-byte SOI 不够，防 2^24 假阳性
        self.assertEqual(looks_image(b"wxgfxxxx"), "wxgf")
        self.assertIsNone(looks_image(b"nope"))


class TestDamagedHeaderIsRejected(unittest.TestCase):
    """A header claiming a huge XOR tail must fail, not decrypt into garbage.

    `raw_end = len(data) - xor_size` went negative for a damaged or truncated
    .dat — the normal shape of an interrupted download — and `data[raw_end:]`
    with a negative index returns nearly the whole file instead of the trailing
    block. The call then reported success with output *longer than its input*
    (measured: 33 bytes in, 48 out) and decrypt_batch counted it as decoded.
    """

    def _with_xor_size(self, xor_size):
        plain = _fake_jpeg(80)
        dat = bytearray(_pack_v2(plain, V1_AES_ASCII, 0xB0, aes_size=16))
        dat[10:14] = struct.pack("<L", xor_size)
        return bytes(dat)

    @_needs_crypto
    def test_oversized_declared_tail_is_rejected(self):
        with self.assertRaises(ValueError):
            decrypt_v2(self._with_xor_size(99999), V1_AES_ASCII, 0xB0)

    @_needs_crypto
    def test_output_is_never_longer_than_its_input(self):
        for xor_size in (0, 1, 2, 8, 100, 4096):
            dat = self._with_xor_size(xor_size)
            try:
                out, _fmt = decrypt_v2(dat, V1_AES_ASCII, 0xB0)
            except ValueError:
                continue  # rejected outright is also acceptable
            self.assertLessEqual(
                len(out), len(dat),
                "xor_size=%d produced %d bytes from %d — a decrypt cannot grow"
                % (xor_size, len(out), len(dat)))

    @_needs_crypto
    def test_a_healthy_file_still_round_trips(self):
        out, fmt = decrypt_v2(self._with_xor_size(8), V1_AES_ASCII, 0xB0)
        self.assertEqual(fmt, "jpg")
        self.assertTrue(out.endswith(b"\xff\xd9"))


class TestBatchKeepsOnlyThePreviewItReturns(unittest.TestCase):
    """`decrypt_batch` must not hold one dict per file to return 20."""

    @_needs_crypto
    def test_record_list_is_bounded(self):
        import image_dat as idm
        from pathlib import Path as _P

        saved = (idm.attach_dir, idm.discover_xor, idm.iter_dat_files,
                 idm.decrypt_file, idm.save_keys)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = _P(tmp)
                idm.attach_dir = lambda: tmp_path
                idm.discover_xor = lambda _a: 0xB0
                idm.save_keys = lambda *a, **k: None
                idm.iter_dat_files = lambda *a, **k: [
                    tmp_path / ("f%03d_t.dat" % i) for i in range(120)]
                idm.decrypt_file = lambda src, dest, k, x: {
                    "src": str(src), "fmt": "jpg", "bytes": 1}
                out = idm.decrypt_batch(aes_key_arg="K" * 16, thumbs_only=True)
        finally:
            (idm.attach_dir, idm.discover_xor, idm.iter_dat_files,
             idm.decrypt_file, idm.save_keys) = saved
        self.assertEqual(out["decoded"], 120)
        self.assertEqual(out["scanned"], 120)
        self.assertEqual(len(out["files"]), 20, "only the preview is retained")
        self.assertTrue(out["files_truncated"])

    @_needs_crypto
    def test_limit_stops_the_scan(self):
        import image_dat as idm
        from pathlib import Path as _P

        saved = (idm.attach_dir, idm.discover_xor, idm.iter_dat_files,
                 idm.decrypt_file, idm.save_keys)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = _P(tmp)
                idm.attach_dir = lambda: tmp_path
                idm.discover_xor = lambda _a: 0xB0
                idm.save_keys = lambda *a, **k: None
                idm.iter_dat_files = lambda *a, **k: [
                    tmp_path / ("f%03d_t.dat" % i) for i in range(120)]
                idm.decrypt_file = lambda src, dest, k, x: {
                    "src": str(src), "fmt": "jpg", "bytes": 1}
                out = idm.decrypt_batch(aes_key_arg="K" * 16, limit=7, thumbs_only=True)
        finally:
            (idm.attach_dir, idm.discover_xor, idm.iter_dat_files,
             idm.decrypt_file, idm.save_keys) = saved
        self.assertEqual(out["decoded"], 7)


if __name__ == "__main__":
    unittest.main()

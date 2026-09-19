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

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

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

    def test_try_aes_key_detects_jpeg(self):
        ct = AES.new(V1_AES_ASCII, AES.MODE_ECB).encrypt(pad(b"\xff\xd8\xff\xe0" + b"\x00" * 12, 16))[:16]
        self.assertEqual(try_aes_key(V1_AES_ASCII, ct), "jpg")
        self.assertIsNone(try_aes_key(b"0" * 16, ct))

    def test_discover_xor_from_thumbs(self):
        xor = 0xB0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sess" / "2022-11" / "Img"
            root.mkdir(parents=True)
            plain = _fake_jpeg(80)
            dat = _pack_v2(plain, V1_AES_ASCII, xor, aes_size=16)
            (root / "abc_t.dat").write_bytes(dat)
            self.assertEqual(discover_xor(Path(tmp), sample=4), xor)

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


class BatchScopeTest(unittest.TestCase):
    """Mass materialisation is opt-in and projected before it writes.

    The account's `.dat` corpus is 346,285 thumbnails / 5.7 GB of source; the
    decoded set is ~3.7 GB on top of that. The first version of this command
    started that write on a bare ``extras images-decrypt`` — no window, no
    projection, no way to know the size beforehand — which is how it got run by
    accident. These tests pin the three behaviours that stop it.
    """

    def _attach(self, tmp: str, months=("2026-08", "2026-09"), per_month=2):
        for month in months:
            d = Path(tmp) / "sess" / month / "Img"
            d.mkdir(parents=True, exist_ok=True)
            for i in range(per_month):
                plain = _fake_jpeg(80 + i)
                (d / f"img{i}_t.dat").write_bytes(
                    _pack_v2(plain, V1_AES_ASCII, 0xB0, aes_size=16))
        return Path(tmp)

    def _run(self, attach, **kw):
        import image_dat

        saved = (image_dat.attach_dir, image_dat.discover_xor, image_dat.load_saved_keys)
        image_dat.attach_dir = lambda: attach
        image_dat.discover_xor = lambda *a, **k: 0xB0
        image_dat.load_saved_keys = lambda: {"xor_key": 0xB0, "aes_key_hex": V1_AES_ASCII.hex()}
        try:
            out = Path(tempfile.mkdtemp()) / "out"
            return image_dat.decrypt_batch(out_dir=out, **kw), out
        finally:
            (image_dat.attach_dir, image_dat.discover_xor,
             image_dat.load_saved_keys) = saved

    def test_unbounded_run_is_refused_and_reports_its_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            attach = self._attach(tmp)
            res, out = self._run(attach)
            self.assertFalse(res["ok"])
            self.assertEqual("window_required", res["error"])
            self.assertEqual(4, res["files"])
            self.assertGreater(res["source_bytes"], 0)
            self.assertIn("--since", res["hint"])
            self.assertFalse(out.exists(), "被拒绝的运行不能写任何文件")

    def test_dry_run_projects_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            attach = self._attach(tmp)
            res, out = self._run(attach, since="2026-09", dry_run=True)
            self.assertTrue(res["dry_run"])
            self.assertEqual(2, res["files"])
            self.assertFalse(out.exists())

    def test_windowed_run_writes_only_that_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            attach = self._attach(tmp)
            res, out = self._run(attach, since="2026-09", until="2026-09")
            self.assertTrue(res["ok"])
            self.assertEqual(2, res["decoded"])
            written = list(out.rglob("*.jpg"))
            self.assertEqual(2, len(written))
            self.assertTrue(all("2026-09" in str(p) for p in written))

    def test_all_flag_is_the_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            attach = self._attach(tmp)
            res, _ = self._run(attach, decrypt_all=True)
            self.assertTrue(res["ok"])
            self.assertEqual(4, res["decoded"])

    def test_reported_sample_is_bounded_without_holding_the_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            attach = self._attach(tmp, months=("2026-01",), per_month=30)
            res, _ = self._run(attach, since="2026-01")
            self.assertEqual(30, res["decoded"])
            self.assertEqual(20, len(res["files"]))
            self.assertTrue(res["files_truncated"])


if __name__ == "__main__":
    unittest.main()

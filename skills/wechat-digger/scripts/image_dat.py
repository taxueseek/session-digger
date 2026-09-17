#!/usr/bin/env python3
"""WeChat 4.x V2 .dat image decrypt (offline).

Format (lane2077 / CSDN, verified on local 4.1.15 files):
  [6B \\x07\\x08V2\\x08\\x07] [4B aes_size LE] [4B xor_size LE] [1B pad]
  [AES-128-ECB] [raw] [XOR]

XOR key: JPEG EOI FF D9 on *_t.dat tails (account-level, one byte).
AES key: 16-byte ASCII (V1 = md5('0').hexdigest()[:16] as ASCII).
V2 key is account-level; discover via wxid KDFs then 2^24 UIN brute.
Does not attach to WeChat. Full-size originals may be wxgf (WxAM); thumbs are JPEG.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Optional

try:  # optional dep: only the image layer needs pycryptodome (setup_deps.sh)
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
except ImportError:  # core analysis stays zero-pip; callers get an actionable error
    AES = None
    unpad = None

HAS_CRYPTO = AES is not None


def require_crypto() -> None:
    if not HAS_CRYPTO:
        raise SystemExit(
            "pycryptodome 未安装：图片层不可用。bash scripts/setup_deps.sh 安装（核心分析层不需要它）"
        )

V2_MAGIC = b"\x07\x08V2\x08\x07"
V1_MAGIC = b"\x07\x08V1\x08\x07"
V1_AES_ASCII = b"cfcd208495d565ef"  # md5("0").hexdigest()[:16]
KEYS_FILE = Path("~/.config/wechat-image-keys.json").expanduser()
BRUTE_UIN_MAX = 1 << 24
BRUTE_CHUNK = 1 << 18


def _acct_dir() -> Optional[Path]:
    cfg = Path.home() / ".config" / "wechat-local-vault.json"
    if not cfg.exists():
        return None
    data = json.loads(cfg.read_text())
    db = Path(data.get("db_base_path") or "").expanduser()
    return db.parent if db.exists() else None


def attach_dir() -> Optional[Path]:
    acct = _acct_dir()
    p = (acct / "msg" / "attach") if acct else None
    return p if p and p.exists() else None


def parse_header(data: bytes) -> Optional[tuple[bytes, int, int]]:
    if len(data) < 15:
        return None
    sig = data[:6]
    if sig not in (V2_MAGIC, V1_MAGIC):
        return None
    aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    return sig, aes_size, xor_size


def aligned_aes_size(aes_size: int) -> int:
    """PKCS7: when aes_size % 16 == 0, ciphertext is aes_size + 16."""
    a = aes_size
    a -= ~(~a % 16)
    return a


_JPEG_MARKERS = {0xE0, 0xE1, 0xE2, 0xDB, 0xC0, 0xC4, 0xDA, 0xDD, 0xEE, 0xFE}


def looks_image(buf: bytes) -> Optional[str]:
    if len(buf) >= 4 and buf.startswith(b"\xff\xd8\xff") and buf[3] in _JPEG_MARKERS:
        return "jpg"
    if buf.startswith(b"\x89PNG"):
        return "png"
    if buf.startswith(b"GIF8"):
        return "gif"
    if buf.startswith(b"RIFF") and b"WEBP" in buf[:16]:
        return "webp"
    if buf.startswith(b"wxgf"):
        return "wxgf"
    return None


def discover_xor(attach: Path, sample: int = 32) -> Optional[int]:
    """JPEG EOI FF D9 on *_t.dat tails. Same byte for the whole account."""
    votes: dict[int, int] = {}
    n = 0
    for p in attach.glob("*/*/Img/*_t.dat"):
        try:
            sz = p.stat().st_size
            with p.open("rb") as f:
                head = f.read(6)
                if head != V2_MAGIC:
                    continue
                f.seek(sz - 2)
                tail = f.read(2)
        except OSError:
            continue
        if len(tail) != 2:
            continue
        k = tail[0] ^ 0xFF
        if (tail[1] ^ 0xD9) != k:
            continue
        votes[k] = votes.get(k, 0) + 1
        n += 1
        if n >= sample:
            break
    if not votes:
        return None
    return max(votes, key=votes.get)


def _ciphertext_block(attach: Path) -> Optional[bytes]:
    for p in attach.glob("*/*/Img/*_t.dat"):
        try:
            data = p.read_bytes()[:31]
        except OSError:
            continue
        if data[:6] == V2_MAGIC and len(data) >= 31:
            return data[15:31]
    return None


def _wxid_parts() -> list[str]:
    acct = _acct_dir()
    if not acct:
        return []
    name = acct.name
    parts = [name]
    if "_" in name:
        parts.append(name.rsplit("_", 1)[0])
    return parts


def _kdf_ascii_hex16(material: bytes) -> bytes:
    return hashlib.md5(material).hexdigest()[:16].encode("ascii")


def _kdf_bin(material: bytes) -> bytes:
    return hashlib.md5(material).digest()


def _harvest_uins() -> list[int]:
    """Pull plausible 32-bit integers from login MMKV / contact extra_buffer."""
    found: set[int] = set()
    acct = _acct_dir()
    blobs: list[bytes] = []
    if acct:
        for rel in ("config/login_configv2", "config/login_config", "config/xlab/config"):
            p = acct / rel
            if p.exists() and p.stat().st_size < 2_000_000:
                blobs.append(p.read_bytes())
    try:
        from extra_layers import decrypted_root

        root = decrypted_root()
        cdb = (root / "contact" / "contact.db") if root else None
        if cdb and cdb.exists():
            import sqlite3

            con = sqlite3.connect(f"file:{cdb}?mode=ro", uri=True)
            for (buf,) in con.execute(
                "SELECT extra_buffer FROM contact WHERE extra_buffer IS NOT NULL LIMIT 8"
            ):
                if isinstance(buf, (bytes, memoryview)):
                    blobs.append(bytes(buf))
            con.close()
    except Exception:
        pass
    for blob in blobs:
        for i in range(0, max(0, len(blob) - 3)):
            u = struct.unpack_from("<I", blob, i)[0]
            if 10_000_000 <= u <= 4_000_000_000:
                found.add(u)
            u2 = struct.unpack_from(">I", blob, i)[0]
            if 10_000_000 <= u2 <= 4_000_000_000:
                found.add(u2)
    return list(found)[:5000]


def instant_aes_candidates() -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = [("v1", V1_AES_ASCII)]
    for w in _wxid_parts():
        b = w.encode()
        out.append((f"md5hex:{w[:8]}", _kdf_ascii_hex16(b)))
        out.append((f"md5bin:{w[:8]}", _kdf_bin(b)))
        out.append((f"sha256:{w[:8]}", hashlib.sha256(b).digest()[:16]))
        out.append((f"md5hex2:{w[:8]}", hashlib.md5(b).hexdigest()[16:32].encode("ascii")))
    for uin in _harvest_uins():
        for kdf in range(4):
            out.append((f"harvest:{kdf}:{uin}", _uin_key(uin, kdf)))
    return out


def try_aes_key(key: bytes, ct: bytes) -> Optional[str]:
    if len(key) != 16:
        return None
    try:
        require_crypto()
        dec = AES.new(key, AES.MODE_ECB).decrypt(ct)
    except ValueError:
        return None
    return looks_image(dec)


def _uin_key(uin: int, kdf: int) -> bytes:
    if kdf == 0:
        return hashlib.md5(str(uin).encode()).hexdigest()[:16].encode("ascii")
    if kdf == 1:
        return hashlib.md5(str(uin).encode()).digest()
    if kdf == 2:
        return hashlib.md5(uin.to_bytes(4, "little")).digest()
    return hashlib.md5(uin.to_bytes(4, "little")).hexdigest()[:16].encode("ascii")


def _brute_chunk(start: int, end: int, kdf: int, ct: bytes) -> Optional[tuple[int, int, bytes]]:
    for uin in range(start, end):
        key = _uin_key(uin, kdf)
        if try_aes_key(key, ct):
            return uin, kdf, key
    return None


def brute_uin_aes(ct: bytes, workers: Optional[int] = None) -> Optional[tuple[str, bytes]]:
    """2^24 UIN × 4 KDFs. Threaded: pycryptodome releases the GIL."""
    workers = workers or max(2, (os.cpu_count() or 4) - 1)
    total_chunks = (BRUTE_UIN_MAX + BRUTE_CHUNK - 1) // BRUTE_CHUNK
    report_every = max(1, total_chunks // 10)
    for kdf, label in (
        (0, "md5(str).hex16"),
        (1, "md5(str).bin"),
        (2, "md5(u32le).bin"),
        (3, "md5(u32le).hex16"),
    ):
        # Per-KDF progress. Four start-lines over a 16-29 minute search is
        # indistinguishable from a hang: the loop below can spin for 4-7 minutes
        # without emitting anything, so a user kills it thinking it froze. The
        # rate measured on this machine is ~70k keys/s single-threaded, and
        # threading does not help (the KDF is hashlib-bound), so the wait is
        # unavoidable — being able to see it is not.
        print(f"brute {label} 2^24 …", file=sys.stderr, flush=True)
        ranges = [
            (i, min(i + BRUTE_CHUNK, BRUTE_UIN_MAX), kdf, ct)
            for i in range(0, BRUTE_UIN_MAX, BRUTE_CHUNK)
        ]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_brute_chunk, *r) for r in ranges]
            for done, fut in enumerate(as_completed(futs), 1):
                hit = fut.result()
                if hit:
                    for f in futs:
                        f.cancel()
                    uin, kd, key = hit
                    return f"uin:{label}:{uin}", key
                if done % report_every == 0 or done == len(futs):
                    print(f"  {label}: {done * 100 // len(futs)}% "
                          f"({done}/{len(futs)} chunks)",
                          file=sys.stderr, flush=True)
    return None


def load_saved_keys() -> dict[str, Any]:
    if not KEYS_FILE.exists():
        return {}
    try:
        return json.loads(KEYS_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def save_keys(xor_key: int, aes_key: bytes, kdf: str) -> None:
    """Persist the discovered XOR/AES keys, owner-only from the first byte.

    The file holds the account's image AES key, so it must never exist in a
    wider mode. ``write_text`` followed by ``chmod`` leaves a window where the
    file exists under the process umask (0644 by default) — short, but this is
    write-once-per-account data that outlives the process, and any local reader
    that wins the race keeps the key. Create it with the mode in the same call,
    and re-assert the mode afterwards so a pre-existing wider file is fixed too.
    """
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "xor_key": xor_key,
        "aes_key_ascii": aes_key.decode("ascii", errors="replace") if all(32 <= b < 127 for b in aes_key) else None,
        "aes_key_hex": aes_key.hex(),
        "kdf": kdf,
    }
    fd = os.open(str(KEYS_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, indent=2))
    try:
        os.chmod(KEYS_FILE, 0o600)
    except OSError:
        pass


def discover_keys(attach: Optional[Path] = None, brute: bool = True) -> dict[str, Any]:
    attach = attach or attach_dir()
    if not attach:
        return {"error": "attach_missing"}
    xor = discover_xor(attach)
    if xor is None:
        return {"error": "xor_not_found", "hint": "需要 *_t.dat 缩略图"}
    ct = _ciphertext_block(attach)
    if not ct:
        return {"error": "no_v2_thumb", "xor_key": xor}

    saved = load_saved_keys()
    saved_hex = saved.get("aes_key_hex")
    if saved_hex:
        try:
            key = bytes.fromhex(saved_hex)
            if try_aes_key(key, ct):
                return {"ok": True, "xor_key": xor, "aes_kdf": saved.get("kdf") or "saved", "cached": True}
        except ValueError:
            pass

    for label, key in instant_aes_candidates():
        if try_aes_key(key, ct):
            save_keys(xor, key, label)
            return {"ok": True, "xor_key": xor, "aes_kdf": label, "cached": False}

    if brute:
        found = brute_uin_aes(ct)
        if found:
            label, key = found
            save_keys(xor, key, label)
            return {"ok": True, "xor_key": xor, "aes_kdf": label, "cached": False}

    return {
        "ok": False,
        "xor_key": xor,
        "error": "aes_not_found",
        "hint": (
            "XOR 已拿到；V2 AES 不在 wxid/V1 瞬时派生里。"
            "默认会再跑 2^24 UIN 爆破；仍失败则只能走看图后内存取 16 字节 ASCII key。"
        ),
    }


def decrypt_v2(data: bytes, aes_key: bytes, xor_key: int) -> tuple[bytes, str]:
    parsed = parse_header(data)
    if not parsed:
        raise ValueError("not v2/v1 dat")
    sig, aes_size, xor_size = parsed
    key = V1_AES_ASCII if sig == V1_MAGIC else aes_key[:16]
    aligned = aligned_aes_size(aes_size)
    offset = 15
    aes_blob = data[offset:offset + aligned]
    if len(aes_blob) != aligned:
        raise ValueError("truncated aes block")
    require_crypto()
    dec_aes = unpad(AES.new(key, AES.MODE_ECB).decrypt(aes_blob), 16)
    offset += aligned
    raw_end = len(data) - xor_size
    # The declared xor tail must not reach back into (or past) the AES block.
    # Without this guard ``raw_end`` went negative and the next slice silently
    # did the wrong thing: ``data[raw_end:]`` with a negative index returns
    # nearly the WHOLE file instead of the trailing block, so a truncated or
    # header-damaged .dat — the normal shape of an interrupted download —
    # decrypted "successfully" into garbage, producing output longer than its
    # input (measured: 33 bytes in, 48 bytes out, reported as success).
    if raw_end < offset:
        raise ValueError(
            "xor tail overlaps the aes block (len=%d, offset=%d, xor_size=%d)"
            % (len(data), offset, xor_size))
    raw = data[offset:raw_end] if offset < raw_end else b""
    xor_blob = data[raw_end:]
    dec_xor = bytes(b ^ xor_key for b in xor_blob)
    out = dec_aes + raw + dec_xor
    fmt = looks_image(out) or "bin"
    return out, fmt


def iter_dat_files(
    attach: Path,
    since: Optional[str] = None,
    until: Optional[str] = None,
    thumbs_only: bool = True,
) -> Iterable[Path]:
    pattern = "*/*/Img/*_t.dat" if thumbs_only else "*/*/Img/*.dat"
    for p in attach.glob(pattern):
        month = p.parent.parent.name  # YYYY-MM
        if since and month < since[:7]:
            continue
        if until and month > until[:7]:
            continue
        yield p


def decrypt_file(src: Path, dest: Path, aes_key: bytes, xor_key: int) -> dict[str, Any]:
    data = src.read_bytes()
    plain, fmt = decrypt_v2(data, aes_key, xor_key)
    dest = dest.with_suffix("." + fmt)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(plain)
    return {"src": str(src), "dest": str(dest), "fmt": fmt, "bytes": len(plain)}


def parse_aes_key_arg(text: str) -> bytes:
    t = text.strip()
    if len(t) == 32 and all(c in "0123456789abcdefABCDEF" for c in t):
        return bytes.fromhex(t)
    raw = t.encode("ascii")
    if len(raw) == 16:
        return raw
    raise ValueError("aes key must be 16 ASCII chars or 32 hex chars")


def decrypt_batch(
    since: Optional[str] = None,
    until: Optional[str] = None,
    thumbs_only: bool = True,
    limit: Optional[int] = None,
    out_dir: Optional[Path] = None,
    aes_key_arg: Optional[str] = None,
    brute: bool = True,
) -> dict[str, Any]:
    attach = attach_dir()
    if not attach:
        return {"error": "attach_missing"}
    xor_key = discover_xor(attach)
    if xor_key is None:
        return {"error": "xor_not_found"}
    if aes_key_arg:
        aes_key = parse_aes_key_arg(aes_key_arg)
        info = {"ok": True, "aes_kdf": "cli"}
        save_keys(xor_key, aes_key, "cli")
    else:
        info = discover_keys(attach, brute=brute)
        if not info.get("ok"):
            return info
        saved = load_saved_keys()
        aes_key = bytes.fromhex(saved["aes_key_hex"])
        xor_key = int(saved["xor_key"])
    dest_root = Path(out_dir) if out_dir else (
        Path.home() / "Library" / "Application Support" / "wechat-local-vault" / "exports" / "images"
    )
    ok = fail = skip_wxgf = 0
    # Only the first 20 records are ever returned, so the rest are counted, not
    # stored. The old form appended one dict per file for the whole run — over a
    # real attach tree that is 348k dicts (and their paths) held to build a
    # 20-item preview.
    written: list[dict[str, Any]] = []
    total = 0
    for src in iter_dat_files(attach, since, until, thumbs_only):
        if limit is not None and total >= limit:
            break
        total += 1
        if total == 1 or total % 500 == 0:
            print(f"  解密中 {total} …", file=sys.stderr, flush=True)
        rel = src.relative_to(attach)
        dest = dest_root / rel
        try:
            rec = decrypt_file(src, dest, aes_key, xor_key)
        except Exception as exc:
            fail += 1
            if len(written) < 20:
                written.append({"src": str(src), "error": str(exc)})
            continue
        if rec["fmt"] == "wxgf":
            skip_wxgf += 1
        else:
            ok += 1
        if len(written) < 20:
            written.append(rec)
    return {
        "ok": True,
        "xor_key": xor_key,
        "aes_kdf": info.get("aes_kdf"),
        "decoded": ok,
        "wxgf": skip_wxgf,
        "failed": fail,
        "scanned": total,
        "out_dir": str(dest_root),
        "files": written,
        "files_truncated": total > len(written),
    }

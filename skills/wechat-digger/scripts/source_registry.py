#!/usr/bin/env python3
"""
Data-driven source registry for WeChat chat acquisition.

对齐 session-digger 的 ENV_REGISTRY / FIND_JSONL_REGISTRY：
新增数据源 = 往 SOURCE_REGISTRY 加一行 + 实现 detect/fetch 钩子，
核心路由代码零修改。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from normalize import normalize_messages
from paths import default_data_root, digger_python, vault_cli_path


@dataclass
class SourceStatus:
    name: str
    ready: bool
    priority: int
    detail: str = ""
    path: str = ""


@dataclass
class SourceContext:
    """Resolved runtime context shared by adapters."""
    vault_script: Optional[Path] = None
    data_root: Path = field(default_factory=default_data_root)
    preferred: Optional[str] = None  # force source name


def _run_json(args: list[str], timeout: int = 60) -> Any:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        return {"error": "not_found", "message": str(e)}
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "message": f"command timed out ({timeout}s)"}
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()[:500]
        return {"error": "command_failed", "message": err, "code": r.returncode}
    text = (r.stdout or "").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # some CLIs print text + json; try last line
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{") or line.startswith("["):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return {"error": "json_decode", "message": text[:300]}


def _wx_running() -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", "WeChat"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


# ── vault adapter ─────────────────────────────────────────

def _detect_vault(ctx: SourceContext) -> SourceStatus:
    script = vault_cli_path()
    if not script or not script.is_file():
        return SourceStatus(
            "vault",
            False,
            100,
            "vault_cli missing (expected scripts/acquire/ or WECHAT_ACQUIRE_DIR)",
        )
    ctx.vault_script = script
    origin = "bundled" if script.parent.name == "acquire" else "external"
    keys = Path.home() / ".config" / "wechat-keys.json"
    vault_cfg = Path.home() / ".config" / "wechat-local-vault.json"
    if not keys.exists() and not vault_cfg.exists():
        probe = _run_json([digger_python(), str(script), "status", "--format", "json"], timeout=15)
        ready = not (isinstance(probe, dict) and probe.get("error"))
        detail = f"{origin}; no key config; status " + ("ok" if ready else "failed")
        return SourceStatus("vault", ready, 100, detail, str(script))
    probe = _run_json([digger_python(), str(script), "status", "--format", "json"], timeout=15)
    if isinstance(probe, dict) and probe.get("error"):
        return SourceStatus("vault", False, 100, probe.get("message", "status failed"), str(script))
    return SourceStatus("vault", True, 100, f"{origin} offline vault ready", str(script))


def _vault_cmd(ctx: SourceContext, *parts: str, timeout: int = 60) -> Any:
    if not ctx.vault_script:
        st = _detect_vault(ctx)
        if not st.ready:
            return {"error": "vault_unavailable", "message": st.detail}
    assert ctx.vault_script
    return _run_json([digger_python(), str(ctx.vault_script), *parts], timeout=timeout)


def _vault_resolve_chat(ctx: SourceContext, chat: str) -> dict:
    """Resolve display name → {chat_id, chat_name} via contacts; fall back to chat as-is."""
    hint = {"chat_name": chat, "chat_id": chat, "name": chat}
    if not chat:
        return hint
    raw = _vault_contacts(ctx, chat)
    contacts = []
    if isinstance(raw, dict):
        contacts = raw.get("contacts") or raw.get("data") or []
        if isinstance(contacts, dict):
            contacts = contacts.get("contacts") or []
    elif isinstance(raw, list):
        contacts = raw
    if not isinstance(contacts, list):
        return hint
    for c in contacts:
        if not isinstance(c, dict):
            continue
        names = " ".join(
            str(c.get(k) or "")
            for k in ("display_name", "nick_name", "remark", "alias", "name", "chat")
        )
        uname = str(c.get("username") or c.get("userName") or c.get("id") or "")
        if chat == uname or chat in names or (c.get("display_name") == chat):
            hint["chat_id"] = uname or chat
            hint["username"] = uname
            hint["chat_name"] = c.get("display_name") or c.get("nick_name") or chat
            hint["name"] = hint["chat_name"]
            break
    return hint


def _vault_history(ctx: SourceContext, chat: str, since: Optional[str], until: Optional[str], limit: Optional[int] = None) -> list[dict]:
    hint = _vault_resolve_chat(ctx, chat)
    target = hint.get("username") or hint.get("chat_id") or chat
    args = ["history", target, "--format", "json"]
    if since:
        args.extend(["--start-time", since])
    if until:
        args.extend(["--end-time", until])
    if limit:
        args.extend(["--limit", str(limit)])
    raw = _vault_cmd(ctx, *args, timeout=90)
    if isinstance(raw, dict) and raw.get("error"):
        # retry with original display name
        if target != chat:
            args[1] = chat
            raw = _vault_cmd(ctx, *args, timeout=90)
        if isinstance(raw, dict) and raw.get("error"):
            return []
    return normalize_messages(raw, source="vault", chat_hint=hint)


def _vault_contacts(ctx: SourceContext, query: Optional[str]) -> Any:
    args = ["contacts", "--format", "json"]
    if query:
        args.extend(["--query", query])
    return _vault_cmd(ctx, *args)


def _vault_sessions(ctx: SourceContext, limit: int = 20) -> Any:
    return _vault_cmd(ctx, "sessions", "--limit", str(limit), "--format", "json")


def _vault_search(ctx: SourceContext, keyword: str, chat: Optional[str] = None, **kwargs) -> Any:
    args = ["search", keyword, "--format", "json"]
    if chat:
        args.extend(["--chat", chat])
    return _vault_cmd(ctx, *args, timeout=90)


# ── message_fts adapter（微信自带全文索引解密副本，2022-02→今全史文本）──

def _detect_fts(ctx: SourceContext) -> SourceStatus:
    try:
        from fts_engine import fts_db_path

        p = fts_db_path()
    except Exception as e:
        return SourceStatus("fts", False, 150, f"detect failed: {e}")
    if p:
        return SourceStatus("fts", True, 150, "message_fts 全史索引就绪", str(p))
    return SourceStatus("fts", False, 150, "message_fts.db 未找到（需先跑 vault 刷新）")


def _fts_search(ctx: SourceContext, keyword: str, chat: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None, limit: Optional[int] = None, **kwargs) -> Any:
    from fts_engine import fts_search

    # with_meta=True 携带 total/has_more：截断必须显式可见，防采样偏差误判
    # limit 用 is None 判定：`--limit 0` 是合法请求（只取 total 不取正文），
    # 用 `or` 兜底会把 0 静默换成 50。
    return fts_search(keyword, chat=chat, since=since, until=until, limit=50 if limit is None else limit, with_meta=True, rank=kwargs.get("rank", "time"))


def _fts_history(ctx: SourceContext, chat: str, since: Optional[str] = None, until: Optional[str] = None, limit: Optional[int] = None, **kwargs) -> list[dict]:
    from fts_engine import fts_history

    # 全史文本层（2022-02→今，仅文本）：vault 空窗段（2026-02 前）与全周期分析的数据源
    return fts_history(chat, since=since, until=until, limit=50000 if limit is None else limit)


# ── wx-cli adapter（经 wx_bridge，禁止 --format json）────────

def _detect_wxcli(ctx: SourceContext) -> SourceStatus:
    try:
        from wx_bridge import probe_ready

        p = probe_ready(timeout=8)
    except Exception as e:
        return SourceStatus("wxcli", False, 50, f"probe failed: {e}")
    path = p.get("binary") or ""
    if not path:
        return SourceStatus("wxcli", False, 50, "wx-cli not in PATH")
    if p.get("ready"):
        return SourceStatus("wxcli", True, 50, p.get("detail") or "wx probe ok", str(path))
    # installed but not ready — still report path for doctor
    return SourceStatus("wxcli", False, 50, p.get("detail") or "wx not ready", str(path))


def _wx_result_data(result: dict) -> Any:
    if not result.get("ok"):
        return {
            "error": result.get("error") or "wx_error",
            "message": result.get("fact") or result.get("probe_error") or "wx failed",
            "action": result.get("action"),
        }
    data = result.get("data")
    # attach meta lightly without breaking list consumers
    if result.get("meta") is not None and isinstance(data, dict):
        data = dict(data)
        data["_meta"] = result["meta"]
    return data


def _wxcli_history(ctx: SourceContext, chat: str, since: Optional[str], until: Optional[str], limit: Optional[int] = None) -> list[dict]:
    from wx_bridge import run_wx

    extra = [chat, "-n", str(limit) if limit else "50000"]
    if since:
        extra.extend(["--since", since])
    if until:
        extra.extend(["--until", until])
    result = run_wx("history", extra, timeout=120)
    data = _wx_result_data(result)
    if isinstance(data, dict) and data.get("error"):
        return []
    return normalize_messages(data, source="wxcli", chat_hint={"chat_name": chat})


def _wxcli_contacts(ctx: SourceContext, query: Optional[str]) -> Any:
    from wx_bridge import run_wx

    extra: list[str] = []
    if query:
        extra.extend(["--query", query])
    return _wx_result_data(run_wx("contacts", extra, timeout=60))


def _wxcli_sessions(ctx: SourceContext, limit: int = 20) -> Any:
    from wx_bridge import run_wx

    data = _wx_result_data(run_wx("sessions", ["-n", str(limit)], timeout=60))
    if isinstance(data, list) and limit:
        return data[:limit]
    return data


def _wxcli_search(ctx: SourceContext, keyword: str, chat: Optional[str] = None, **kwargs) -> Any:
    from wx_bridge import run_wx

    extra = [keyword]
    if chat:
        # 0.3 uses --in for chat filter
        extra.extend(["--in", chat])
    return _wx_result_data(run_wx("search", extra, timeout=90))


# ── export / fixture adapters ─────────────────────────────

def _detect_export(ctx: SourceContext) -> SourceStatus:
    roots = [
        Path.home() / "Documents" / "wechat-local-vault" / "exports",
        Path.home() / "Documents" / "wechat-digests",
        ctx.data_root,
    ]
    existing = [str(p) for p in roots if p.exists()]
    if not existing:
        return SourceStatus("export", False, 20, "no export directories found")
    return SourceStatus("export", True, 20, f"export dirs: {len(existing)}", existing[0])


def _export_history(ctx: SourceContext, chat: str, since: Optional[str], until: Optional[str], limit: Optional[int] = None) -> list[dict]:
    """Load messages from prior JSON/MD exports under data_root."""
    search_roots = [
        ctx.data_root,
        Path.home() / "Documents" / "wechat-local-vault" / "exports",
        Path.home() / "Documents" / "wechat-digests",
    ]
    hits: list[Path] = []
    for root in search_roots:
        if not root.exists():
            continue
        for p in root.rglob("*.json"):
            name = p.name.lower()
            if chat.lower() in name or chat.lower() in str(p.parent).lower():
                hits.append(p)
    messages: list[dict] = []
    for p in hits[:20]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        messages.extend(normalize_messages(data, source="export", chat_hint={"chat_name": chat}))
    # optional since/until filter
    if since or until:
        from datetime import datetime

        def _ok(m: dict) -> bool:
            ts = m.get("ts")
            if ts is None:
                return True
            if since:
                s = int(datetime.strptime(since[:10], "%Y-%m-%d").timestamp())
                if ts < s:
                    return False
            if until:
                u = int(datetime.strptime(until[:10], "%Y-%m-%d").timestamp()) + 86400
                if ts > u:
                    return False
            return True

        messages = [m for m in messages if _ok(m)]
    return messages


def _detect_fixture(ctx: SourceContext) -> SourceStatus:
    # allow offline demos/tests
    env = os.environ.get("WECHAT_DIGGER_FIXTURE")
    if env and Path(env).exists():
        return SourceStatus("fixture", True, 5, "fixture from env", env)
    # package fixtures
    try:
        from paths import digger_root

        fix = digger_root() / "tests" / "fixtures" / "sample_messages.json"
        if fix.exists():
            return SourceStatus("fixture", True, 1, "package sample fixture", str(fix))
    except Exception:
        pass
    return SourceStatus("fixture", False, 1, "no fixture")


def _fixture_history(ctx: SourceContext, chat: str, since: Optional[str], until: Optional[str], limit: Optional[int] = None) -> list[dict]:
    path = os.environ.get("WECHAT_DIGGER_FIXTURE")
    if not path:
        from paths import digger_root

        path = str(digger_root() / "tests" / "fixtures" / "sample_messages.json")
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return normalize_messages(data, source="fixture", chat_hint={"chat_name": chat or "fixture"})


# ── Registry: one row per source ──────────────────────────
# priority: higher preferred. detect() returns SourceStatus.
# history/contacts/sessions/search may be None if unsupported.

SOURCE_REGISTRY: dict[str, dict[str, Any]] = {
    "fts": {
        "priority": 150,
        "label": "message_fts 全史文本检索 (2022-02→今, 含归档分片)",
        "detect": _detect_fts,
        "history": _fts_history,
        "contacts": None,
        "sessions": None,
        "search": _fts_search,
    },
    "vault": {
        "priority": 100,
        "label": "wechat-local-vault (offline)",
        "detect": _detect_vault,
        "history": _vault_history,
        "contacts": _vault_contacts,
        "sessions": _vault_sessions,
        "search": _vault_search,
    },
    "wxcli": {
        "priority": 50,
        "label": "wx-cli (online/rich ops when daemon ready)",
        "detect": _detect_wxcli,
        "history": _wxcli_history,
        "contacts": _wxcli_contacts,
        "sessions": _wxcli_sessions,
        "search": _wxcli_search,
    },
    "export": {
        "priority": 20,
        "label": "exported JSON/MD archives",
        "detect": _detect_export,
        "history": _export_history,
        "contacts": None,
        "sessions": None,
        "search": None,
    },
    "fixture": {
        "priority": 1,
        "label": "offline fixture (tests/demo)",
        "detect": _detect_fixture,
        "history": _fixture_history,
        "contacts": None,
        "sessions": None,
        "search": None,
    },
}


def _maybe_fill_fts_gap(
    router: "SourceRouter",
    result: Any,
    chat: str,
    since: Optional[str],
    until: Optional[str],
    limit: Optional[int],
) -> Any:
    """vault 非空也会丢掉 2026-02 之前：窗口伸进归档段时用 FTS 补缺口。"""
    if router.ctx.preferred:
        return result
    if isinstance(result, dict) and result.get("error"):
        return result
    data = result.get("data") if isinstance(result, dict) else result
    if not isinstance(data, list):
        return result
    src = result.get("source") if isinstance(result, dict) else None
    if src == "fts":
        return result
    try:
        from fts_engine import _as_epoch, vault_rich_start_ts

        vault_min = vault_rich_start_ts()
        since_e = _as_epoch(since)
        until_e = _as_epoch(until)
    except Exception:
        return result
    # all-time (since is None) or since before vault rich start
    if since_e is not None and since_e >= vault_min:
        return result
    if until_e is not None and until_e + 86400 <= vault_min:
        # entire window is pre-vault; empty-fallthrough already used fts
        return result
    fts_fn = SOURCE_REGISTRY.get("fts", {}).get("history")
    if not fts_fn:
        return result
    gap_until = gap_until_value(vault_min, until)
    try:
        fts_data = fts_fn(router.ctx, chat, since, gap_until, 50000 if limit is None else limit)
    except Exception:
        return result
    if not fts_data:
        return result
    vault_keys = {(m.get("ts") or 0, (m.get("text") or "")[:80]) for m in data}
    filled = []
    for m in fts_data:
        ts = m.get("ts") or 0
        if ts >= vault_min:
            continue
        key = (ts, (m.get("text") or "")[:80])
        if key in vault_keys:
            continue
        filled.append(m)
    if not filled:
        return result
    merged = filled + data
    merged.sort(key=lambda m: m.get("ts") or 0)
    return {
        "source": f"{src}+fts" if src else "vault+fts",
        "data": merged,
        "merged": True,
        "vault_count": len(data),
        "fts_filled": len(filled),
    }


def gap_until_value(vault_min: int, until: Optional[str]) -> Any:
    """FTS 缺口右边界：vault 起始前一秒，或用户 until（取更早者）。"""
    from fts_engine import _as_epoch

    until_e = _as_epoch(until)
    if until_e is not None and until_e < vault_min:
        return until
    return vault_min - 1


class SourceRouter:
    """Select and call the best available source."""

    def __init__(self, preferred: Optional[str] = None, allow_fixture: bool = False):
        self.ctx = SourceContext(preferred=preferred)
        self.allow_fixture = allow_fixture
        self._statuses: Optional[list[SourceStatus]] = None

    def detect_all(self) -> list[SourceStatus]:
        statuses = []
        for name, meta in SOURCE_REGISTRY.items():
            if name == "fixture" and not self.allow_fixture and not os.environ.get("WECHAT_DIGGER_FIXTURE"):
                # still detect for doctor, but mark separately
                st = meta["detect"](self.ctx)
                statuses.append(st)
                continue
            st = meta["detect"](self.ctx)
            statuses.append(st)
        statuses.sort(key=lambda s: (-int(s.ready), -s.priority, s.name))
        self._statuses = statuses
        return statuses

    def primary(self) -> Optional[SourceStatus]:
        statuses = self._detected()
        if self.ctx.preferred:
            for st in statuses:
                if st.name == self.ctx.preferred and st.ready:
                    return st
            return None
        for st in statuses:
            if not st.ready:
                continue
            if st.name == "fixture" and not self.allow_fixture and not os.environ.get("WECHAT_DIGGER_ALLOW_FIXTURE"):
                continue
            return st
        return None

    def _detected(self) -> list[SourceStatus]:
        """已探测结果优先。detect 会起子进程（vault_cli status）和 pgrep，
        同一 router 生命周期内重复探测纯属浪费——detect_summary 就因此把每个
        数据源探测跑了两遍。"""
        return self._statuses if self._statuses is not None else self.detect_all()

    def _engine_order(self, op: str) -> list[str]:
        """Capability-aware engine order（一行 CAPABILITY_MATRIX 消灭错误源优先）。"""
        if self.ctx.preferred:
            return [self.ctx.preferred]
        ready = {s.name for s in self._detected() if s.ready}
        if not self.allow_fixture and not os.environ.get("WECHAT_DIGGER_ALLOW_FIXTURE"):
            ready.discard("fixture")
        try:
            from wx_bridge import engines_for

            preferred_engines = engines_for(op)
        except Exception:
            preferred_engines = ["vault", "wxcli", "export", "fixture"]
        order = [e for e in preferred_engines if e in ready]
        # append other ready engines not in matrix (export/fixture for history)
        for name in sorted(ready, key=lambda n: -SOURCE_REGISTRY.get(n, {}).get("priority", 0)):
            if name not in order:
                order.append(name)
        return order

    def _call(self, op: str, *args, **kwargs) -> Any:
        order = self._engine_order(op)
        errors = []
        first_empty: Optional[dict] = None
        for name in order:
            meta = SOURCE_REGISTRY.get(name)
            if not meta:
                continue
            fn: Optional[Callable] = meta.get(op)
            if not fn:
                errors.append({"source": name, "error": "unsupported_op", "op": op})
                continue
            try:
                result = fn(self.ctx, *args, **kwargs)
            except Exception as e:
                errors.append({"source": name, "error": "exception", "message": str(e)})
                continue
            if isinstance(result, dict) and result.get("error"):
                errors.append({"source": name, **result})
                continue
            # 空结果≠有覆盖：vault 只有 2026-02 后，该段查询应继续尝试 fts 全史层
            if isinstance(result, list) and not result:
                if first_empty is None:
                    first_empty = {"source": name, "data": result}
                continue
            if result is None:
                errors.append({"source": name, "error": "empty", "op": op})
                continue
            if result == {} and op in ("contacts", "sessions"):
                errors.append({"source": name, "error": "empty", "op": op})
                continue
            return {"source": name, "data": result}
        if first_empty is not None:
            return first_empty
        return {
            "error": "all_sources_failed",
            "fact": "所有可用数据源均未能完成该操作。",
            "action": "检查 vault 解密 / 微信是否运行 / 是否有导出文件；运行: python3 scripts/wd.py doctor",
            "forbidden": "不要编造聊天数据。",
            "attempts": errors,
        }

    def history(self, chat: str, since: Optional[str] = None, until: Optional[str] = None, limit: Optional[int] = None) -> Any:
        result = self._call("history", chat, since, until, limit)
        return _maybe_fill_fts_gap(self, result, chat, since, until, limit)

    def contacts(self, query: Optional[str] = None) -> Any:
        return self._call("contacts", query)

    def sessions(self, limit: int = 20) -> Any:
        return self._call("sessions", limit)

    def search(self, keyword: str, chat: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None, limit: Optional[int] = None, **kwargs) -> Any:
        return self._call("search", keyword, chat=chat, since=since, until=until, limit=limit, **kwargs)


def detect_summary(preferred: Optional[str] = None, allow_fixture: bool = True) -> dict:
    router = SourceRouter(preferred=preferred, allow_fixture=allow_fixture)
    statuses = router.detect_all()
    primary = router.primary()
    engines: dict[str, list[str]] = {}
    try:
        from wx_bridge import CAPABILITY_MATRIX, inventory as wx_inventory

        ready_names = {s.name for s in statuses if s.ready}
        for op, prefs in CAPABILITY_MATRIX.items():
            engines[op] = [e for e in prefs if e in ready_names]
        wx_info = wx_inventory()
    except Exception:
        wx_info = {"ready": False}
    return {
        "primary": primary.name if primary else None,
        "primary_detail": primary.detail if primary else "no ready source",
        "available": [s.name for s in statuses if s.ready],
        "engines_by_op": engines,
        "wx": {
            "binary": wx_info.get("binary"),
            "version": wx_info.get("version"),
            "ready": wx_info.get("ready"),
            "detail": wx_info.get("detail"),
            "wechat_process": wx_info.get("wechat_process"),
        },
        "sources": [
            {
                "name": s.name,
                "ready": s.ready,
                "priority": s.priority,
                "detail": s.detail,
                "path": s.path,
            }
            for s in statuses
        ],
    }

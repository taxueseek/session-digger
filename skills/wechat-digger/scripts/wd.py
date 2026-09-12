#!/usr/bin/env python3
"""
wechat-digger unified CLI.

Usage:
  python3 wd.py detect | doctor | acquire-info
  python3 wd.py keys list-dbs|match|capture [--duration N]
  python3 wd.py decrypt [--mode incremental|full]
  python3 wd.py refresh          # keys-match + decrypt incremental (safe default)
  python3 wd.py vault status|sessions|moments|favorites|...
  python3 wd.py sessions|contacts|history|search|index|analyze|digest|export-msg
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# ensure sibling imports work when invoked as script
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from acquire_bridge import inventory as acquire_inventory  # noqa: E402
from acquire_bridge import run_passthrough  # noqa: E402
from analyze import ANALYSIS_MODES, run_pipeline  # noqa: E402
from health import doctor  # noqa: E402
from index_builder import connect, search as index_search, stats as index_stats, upsert_messages  # noqa: E402
from normalize import normalize_messages  # noqa: E402
from paths import default_data_root, default_index_path  # noqa: E402
from render import render_summary  # noqa: E402
from report import render_lab_report  # noqa: E402
from runtime_contract import check_python, require_python  # noqa: E402
from source_registry import SourceRouter, detect_summary  # noqa: E402

# Refuse to start on an interpreter the code cannot run on, with an
# actionable message instead of a bare AttributeError mid-pipeline.
require_python()


def _print(data: Any, as_text: bool = False) -> None:
    if as_text and isinstance(data, str):
        print(data)
        return
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _default_since(days: int = 7) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def cmd_detect(args) -> int:
    _print(detect_summary(preferred=args.source, allow_fixture=args.allow_fixture))
    return 0


def cmd_doctor(args) -> int:
    report = doctor(allow_fixture=True)
    _print(report)
    return 0 if report.get("ok") else 2


def cmd_sessions(args) -> int:
    r = SourceRouter(preferred=args.source, allow_fixture=args.allow_fixture)
    out = r.sessions(limit=args.limit)
    _print(out)
    return 0 if "error" not in out else 1


def cmd_contacts(args) -> int:
    r = SourceRouter(preferred=args.source, allow_fixture=args.allow_fixture)
    out = r.contacts(query=args.query)
    _print(out)
    return 0 if "error" not in out else 1


def _fetch_history(args, limit: Optional[int] = None) -> tuple[Optional[list], Optional[dict]]:
    r = SourceRouter(preferred=args.source, allow_fixture=getattr(args, "allow_fixture", False))
    since = args.since
    until = getattr(args, "until", None)
    if since is None and not getattr(args, "all_time", False):
        since = _default_since(7)
    out = r.history(args.chat, since=since, until=until, limit=limit)
    if isinstance(out, dict) and out.get("error"):
        return None, out
    data = out.get("data") if isinstance(out, dict) else out
    if not isinstance(data, list):
        return None, {"error": "bad_history", "message": "history is not a list", "raw": out}
    meta = {"source": out.get("source") if isinstance(out, dict) else None, "since": since, "until": until}
    return data, meta


def cmd_history(args) -> int:
    msgs, meta = _fetch_history(args)
    if msgs is None:
        _print(meta)
        return 1
    _print({"source": meta.get("source"), "count": len(msgs), "messages": msgs if args.full else msgs[: args.limit]})
    return 0


def cmd_search(args) -> int:
    if getattr(args, "group_by", None) == "chat":
        # 聚合视图：关键词 × 会话分布（诊断/盘点主视图，fts 独有）
        if getattr(args, "source", None) not in (None, "fts"):
            _print({"error": "unsupported_source", "message": "--group-by 仅 fts 引擎支持", "action": "去掉 --source 或用 --source fts"})
            return 1
        from fts_engine import fts_group
        _print({"source": "fts", "data": fts_group(args.keyword, chat=args.chat, since=getattr(args, "since", None), until=getattr(args, "until", None), top=args.limit)})
        return 0
    if args.use_index:
        conn = connect(default_index_path())
        hits = index_search(conn, args.keyword, chat=args.chat, limit=args.limit)
        _print({"source": "index", "count": len(hits), "hits": hits})
        return 0
    r = SourceRouter(preferred=args.source, allow_fixture=args.allow_fixture)
    out = r.search(args.keyword, chat=args.chat, since=getattr(args, "since", None), until=getattr(args, "until", None), limit=args.limit)
    _print(out)
    return 0 if not isinstance(out, dict) or "error" not in out else 1


def cmd_index(args) -> int:
    if getattr(args, "input", None):
        msgs = _load_input_file(args.input)
        meta = {"source": "file", "path": args.input}
        if not msgs and not Path(args.input).exists():
            _print({"error": "bad_input", "path": args.input})
            return 1
    else:
        if not getattr(args, "chat", None):
            _print({"error": "missing_chat", "message": "不指定 --input 时必须用 --chat 指定群名/联系人", "action": "wd index --chat \"群名\" --all-time"})
            return 1
        msgs, meta = _fetch_history(args, limit=getattr(args, "limit", None))
        if msgs is None:
            _print(meta)
            return 1
    # ensure chat_name filled
    for m in msgs:
        m.setdefault("chat_name", args.chat)
        m.setdefault("chat_id", m.get("chat_id") or args.chat)
    conn = connect(Path(args.db) if args.db else default_index_path())
    result = upsert_messages(conn, msgs, source=meta.get("source") or "unknown")
    result["index"] = str(default_index_path() if not args.db else args.db)
    result["stats"] = index_stats(conn)
    _print(result)
    return 0


def _load_input_file(path: str) -> list:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return normalize_messages(raw, source="file")


def cmd_coverage(args) -> int:
    """数据覆盖审计：vault 富文本层 + fts 全史文本层的跨度/总量/空洞。"""
    from fts_engine import coverage_data

    _print(coverage_data())
    return 0


def cmd_analyze(args) -> int:
    # --input always wins (offline / replay path)
    if getattr(args, "snapshot", False) and not args.chat:
        _print({"error": "missing_chat", "message": "--snapshot 需要 --chat 区分快照文件（--input 回放模式同样必填）", "action": "加 --chat \"群名\""})
        return 1
    if getattr(args, "input", None):
        msgs = _load_input_file(args.input)
        meta = {"source": "file", "path": args.input}
    else:
        msgs, meta = _fetch_history(args, limit=getattr(args, "limit", None))
        if msgs is None:
            _print(meta)
            return 1
    analysis = run_pipeline(msgs, mode=args.mode, self_hint=getattr(args, "self_hint", None))
    analysis["meta"] = {"chat": args.chat, **(meta or {})}
    # 跨次画像快照（knowledge-extractor 精华 lite）：保存 + 与上次 diff
    if getattr(args, "snapshot", False):
        snap_root = default_data_root() / "snapshots"
        snap_root.mkdir(parents=True, exist_ok=True)
        snap_path = snap_root / (_safe_name(args.chat or "unknown") + ".json")
        prev = None
        if snap_path.exists():
            try:
                prev = json.loads(snap_path.read_text(encoding="utf-8"))
            except Exception:
                prev = None
        members = sorted({(m.get("nickname") or m.get("sender")) for m in msgs if m.get("nickname") or m.get("sender")})
        snap = {
            "chat": args.chat,
            "savedAt": datetime.now().isoformat(timespec="seconds"),
            "stats": analysis.get("stats"),
            "topics": [t.get("title") for t in (analysis.get("topics") or [])],
            "members": members,
        }
        diff: dict[str, Any] = {"firstRun": True}
        if prev:
            pt, ct = set(prev.get("topics") or []), set(snap["topics"])
            pm, cm = set(prev.get("members") or []), set(snap["members"])
            diff = {
                "firstRun": False,
                "newTopics": sorted(ct - pt),
                "goneTopics": sorted(pt - ct),
                "newMembers": sorted(cm - pm),
                "messageDelta": (snap["stats"] or {}).get("totalMessages", 0) - (prev.get("stats") or {}).get("totalMessages", 0),
            }
        snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        analysis["snapshot"] = {"saved": str(snap_path), "diff": diff}

    if args.output and args.output != "-":
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
        _print({"written": str(out_path), "stats": analysis.get("stats")})
    else:
        _print(analysis)
    # persist lightweight insight history
    if args.save_history:
        root = default_data_root() / _safe_name(args.chat)
        root.mkdir(parents=True, exist_ok=True)
        hist = {
            "chat": args.chat,
            "lastMessageTimestamp": max((m.get("ts") or 0) for m in msgs) if msgs else None,
            "topics": analysis.get("topics") or [],
            "profileVersion": 1,
            "savedAt": datetime.now().isoformat(timespec="seconds"),
        }
        (root / "insight-history.json").write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:80] or "chat"


def cmd_digest(args) -> int:
    if getattr(args, "input", None):
        msgs = _load_input_file(args.input)
        meta = {"source": "file", "path": args.input}
    else:
        msgs, meta = _fetch_history(args)
        if msgs is None:
            _print(meta)
            return 1
    analysis = run_pipeline(msgs, mode="full")
    text = render_summary(analysis, chat_name=args.chat, messages=msgs, version=args.version)
    out_dir = Path(args.output_dir) if args.output_dir else default_data_root() / _safe_name(args.chat) / "digests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"digest-{args.version}-{stamp}.md"
    out_path.write_text(text, encoding="utf-8")
    # machine side
    (out_dir / f"analysis-{stamp}.json").write_text(
        json.dumps({"analysis": analysis, "meta": meta, "count": len(msgs)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if args.print_body:
        print(text)
        print(f"\n---\nwritten: {out_path}")
    else:
        _print({
            "written": str(out_path),
            "source": (meta or {}).get("source"),
            "count": len(msgs),
            "overview": text.splitlines()[0] if text else "",
            "preview": "\n".join(text.splitlines()[:12]),
        })
    return 0


def cmd_report(args) -> int:
    """
    实验室/关系报告（吸收 welink 报表 + 垂直关系 skill 精华）。
    flavor=lab|dyad；分析 mode 默认 lab / dyad。
    """
    if getattr(args, "input", None):
        msgs = _load_input_file(args.input)
        meta = {"source": "file", "path": args.input}
    else:
        msgs, meta = _fetch_history(args)
        if msgs is None:
            _print(meta)
            return 1
    flavor = args.flavor or "lab"
    mode = args.mode or ("dyad" if flavor == "dyad" else "lab")
    analysis = run_pipeline(msgs, mode=mode)
    text = render_lab_report(analysis, chat_name=args.chat, messages=msgs, flavor=flavor)
    out_dir = Path(args.output_dir) if args.output_dir else default_data_root() / _safe_name(args.chat) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"report-{flavor}-{stamp}.md"
    out_path.write_text(text, encoding="utf-8")
    (out_dir / f"analysis-{flavor}-{stamp}.json").write_text(
        json.dumps(
            {"analysis": analysis, "meta": meta, "count": len(msgs), "flavor": flavor},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if args.print_body:
        print(text)
        print(f"\n---\nwritten: {out_path}")
    else:
        _print({
            "written": str(out_path),
            "flavor": flavor,
            "mode": mode,
            "source": (meta or {}).get("source"),
            "count": len(msgs),
            "preview": "\n".join(text.splitlines()[:16]),
        })
    return 0


def cmd_chats(args) -> int:
    """会话质量分层排查：识别线报/广告类噪音群，附证据理由；确认排除走 followups --feedback。"""
    from chat_quality import classify_chats
    from fts_engine import fts_recent

    window_days = args.window or 30
    since = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    msgs = fts_recent(since=since)
    if not msgs:
        _print({"error": "no_data", "message": "fts 层窗口内无数据", "action": "先跑 wd.py refresh，或放宽 --window"})
        return 1
    by_chat: dict[str, list] = {}
    for m in msgs:
        by_chat.setdefault(m.get("chat_id") or "?", []).append(m)
    classified = classify_chats(by_chat)
    from collections import Counter

    level_order = {"noise": 0, "low": 1, "normal": 2}
    items = [{**info, "chat_id": cid} for cid, info in classified.items()]
    if args.level != "all":
        items = [it for it in items if it["level"] == args.level]
    items.sort(key=lambda it: (level_order.get(it["level"], 3), -it["score"], -it["stats"]["messages"]))
    summary = Counter(it["level"] for it in classified.values())
    _print({
        "window": {"since": since, "days": window_days},
        "totalChats": len(classified),
        "summary": dict(summary),
        "count": len(items),
        "chats": items[: args.limit],
        "next": "确认排除：wd.py followups --feedback chat:<chat_id> --verdict ignore（该群后续信号不再进状态机）",
    })
    return 0


def cmd_roundup(args) -> int:
    """跨会话信息整合成文：--match 同类群汇总 / --keyword 主题提取 / --mine 我的发言梳理。"""
    from chat_quality import classify_chats
    from fts_engine import fts_recent
    from roundup import render_group_article, render_mine_article
    from paths import default_data_root

    mode = "mine" if getattr(args, "mine", False) else ("keyword" if args.keyword else ("match" if args.match else None))
    if not mode:
        _print({"error": "missing_mode", "message": "三种模式三选一：--match 群名 / --keyword 关键词 / --mine", "action": "例：wd.py roundup --match 线报 --window 7"})
        return 1
    if mode == "mine" and not args.self_hint:
        _print({"error": "missing_self", "message": "--mine 需要 --self 识别本人发言", "action": '加 --self "我的昵称"'})
        return 1

    window_days = args.window or (90 if mode == "mine" else 7)
    since = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    msgs = fts_recent(since=since)
    if not msgs:
        _print({"error": "no_data", "message": "fts 层窗口内无数据", "action": "先跑 wd.py refresh，或放宽 --window"})
        return 1
    by_chat: dict[str, list] = {}
    for m in msgs:
        by_chat.setdefault(m.get("chat_id") or "?", []).append(m)

    excluded: dict[str, dict] = {}
    if mode == "match":
        low = args.match.lower()
        chosen = {
            cid: ms for cid, ms in by_chat.items()
            if low in (ms[0].get("chat_name") or "").lower() or low in cid.lower()
        }
        if not chosen:
            _print({"error": "chat_not_found", "match": args.match, "action": "用 wd.py chats 查看窗口内会话名"})
            return 1
        title = f"{args.match} 相关会话汇总"
    else:
        classified = classify_chats(by_chat)
        if mode == "keyword":
            kw = args.keyword.lower()
            filtered = {
                cid: [m for m in ms if kw in (m.get("text") or "").lower()]
                for cid, ms in by_chat.items()
            }
            filtered = {cid: ms for cid, ms in filtered.items() if ms}
            if not filtered:
                _print({"error": "no_match", "keyword": args.keyword, "action": "放宽关键词或 --window"})
                return 1
            if args.include_noise:
                chosen = filtered
            else:
                noise_ids = {cid for cid, info in classified.items() if info.get("level") == "noise"}
                chosen = {cid: ms for cid, ms in filtered.items() if cid not in noise_ids}
                excluded = {classified[cid].get("name") or cid: classified[cid] for cid in filtered if cid in noise_ids}
            title = f"「{args.keyword}」跨会话主题汇总"
        else:  # mine
            if args.include_noise:
                chosen = by_chat
            else:
                noise_ids = {cid for cid, info in classified.items() if info.get("level") == "noise"}
                chosen = {cid: ms for cid, ms in by_chat.items() if cid not in noise_ids}
            title = ""

    if mode == "mine":
        text = render_mine_article(chosen, args.self_hint, {"days": window_days})
    else:
        flat = [m for ms in chosen.values() for m in ms]
        text = render_group_article(title, flat, {"days": window_days}, excluded_noise=excluded or None)

    out_dir = Path(args.output_dir) if args.output_dir else default_data_root() / "roundups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"roundup-{mode}-{stamp}.md"
    out_path.write_text(text, encoding="utf-8")
    if args.print_body:
        print(text)
        print(f"\n---\nwritten: {out_path}")
    else:
        _print({
            "written": str(out_path),
            "mode": mode,
            "chats": len(chosen),
            "messages": sum(len(ms) for ms in chosen.values()),
            "noiseExcludedChats": len(excluded),
            "preview": "\n".join(text.splitlines()[:12]),
        })
    return 0


def cmd_followups(args) -> int:
    """商机/承诺跟进（hub 精华 lite）：scan 并入状态机 / list 条目 / triage 分流 / feedback 学习。"""
    from followups import (
        FEEDBACK_VERDICTS,
        add_feedback,
        load_state,
        open_count,
        reactivations,
        save_state,
        scan_messages,
        sync_signals,
        today_actions,
        triage,
    )
    from fts_engine import fts_recent
    from paths import default_data_root

    state_path = default_data_root() / "followups.json"
    state = load_state(state_path)

    if getattr(args, "triage", None) is not None:
        if not args.decision:
            _print({"error": "missing_decision", "message": "--triage 必须配 --decision", "action": "加 --decision pursue|wait|pause|ignore|won|lost"})
            return 1
        item = triage(state, args.triage, args.decision, note=args.note or "")
        if not item:
            _print({"error": "item_not_found", "id": args.triage, "action": "先 --list 查看条目 ID"})
            return 1
        save_state(state_path, state)
        _print({"triaged": item})
        return 0

    if getattr(args, "feedback", None):
        if not args.verdict:
            _print({"error": "missing_verdict", "message": "--feedback 必须配 --verdict", "action": f"加 --verdict {'|'.join(sorted(FEEDBACK_VERDICTS))}"})
            return 1
        add_feedback(state, args.feedback, args.verdict, note=args.note or "")
        save_state(state_path, state)
        _print({"feedback": {"target": args.feedback, "verdict": args.verdict}})
        return 0

    if getattr(args, "purge_excluded", False):
        # 历史残留清理：被排除会话的条目一旦入库就会永久留存——扫描只跳过
        # 这些会话，不会删除已存在的条目。实测 607 条中 363 条（59.8%）来自
        # 现已判定的 noise/low 群。默认 dry-run，--yes 才真正删除并先备份。
        from chat_quality import FOLLOWUP_EXCLUDED_LEVELS, classify_chats

        window_days = args.window or 30
        since = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
        msgs = fts_recent(since=since)
        if not msgs:
            _print({"error": "no_data", "message": "fts 层窗口内无数据", "action": "先跑 wd.py refresh"})
            return 1
        by_chat: dict[str, list] = {}
        for m in msgs:
            by_chat.setdefault(m.get("chat_id") or "?", []).append(m)
        classified = classify_chats(by_chat)
        excluded = {
            cid for cid, info in classified.items()
            if info.get("level") in FOLLOWUP_EXCLUDED_LEVELS
        }
        items = state.get("items", [])
        doomed = [it for it in items if it.get("chat_id") in excluded]
        survivors = [it for it in items if it.get("chat_id") not in excluded]

        if not getattr(args, "yes", False):
            _print({
                "dryRun": True,
                "wouldRemove": len(doomed),
                "wouldKeep": len(survivors),
                "excludedChats": len(excluded),
                "levels": sorted(FOLLOWUP_EXCLUDED_LEVELS),
                "topChats": [
                    {"name": classified.get(c, {}).get("name") or c,
                     "level": classified.get(c, {}).get("level"),
                     "items": sum(1 for it in doomed if it.get("chat_id") == c)}
                    for c in sorted(excluded, key=lambda x: -sum(1 for it in doomed if it.get("chat_id") == x))[:8]
                ],
                "action": "确认后加 --yes 执行（会先写 .bak 备份）",
            })
            return 0

        backup = state_path.with_suffix(state_path.suffix + ".bak")
        try:
            backup.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            _print({"error": "backup_failed", "detail": str(exc),
                    "action": "未做任何修改；请检查 state 目录写入权限"})
            return 1
        state["items"] = survivors
        save_state(state_path, state)
        _print({
            "dryRun": False,
            "removed": len(doomed),
            "kept": len(survivors),
            "excludedChats": len(excluded),
            "backup": str(backup),
            "byStatus": open_count(state),
        })
        return 0

    if getattr(args, "list_only", False):
        items = state.get("items", [])
        if args.status != "all":
            from followups import OPEN_STATUSES

            items = [it for it in items if it.get("status") in OPEN_STATUSES]
        _print({"count": len(items), "byStatus": open_count(state), "items": items[-args.limit:]})
        return 0

    # scan：fts 全史文本层一次扫描窗口段，按会话分组提信号
    window_days = args.window or 30
    since = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    msgs = fts_recent(since=since)
    if not msgs:
        _print({"error": "no_data", "message": "fts 层窗口内无数据", "action": "先跑 wd.py refresh 刷新 vault，或放宽 --window"})
        return 1
    by_chat: dict[str, list] = {}
    for m in msgs:
        by_chat.setdefault(m.get("chat_id") or "?", []).append(m)

    # 噪音群排除：自动分层 + 用户已确认 ignore（显式 --chat 点名时不排除）
    from chat_quality import classify_chats
    from relevance import RelevanceProfile

    # 用户相关性策略（可选配置文件；缺失即空策略 = 与引入前行为一致）
    profile_path = Path(args.relevance) if getattr(args, "relevance", None) else None
    profile = RelevanceProfile.load(profile_path)

    classified = classify_chats(by_chat)
    latest_chat_verdict: dict[str, str] = {}
    for fb in state.get("feedback", []):
        target = fb.get("target") or ""
        if target.startswith("chat:"):
            latest_chat_verdict[target[5:]] = fb.get("verdict") or ""
    confirmed_noise = {cid for cid, v in latest_chat_verdict.items() if v in ("ignore", "false_positive")}

    if args.chat:
        low = args.chat.lower()
        by_chat = {
            cid: ms for cid, ms in by_chat.items()
            if low in (ms[0].get("chat_name") or "").lower() or low in cid.lower()
        }
        if not by_chat:
            _print({"error": "chat_not_found", "chat": args.chat, "action": "核对群名/联系人"})
            return 1
        noise_excluded = {"chats": 0, "messages": 0, "top": [], "note": "显式 --chat 点名，不做噪音排除"}
    else:
        # 商机扫描比 roundup 更严格：`low`（低信息群）同样排除。
        # roundup 保留 low 是刻意的（成文时需要素材广度），但 low 群实测
        # 全是线报/羊毛群，其"商机"信号 100% 是电商转发——不排除则
        # followups 条目 54-60% 为噪声（实测 607 条中 330+ 条）。
        from chat_quality import FOLLOWUP_EXCLUDED_LEVELS

        noise_ids = {
            cid for cid, info in classified.items()
            if info.get("level") in FOLLOWUP_EXCLUDED_LEVELS
        } | (confirmed_noise & set(by_chat))
        if getattr(args, "include_noise", False):
            noise_ids = confirmed_noise & set(by_chat)  # 用户确认的仍然生效
        excluded_ids = [cid for cid in by_chat if cid in noise_ids]
        noise_excluded = {
            "chats": len(excluded_ids),
            "messages": sum(len(by_chat[c]) for c in excluded_ids),
            "levels": sorted(FOLLOWUP_EXCLUDED_LEVELS),
            "top": [
                {"name": classified[c].get("name"), "level": classified[c].get("level"),
                 "score": classified[c].get("score"), "confirmed": c in confirmed_noise}
                for c in sorted(excluded_ids, key=lambda x: -classified[x].get("score", 0))[:8]
            ],
        }
        by_chat = {cid: ms for cid, ms in by_chat.items() if cid not in noise_ids}

    stats = {"created": 0, "updated": 0, "skipped": 0}
    to_reply, waiting = [], []
    for chat_id, chat_msgs in by_chat.items():
        res = scan_messages(chat_msgs, self_hint=args.self_hint)
        name = chat_msgs[0].get("chat_name") or chat_id
        chat_type = chat_msgs[0].get("chat_type")
        if res["signals"]:
            s = sync_signals(state, name, chat_id, res["signals"], profile=profile)
            for k in stats:
                stats[k] += s[k]
        # 群聊只报点名，私聊全部报（防群 @ 刷屏）
        if res["toReply"] and (chat_type != "group" or "点名" in res["toReply"]["note"]):
            to_reply.append({"chat": name, "chat_id": chat_id, **res["toReply"]})
        if res["waiting"]:
            waiting.append({"chat": name, "chat_id": chat_id, **res["waiting"]})
    save_state(state_path, state)

    private_chats = {cid: ms for cid, ms in by_chat.items() if ms[0].get("chat_type") != "group"}
    out = {
        "asOf": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": {"since": since, "days": window_days},
        "chatsScanned": len(by_chat),
        "messagesScanned": sum(len(ms) for ms in by_chat.values()),
        "noiseExcluded": noise_excluded,
        "relevanceProfile": profile.describe(),
        "today": today_actions(state, limit=args.limit),
        "toReply": sorted(to_reply, key=lambda x: -(x.get("ts") or 0))[: args.limit],
        "waiting": waiting[: args.limit],
        "reactivations": reactivations(state, private_chats, self_hint=args.self_hint),
        "sync": stats,
        "byStatus": open_count(state),
        "stateFile": str(state_path),
    }
    _print(out)
    return 0


def cmd_export_msg(args) -> int:
    msgs, meta = _fetch_history(args)
    if msgs is None:
        _print(meta)
        return 1
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"meta": meta, "chat": args.chat, "messages": msgs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _print({"written": str(path), "count": len(msgs), "source": (meta or {}).get("source")})
    return 0


def cmd_acquire_info(args) -> int:
    _print(acquire_inventory())
    return 0


def cmd_setup_deps(args) -> int:
    """Create skill .venv and install acquire requirements."""
    import subprocess
    from paths import digger_root

    script = digger_root() / "scripts" / "setup_deps.sh"
    if not script.is_file():
        _print({"error": "missing_setup_script", "path": str(script)})
        return 2
    r = subprocess.run(["bash", str(script)], timeout=args.timeout or 300)
    return r.returncode


def cmd_keys(args) -> int:
    """list-dbs | match | capture — never print secrets in summary mode."""
    action = args.action
    if action == "list-dbs":
        # passthrough text is OK (no keys)
        return run_passthrough("keys", ["--list-dbs"], timeout=args.timeout)
    if action == "match":
        return run_passthrough(
            "keys",
            ["--match-only", "--targets", "all", "--reuse-log"],
            timeout=args.timeout or 120,
        )
    if action == "capture":
        argv = ["--targets", "all"]
        if args.duration:
            argv.extend(["--duration", str(args.duration)])
        # long-running, stream output
        return run_passthrough("keys", argv, timeout=args.timeout or 600)
    _print({"error": "unknown_keys_action", "action": action})
    return 1


def cmd_decrypt(args) -> int:
    mode = args.mode or "incremental"
    return run_passthrough("decrypt", ["--mode", mode], timeout=args.timeout or 600)


def cmd_refresh(args) -> int:
    """Safe daily path: try key match, then incremental decrypt, then doctor."""
    steps = []
    code_match = run_passthrough(
        "keys",
        ["--match-only", "--targets", "all", "--reuse-log"],
        timeout=args.timeout or 120,
    )
    steps.append({"step": "keys-match", "code": code_match})
    code_dec = run_passthrough(
        "decrypt",
        ["--mode", "incremental"],
        timeout=args.timeout or 600,
    )
    steps.append({"step": "decrypt-incremental", "code": code_dec})
    report = doctor(allow_fixture=True)
    _print({"steps": steps, "doctor_ok": report.get("ok"), "primary": report.get("sources", {}).get("primary")})
    return 0 if code_dec == 0 else code_dec


def cmd_vault(args) -> int:
    """Passthrough to vault_cli with unified discovery."""
    sub = args.vault_cmd
    extra = list(args.vault_args or [])
    # default json for machine-friendly when --json
    if args.json and "--format" not in extra:
        extra.extend(["--format", "json"])
    return run_passthrough("vault_cli", [sub, *extra], timeout=args.timeout or 120)


def cmd_moments(args) -> int:
    """双引擎：默认 vault moments；--source wxcli 或 vault 失败时 sns-feed。"""
    prefer = getattr(args, "source", None)
    if prefer == "wxcli" or prefer == "wx":
        from wx_bridge import run_wx

        extra: list[str] = []
        if args.name:
            extra.extend(["--user", args.name])
        if args.start:
            extra.extend(["--since", args.start])
        if args.limit:
            extra.extend(["-n", str(args.limit)])
        result = run_wx("sns-feed", extra, timeout=args.timeout or 90)
        _print(result)
        return 0 if result.get("ok") else 1

    argv = ["moments"]
    if args.name:
        argv.extend(["--name", args.name])
    if args.start:
        argv.extend(["--start", args.start])
    argv.extend(["--format", args.format])
    code = run_passthrough("vault_cli", argv, timeout=args.timeout or 90)
    if code == 0:
        return 0
    # fallback wx sns-feed
    from wx_bridge import run_wx

    extra = []
    if args.name:
        extra.extend(["--user", args.name])
    if args.start:
        extra.extend(["--since", args.start])
    result = run_wx("sns-feed", extra, timeout=args.timeout or 90)
    if result.get("ok"):
        _print({"source": "wxcli", "via": "sns-feed", **{k: result[k] for k in ("data", "meta") if k in result}})
        return 0
    return code


def cmd_favorites(args) -> int:
    if getattr(args, "source", None) in ("wxcli", "wx"):
        from wx_bridge import run_wx

        extra: list[str] = []
        if args.type:
            extra.extend(["--type", args.type])
        if args.query:
            extra.extend(["-q", args.query])
        if args.limit:
            extra.extend(["-n", str(args.limit)])
        result = run_wx("favorites", extra, timeout=args.timeout or 90)
        _print(result)
        return 0 if result.get("ok") else 1
    argv = ["favorites"]
    if args.type:
        argv.extend(["--type", args.type])
    if args.query:
        argv.extend(["--query", args.query])
    argv.extend(["--format", args.format])
    return run_passthrough("vault_cli", argv, timeout=args.timeout or 90)


def cmd_wx(args) -> int:
    """统一 wx-cli 入口：wd wx <op> [args...]；表驱动 WX_SURFACE。"""
    from wx_bridge import inventory, run_wx

    if args.wx_op in ("info", "probe", None):
        _print(inventory())
        return 0
    rest = list(args.wx_args or [])
    if rest and rest[0] == "--":
        rest = rest[1:]
    # text ops stream
    if args.wx_op in ("init", "daemon-status", "daemon-stop", "daemon-logs") or args.raw:
        from wx_bridge import wx_bin
        import subprocess

        b = wx_bin()
        if not b:
            _print({"error": "wx_not_found", "action": "install wx-cli or set WECHAT_WX_BIN"})
            return 2
        if args.wx_op == "daemon-status":
            cmd = [b, "daemon", "status", *rest]
        elif args.wx_op == "daemon-stop":
            cmd = [b, "daemon", "stop", *rest]
        elif args.wx_op == "daemon-logs":
            cmd = [b, "daemon", "logs", *rest]
        elif args.wx_op == "init":
            cmd = [b, "init", *rest]
        else:
            cmd = [b, args.wx_op, *rest]
        return subprocess.run(cmd).returncode
    result = run_wx(args.wx_op, rest, timeout=args.timeout or 90)
    _print(result)
    return 0 if result.get("ok") else 1


def _cmd_wx_op(op_name: str):
    def _inner(args) -> int:
        from wx_bridge import run_wx

        rest = list(getattr(args, "rest", None) or [])
        if rest and rest[0] == "--":
            rest = rest[1:]
        # convenience flags
        if getattr(args, "limit", None) is not None and "-n" not in rest:
            rest = ["-n", str(args.limit), *rest]
        if getattr(args, "keyword", None):
            rest = [args.keyword, *rest]
        if getattr(args, "chat", None) and op_name in ("history", "members", "stats", "attachments", "export"):
            rest = [args.chat, *rest]
        if getattr(args, "account", None):
            rest.extend(["--account", args.account])
        if getattr(args, "user", None):
            rest.extend(["--user", args.user])
        if getattr(args, "since", None):
            rest.extend(["--since", args.since])
        if getattr(args, "until", None):
            rest.extend(["--until", args.until])
        result = run_wx(op_name, rest, timeout=getattr(args, "timeout", None) or 90)
        _print(result)
        return 0 if result.get("ok") else 1

    return _inner


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wd", description="wechat-digger unified CLI")
    p.add_argument("--source", default=None, help="force source: vault|wxcli|export|fixture")
    p.add_argument("--allow-fixture", action="store_true", help="allow fixture fallback")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("detect", help="detect data sources")
    d.set_defaults(func=cmd_detect)

    doc = sub.add_parser("doctor", help="health check")
    doc.set_defaults(func=cmd_doctor)

    s = sub.add_parser("sessions")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_sessions)

    c = sub.add_parser("contacts")
    c.add_argument("--query", default=None)
    c.set_defaults(func=cmd_contacts)

    h = sub.add_parser("history")
    h.add_argument("--chat", required=True)
    h.add_argument("--since", default=None)
    h.add_argument("--until", default=None)
    h.add_argument("--all-time", action="store_true")
    h.add_argument("--source", default=argparse.SUPPRESS, help="force source（fts=全史文本层，亦可放在子命令前）")
    h.add_argument("--limit", type=int, default=50)
    h.add_argument("--full", action="store_true")
    h.set_defaults(func=cmd_history)

    cov = sub.add_parser("coverage", help="数据覆盖审计（vault/fts 跨度、总量、空洞）")
    cov.set_defaults(func=cmd_coverage)

    se = sub.add_parser("search")
    se.add_argument("keyword")
    se.add_argument("--chat", default=None)
    se.add_argument("--since", default=None)
    se.add_argument("--until", default=None)
    se.add_argument("--limit", type=int, default=50)
    # SUPPRESS：未提供时不覆盖顶层 --source；两个位置均可生效
    se.add_argument("--source", default=argparse.SUPPRESS, help="force source: fts|vault|wxcli…（亦可放在子命令前）")
    se.add_argument("--group-by", choices=["chat"], default=None, help="按会话聚合命中分布（fts 引擎）")
    se.add_argument("--use-index", action="store_true")
    se.set_defaults(func=cmd_search)

    ix = sub.add_parser("index")
    ix.add_argument("--chat", default=None)
    ix.add_argument("--since", default=None)
    ix.add_argument("--until", default=None)
    ix.add_argument("--all-time", action="store_true")
    ix.add_argument("--source", default=argparse.SUPPRESS, help="force source（fts=全史文本层，亦可放在子命令前）")
    ix.add_argument("--input", default=None)
    ix.add_argument("--db", default=None)
    ix.add_argument("--limit", type=int, default=100000, help="单群取数上限（索引用大值取全量）")
    ix.set_defaults(func=cmd_index)

    a = sub.add_parser("analyze")
    a.add_argument("--chat", default=None)
    a.add_argument("--since", default=None)
    a.add_argument("--until", default=None)
    a.add_argument("--all-time", action="store_true")
    a.add_argument("--source", default=argparse.SUPPRESS, help="force source（fts=全史文本层，亦可放在子命令前）")
    a.add_argument(
        "--mode",
        default="full",
        choices=sorted(ANALYSIS_MODES.keys()),
        help="summary|lab|dyad|full|…（表驱动 ANALYSIS_MODES）",
    )
    a.add_argument("--input", default=None)
    a.add_argument("--output", default="-")
    a.add_argument("--limit", type=int, default=100000, help="取数上限（分析需要全量深度）")
    a.add_argument("--save-history", action="store_true")
    a.add_argument("--snapshot", action="store_true", help="保存跨次画像快照并输出与上次的 diff")
    a.add_argument("--self", dest="self_hint", default=None, help="本人昵称/wxid 片段（signals 模式判定待回复/承诺方向）")
    a.set_defaults(func=cmd_analyze)

    dg = sub.add_parser("digest")
    dg.add_argument("--chat", required=True)
    dg.add_argument("--since", default=None)
    dg.add_argument("--until", default=None)
    dg.add_argument("--all-time", action="store_true")
    dg.add_argument("--source", default=argparse.SUPPRESS, help="force source（fts=全史文本层，亦可放在子命令前）")
    dg.add_argument("--version", choices=["normal", "roast"], default="normal")
    dg.add_argument("--input", default=None)
    dg.add_argument("--output-dir", default=None)
    dg.add_argument("--print-body", action="store_true")
    dg.set_defaults(func=cmd_digest)

    rp = sub.add_parser("report", help="实验室/关系报告（吸收 welink+垂直 skill 精华）")
    rp.add_argument("--chat", default=None)
    rp.add_argument("--since", default=None)
    rp.add_argument("--until", default=None)
    rp.add_argument("--all-time", action="store_true")
    rp.add_argument("--source", default=argparse.SUPPRESS, help="force source（fts=全史文本层，亦可放在子命令前）")
    rp.add_argument("--flavor", choices=["lab", "dyad"], default="lab")
    rp.add_argument("--mode", default=None, choices=sorted(ANALYSIS_MODES.keys()))
    rp.add_argument("--input", default=None)
    rp.add_argument("--output-dir", default=None)
    rp.add_argument("--print-body", action="store_true")
    rp.set_defaults(func=cmd_report)

    fu = sub.add_parser("followups", help="商机/承诺跟进（状态机+反馈学习+今日行动；fts 文本层）")
    fu.add_argument("--chat", default=None, help="仅扫描指定会话（默认全库窗口）")
    fu.add_argument("--window", type=int, default=30, help="扫描窗口天数")
    fu.add_argument("--self", dest="self_hint", default=None, help="本人昵称/wxid 片段（判定承诺方向/待回复/复联）")
    fu.add_argument("--limit", type=int, default=20)
    fu.add_argument("--list", dest="list_only", action="store_true", help="只列状态机条目，不扫描")
    fu.add_argument("--status", default="open", choices=["open", "all"])
    fu.add_argument("--triage", type=int, default=None, metavar="ID", help="对条目 ID 做分流")
    fu.add_argument("--decision", default=None, choices=["pursue", "wait", "pause", "ignore", "won", "lost"])
    fu.add_argument("--feedback", default=None, help="反馈对象：item:<id> 或 chat:<chat_id>（学习后抑制同类信号）")
    fu.add_argument("--verdict", default=None, choices=["confirmed", "false_positive", "ignore", "low_priority"])
    fu.add_argument("--note", default=None)
    fu.add_argument("--include-noise", action="store_true", help="不排除自动识别的噪音群（用户已确认的 ignore 仍生效）")
    fu.add_argument("--relevance", default=None, metavar="PATH",
                    help="用户相关性策略 JSON（默认 ~/.config/wechat-digger/relevance.json；缺失则不启用）")
    fu.add_argument("--purge-excluded", action="store_true",
                    help="清理历史残留：移除已判定 noise/low 会话的既有条目（默认 dry-run）")
    fu.add_argument("--yes", action="store_true",
                    help="配合 --purge-excluded 真正执行删除（会先写 .bak 备份）")
    fu.set_defaults(func=cmd_followups)

    ch = sub.add_parser("chats", help="会话质量分层排查（识别线报/广告类噪音群，附证据）")
    ch.add_argument("--window", type=int, default=30, help="排查窗口天数")
    ch.add_argument("--level", default="all", choices=["all", "noise", "low", "normal"])
    ch.add_argument("--limit", type=int, default=100)
    ch.set_defaults(func=cmd_chats)

    rp2 = sub.add_parser("roundup", help="跨会话信息整合成文（--match 同类群 / --keyword 主题 / --mine 我的发言）")
    rp2.add_argument("--match", default=None, help="按群名/联系人名匹配同类会话（点名模式，不排噪音）")
    rp2.add_argument("--keyword", default=None, help="按内容关键词跨群提取（默认排除噪音群）")
    rp2.add_argument("--mine", action="store_true", help="梳理本人发言（带对话上下文，默认窗口 90 天）")
    rp2.add_argument("--window", type=int, default=None, help="窗口天数（默认 match/keyword 7 天，mine 90 天）")
    rp2.add_argument("--self", dest="self_hint", default=None, help="本人昵称/wxid 片段（--mine 必填）")
    rp2.add_argument("--include-noise", action="store_true", help="不排除噪音群")
    rp2.add_argument("--output-dir", default=None)
    rp2.add_argument("--print-body", action="store_true")
    rp2.set_defaults(func=cmd_roundup)

    ex = sub.add_parser("export-msg")
    ex.add_argument("--chat", required=True)
    ex.add_argument("--since", default=None)
    ex.add_argument("--until", default=None)
    ex.add_argument("--all-time", action="store_true")
    ex.add_argument("--source", default=argparse.SUPPRESS, help="force source（fts=全史文本层，亦可放在子命令前）")
    ex.add_argument("--output", required=True)
    ex.set_defaults(func=cmd_export_msg)

    # ── acquire / decrypt (self-contained) ──
    ai = sub.add_parser("acquire-info", help="show embedded acquire stack inventory")
    ai.set_defaults(func=cmd_acquire_info)

    sd = sub.add_parser("setup-deps", help="create .venv + install pycryptodome/zstandard")
    sd.add_argument("--timeout", type=int, default=None)
    sd.set_defaults(func=cmd_setup_deps)

    k = sub.add_parser("keys", help="list-dbs / match / capture encryption keys")
    k.add_argument("action", choices=["list-dbs", "match", "capture"])
    k.add_argument("--duration", type=int, default=None)
    k.add_argument("--timeout", type=int, default=None)
    k.set_defaults(func=cmd_keys)

    dec = sub.add_parser("decrypt", help="decrypt WeChat DBs into private vault")
    dec.add_argument("--mode", choices=["incremental", "full"], default="incremental")
    dec.add_argument("--timeout", type=int, default=None)
    dec.set_defaults(func=cmd_decrypt)

    rf = sub.add_parser("refresh", help="keys-match + decrypt incremental (daily path)")
    rf.add_argument("--timeout", type=int, default=None)
    rf.set_defaults(func=cmd_refresh)

    v = sub.add_parser("vault", help="passthrough to embedded vault_cli")
    v.add_argument(
        "vault_cmd",
        choices=[
            "status", "sessions", "unread", "new-messages", "contacts", "members",
            "history", "search", "stats", "export", "digest-source", "favorites", "moments",
        ],
    )
    v.add_argument("vault_args", nargs=argparse.REMAINDER)
    v.add_argument("--json", action="store_true")
    v.add_argument("--timeout", type=int, default=None)
    v.set_defaults(func=cmd_vault)

    mo = sub.add_parser("moments", help="query moments (朋友圈；vault 优先，可 --source wxcli)")
    mo.add_argument("--name", default=None)
    mo.add_argument("--start", default=None)
    mo.add_argument("--limit", type=int, default=None)
    mo.add_argument("--format", default="text", choices=["text", "json"])
    mo.add_argument("--timeout", type=int, default=None)
    mo.set_defaults(func=cmd_moments)

    fv = sub.add_parser("favorites", help="query favorites (收藏夹)")
    fv.add_argument("--type", default=None)
    fv.add_argument("--query", default=None)
    fv.add_argument("--limit", type=int, default=None)
    fv.add_argument("--format", default="text", choices=["text", "json"])
    fv.add_argument("--timeout", type=int, default=None)
    fv.set_defaults(func=cmd_favorites)

    # ── wx-cli first-class (table-driven via wx_bridge) ──
    wxp = sub.add_parser("wx", help="wx-cli bridge: info|sessions|sns-feed|biz-articles|…")
    wxp.add_argument(
        "wx_op",
        nargs="?",
        default="info",
        help="op in WX_SURFACE or info/probe",
    )
    wxp.add_argument("wx_args", nargs=argparse.REMAINDER)
    wxp.add_argument("--timeout", type=int, default=None)
    wxp.add_argument("--raw", action="store_true", help="stream raw subprocess IO")
    wxp.set_defaults(func=cmd_wx)

    for op_name, help_text in (
        ("sns-feed", "朋友圈时间线 (wx)"),
        ("sns-search", "朋友圈搜索 (wx)"),
        ("sns-notifications", "朋友圈互动通知 (wx)"),
        ("biz-articles", "公众号推送本地缓存 (wx)"),
        ("attachments", "会话图片附件列表 (wx)"),
        ("unread", "未读会话 (wx)"),
        ("new-messages", "增量新消息 (wx)"),
    ):
        p_op = sub.add_parser(op_name, help=help_text)
        p_op.add_argument("rest", nargs=argparse.REMAINDER)
        p_op.add_argument("--limit", type=int, default=None)
        p_op.add_argument("--timeout", type=int, default=None)
        p_op.add_argument("--user", default=None)
        p_op.add_argument("--account", default=None)
        p_op.add_argument("--since", default=None)
        p_op.add_argument("--until", default=None)
        p_op.add_argument("--chat", default=None)
        p_op.add_argument("--keyword", default=None)
        p_op.set_defaults(func=_cmd_wx_op(op_name))

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # propagate top-level flags if missing on sub
    if not hasattr(args, "source"):
        args.source = None
    if not hasattr(args, "allow_fixture"):
        args.allow_fixture = False
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

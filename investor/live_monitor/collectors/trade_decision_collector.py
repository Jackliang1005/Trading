#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, Optional

from live_monitor.config import QMTTRADER_LOG_ROOT


LIVE_LOG_DIR = QMTTRADER_LOG_ROOT / "live"

FINAL_SELECTION_RE = re.compile(r"最终选股完成,\s*信号:\s*(\d+),\s*标的:\s*(\[[^\]]*\])")
POOL_UPDATE_RE = re.compile(r"实时监控池更新:\s*(\d+)\s*只\s*\(持仓(\d+)\+候选(\d+)\)")
BUY_SUBMIT_RE = re.compile(r"买入委托已提交\s*-\s*代码:\s*([0-9]{6}\.[A-Z]{2}),\s*数量:\s*(\d+)")
BUY_FILLED_RE = re.compile(r"检测到当日买入成交:\s*([0-9]{6}\.[A-Z]{2})")
BUY_SKIP_RE = re.compile(r"买入过滤跳过:\s*([0-9]{6}\.[A-Z]{2}),\s*reason=([a-zA-Z0-9_]+)")
PRIORITY_BUY_RE = re.compile(r"符合条件的 .* 股票:\s*(\[[^\]]*\])")
WATCHLIST_FILE_RE = re.compile(r"(已保存选股文件|从文件加载选股结果成功|选股文件不存在):\s*(.+watchlists[\\/][^\s]+\.json)")
WATCHLIST_DIR_MISSING_RE = re.compile(r"未找到watchlists目录:\s*(.+)$")
GENERIC_CODES_RE = re.compile(r"([0-9]{6}\.[A-Z]{2})")
LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _safe_literal_list(raw: str) -> List[str]:
    try:
        value = ast.literal_eval(raw)
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_trade_date(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if re.fullmatch(r"\d{8}", value):
        return value
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value.replace("-", "")
    return ""


def _parse_watchlist_meta(path_text: str) -> Dict[str, str]:
    normalized = str(path_text or "").strip().replace("\\", "/")
    file_name = normalized.rsplit("/", 1)[-1] if normalized else ""
    strategy = ""
    date_token = ""
    if file_name.endswith(".json"):
        stem = file_name[:-5]
        if "_" in stem:
            maybe_strategy, maybe_date = stem.rsplit("_", 1)
            if re.fullmatch(r"\d{8}", maybe_date):
                strategy = maybe_strategy
                date_token = maybe_date
            else:
                strategy = stem
        else:
            strategy = stem
    return {
        "path": str(path_text or "").strip(),
        "file": file_name,
        "strategy": strategy,
        "watchlist_date": date_token,
    }


def _extract_line_ts(line: str) -> str:
    matched = LINE_TS_RE.search(str(line or ""))
    return matched.group(1) if matched else ""


def _parse_system_log(path: Path) -> Dict:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    final_candidates: List[str] = []
    priority_buy_candidates: List[str] = []
    submitted_buys: List[Dict] = []
    filled_buys: List[str] = []
    skipped_buys: List[Dict] = []
    latest_pool: Dict[str, int] = {}
    phase1_candidates: List[str] = []
    watchlists: List[Dict] = []
    watchlist_dir_warnings: List[str] = []
    timeline_events: List[Dict] = []
    signal_count = 0
    strategy_name = ""
    seen_watchlists = set()

    for line in lines:
        if "[" in line and "]" in line and not strategy_name:
            try:
                strategy_name = line.split("[", 1)[1].split("]", 1)[0].strip()
            except Exception:
                pass

        match = FINAL_SELECTION_RE.search(line)
        if match:
            signal_count = int(match.group(1))
            final_candidates = _safe_literal_list(match.group(2))
            timeline_events.append(
                {
                    "ts": _extract_line_ts(line),
                    "event": "final_selection",
                    "signal_count": signal_count,
                    "codes": final_candidates[:10],
                }
            )

        match = POOL_UPDATE_RE.search(line)
        if match:
            latest_pool = {
                "universe_count": int(match.group(1)),
                "position_count": int(match.group(2)),
                "candidate_count": int(match.group(3)),
            }

        match = PRIORITY_BUY_RE.search(line)
        if match:
            priority_buy_candidates = _safe_literal_list(match.group(1))
            timeline_events.append(
                {
                    "ts": _extract_line_ts(line),
                    "event": "priority_candidates",
                    "codes": priority_buy_candidates[:10],
                }
            )

        match = BUY_SUBMIT_RE.search(line)
        if match:
            submitted_buys.append({"code": match.group(1), "volume": int(match.group(2))})
            timeline_events.append(
                {
                    "ts": _extract_line_ts(line),
                    "event": "buy_submitted",
                    "code": match.group(1),
                    "volume": int(match.group(2)),
                }
            )

        match = BUY_FILLED_RE.search(line)
        if match:
            filled_buys.append(match.group(1))
            timeline_events.append(
                {
                    "ts": _extract_line_ts(line),
                    "event": "buy_filled",
                    "code": match.group(1),
                }
            )

        match = BUY_SKIP_RE.search(line)
        if match:
            skipped_buys.append({"code": match.group(1), "reason": match.group(2)})
            timeline_events.append(
                {
                    "ts": _extract_line_ts(line),
                    "event": "buy_skipped",
                    "code": match.group(1),
                    "reason": match.group(2),
                }
            )

        if "候选股:" in line:
            phase1_candidates.extend(GENERIC_CODES_RE.findall(line))

        match = WATCHLIST_FILE_RE.search(line)
        if match:
            status_text = match.group(1)
            raw_path = match.group(2).strip()
            status = {
                "已保存选股文件": "saved",
                "从文件加载选股结果成功": "loaded",
                "选股文件不存在": "missing",
            }.get(status_text, "unknown")
            item = _parse_watchlist_meta(raw_path)
            item["status"] = status
            key = (item.get("path", ""), status)
            if key not in seen_watchlists:
                seen_watchlists.add(key)
                watchlists.append(item)

        match = WATCHLIST_DIR_MISSING_RE.search(line)
        if match:
            missing_dir = match.group(1).strip()
            if missing_dir and missing_dir not in watchlist_dir_warnings:
                watchlist_dir_warnings.append(missing_dir)

    unique_phase1 = []
    for code in phase1_candidates:
        if code not in unique_phase1:
            unique_phase1.append(code)
    unique_filled = []
    for code in filled_buys:
        if code not in unique_filled:
            unique_filled.append(code)
    unique_skipped: List[Dict] = []
    seen_skipped = set()
    for item in skipped_buys:
        key = (item["code"], item["reason"])
        if key in seen_skipped:
            continue
        seen_skipped.add(key)
        unique_skipped.append(item)

    return {
        "path": str(path),
        "name": path.name,
        "log_date": path.parent.name,
        "strategy": strategy_name,
        "signal_count": signal_count,
        "final_candidates": final_candidates,
        "priority_buy_candidates": priority_buy_candidates,
        "submitted_buys": submitted_buys,
        "filled_buys": unique_filled,
        "skipped_buys": unique_skipped[:50],
        "phase1_candidates": unique_phase1[:50],
        "latest_pool": latest_pool,
        "watchlists": watchlists[:80],
        "watchlist_dir_warnings": watchlist_dir_warnings[:20],
        "timeline_events": timeline_events[:200],
    }


def collect_trade_decisions(date: str | None = None) -> Dict:
    latest_system: Optional[Path] = None
    normalized_date = _normalize_trade_date(date or "")
    if LIVE_LOG_DIR.exists():
        if normalized_date:
            candidate = LIVE_LOG_DIR / normalized_date / "system.log"
            if candidate.exists():
                latest_system = candidate
        elif latest_system is None:
            candidates = sorted(LIVE_LOG_DIR.glob("*/system.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            if candidates:
                latest_system = candidates[0]

    system_summary = _parse_system_log(latest_system) if latest_system else {}
    log_sources = {"entries": [system_summary] if system_summary else []}
    watchlist_entries = system_summary.get("watchlists", []) if system_summary else []
    watchlist_saved_count = len([item for item in watchlist_entries if item.get("status") == "saved"])
    return {
        "kind": "trade_decisions",
        "requested_date": normalized_date,
        "system_log": system_summary,
        "sources": log_sources,
        "watchlists": {
            "entries": watchlist_entries,
            "dir_warnings": (system_summary.get("watchlist_dir_warnings", []) if system_summary else []),
        },
        "summary": {
            "requested_date": normalized_date,
            "latest_log_date": system_summary.get("log_date", ""),
            "strategy": system_summary.get("strategy", ""),
            "signal_count": int(system_summary.get("signal_count", 0) or 0),
            "final_candidate_count": len(system_summary.get("final_candidates", []) or []),
            "submitted_buy_count": len(system_summary.get("submitted_buys", []) or []),
            "filled_buy_count": len(system_summary.get("filled_buys", []) or []),
            "source_count": len(log_sources.get("entries", []) or []),
            "watchlist_count": len(watchlist_entries),
            "watchlist_saved_count": watchlist_saved_count,
            "timeline_event_count": len(system_summary.get("timeline_events", []) or []),
            "skipped_buy_count": len(system_summary.get("skipped_buys", []) or []),
        },
    }

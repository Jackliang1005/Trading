
#!/usr/bin/env python3
"""Weekly assistant report for OpenClaw Investor."""

from __future__ import annotations

import contextlib
import io
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

import db
from domain.services.longterm_portfolio_service import (
    build_longterm_snapshot_text,
    load_longterm_snapshot,
    summarize_longterm_snapshot,
)

WORKSPACE = Path("/root/.openclaw/workspace")
REPORTS_DIR = WORKSPACE / "reports"


def _u(value: str) -> str:
    return value


def _init_db_quietly() -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        db.init_db()


def _parse_dt(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if "%H" in fmt else text[:10], fmt)
        except Exception:
            continue
    return None


def _week_range(days: int = 7, end: date | None = None) -> Tuple[date, date]:
    end_date = end or date.today()
    start_date = end_date - timedelta(days=max(1, days) - 1)
    return start_date, end_date


def _load_week_events(start: date, end: date, limit: int = 200) -> List[Dict[str, Any]]:
    _init_db_quietly()
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time())
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, captured_at, data FROM market_snapshots WHERE snapshot_type='event_alert' ORDER BY captured_at DESC LIMIT ?",
        (max(1, limit),),
    ).fetchall()
    conn.close()
    events: List[Dict[str, Any]] = []
    for row in rows:
        captured = _parse_dt(row["captured_at"])
        if captured and not (start_dt <= captured < end_dt):
            continue
        try:
            payload = json.loads(row["data"])
        except Exception:
            continue
        payload["_snapshot_id"] = row["id"]
        payload["_captured_at"] = row["captured_at"]
        events.append(payload)
    return events


def _summarize_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    theme_counts: Counter[str] = Counter()
    severity_counts: Counter[str] = Counter()
    for event in events:
        severity_counts[str(event.get("severity") or "unknown")] += 1
        for theme in event.get("themes", []) or []:
            name = str(theme.get("theme") or "").strip()
            if name:
                theme_counts[name] += 1
    top_events = sorted(events, key=lambda e: int(e.get("score", 0) or 0), reverse=True)[:8]
    return {
        "count": len(events),
        "theme_counts": theme_counts.most_common(8),
        "severity_counts": severity_counts.most_common(),
        "top_events": top_events,
    }


def _summarize_predictions(start: date, end: date) -> Dict[str, Any]:
    _init_db_quietly()
    rows = db.get_checked_predictions_in_range(start.isoformat(), end.isoformat())
    total = len(rows)
    correct = sum(1 for item in rows if item.get("is_correct"))
    by_strategy: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "correct": 0})
    for item in rows:
        strategy = str(item.get("strategy_used") or item.get("model") or "unknown")
        by_strategy[strategy]["total"] += 1
        if item.get("is_correct"):
            by_strategy[strategy]["correct"] += 1
    strategies = []
    for name, stat in by_strategy.items():
        t = stat["total"]
        c = stat["correct"]
        strategies.append({"strategy": name, "total": t, "correct": c, "win_rate": round(c / t * 100, 1) if t else 0.0})
    strategies.sort(key=lambda item: (item["win_rate"], item["total"]), reverse=True)
    return {
        "total": total,
        "correct": correct,
        "win_rate": round(correct / total * 100, 1) if total else 0.0,
        "strategies": strategies[:6],
    }


def _load_audit_summary() -> Dict[str, Any]:
    path = REPORTS_DIR / "investor_assistant_capability_audit_latest.json"
    if not path.exists():
        return {"available": False, "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "path": str(path), "error": str(exc)}
    blocked = [item for item in payload.get("items", []) if item.get("status") == "blocked"]
    warnings = [item for item in payload.get("items", []) if item.get("status") == "warn"]
    return {
        "available": True,
        "overall": payload.get("overall"),
        "blocked_count": payload.get("blocked_count", len(blocked)),
        "warning_count": payload.get("warning_count", len(warnings)),
        "blocked_items": [item.get("name") for item in blocked],
        "warning_items": [item.get("name") for item in warnings],
        "generated_at": payload.get("generated_at", ""),
    }


def _load_longterm_summary() -> Dict[str, Any]:
    summary = summarize_longterm_snapshot(load_longterm_snapshot())
    return {"summary": summary, "text": build_longterm_snapshot_text(summary)}


def build_weekly_report(days: int = 7, end_date: str = "") -> Dict[str, Any]:
    end = date.fromisoformat(end_date) if end_date else date.today()
    start, end = _week_range(days=days, end=end)
    events = _load_week_events(start, end)
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "events": _summarize_events(events),
        "predictions": _summarize_predictions(start, end),
        "longterm": _load_longterm_summary(),
        "audit": _load_audit_summary(),
    }
    report["text"] = format_weekly_report(report)
    return report



def _fmt_pairs(items: List[Tuple[str, int]], empty: str) -> str:
    if not items:
        return empty
    return ", ".join(f"{name}:{count}" for name, count in items)


def format_weekly_report(report: Dict[str, Any]) -> str:
    events = report.get("events", {}) or {}
    predictions = report.get("predictions", {}) or {}
    longterm = report.get("longterm", {}) or {}
    audit = report.get("audit", {}) or {}
    lines = [
        "OpenClaw A-share Investment Assistant Weekly Report",
        f"Period: {report.get('period_start')} ~ {report.get('period_end')}",
        f"Generated: {report.get('generated_at')}",
        "",
        "1. Events and Themes",
        f"Event count: {events.get('count', 0)} | Severity: {_fmt_pairs(events.get('severity_counts', []), 'none')}",
        f"Theme heat: {_fmt_pairs(events.get('theme_counts', []), 'none')}",
    ]
    top_events = events.get("top_events", []) or []
    if top_events:
        lines.append("Top events:")
        for event in top_events[:5]:
            themes = ", ".join(str(t.get("theme")) for t in (event.get("themes") or [])[:3] if t.get("theme")) or "unmatched"
            lines.append(f"- {event.get('severity', 'P3')}/{event.get('score', 0)} {event.get('title', '')} | {themes}")
    else:
        lines.append("Top events: none")

    lines.extend([
        "",
        "2. Prediction Performance",
        f"Checked: {predictions.get('total', 0)} | Correct: {predictions.get('correct', 0)} | Win rate: {predictions.get('win_rate', 0)}%",
    ])
    strategies = predictions.get("strategies", []) or []
    if strategies:
        lines.append("Strategy performance: " + " | ".join(f"{s['strategy']} {s['correct']}/{s['total']}({s['win_rate']}%)" for s in strategies[:4]))
    else:
        lines.append("Strategy performance: no checked predictions this week")

    lines.extend([
        "",
        "3. Long-term Portfolio",
        str(longterm.get("text") or "No long-term portfolio snapshot"),
        "",
        "4. Runtime Health and Blockers",
    ])
    if audit.get("available"):
        lines.append(f"Capability audit: {audit.get('overall')} | blocked={audit.get('blocked_count')} warn={audit.get('warning_count')}")
        blocked = audit.get("blocked_items") or []
        if blocked:
            lines.append("Blocked items: " + ", ".join(str(item) for item in blocked))
    else:
        lines.append("Capability audit: no latest report")

    lines.extend([
        "",
        "5. Next-week Focus",
        "- Restore Guojin qmt2http/miniQMT reads to close the dual-account monitoring loop.",
        "- Check /global and /events daily for overseas AI, macro-rate, energy, and geopolitical transmission into A-share themes.",
        "- Watch long-term portfolio cash ratio, concentration, and rejected plan reasons.",
    ])
    return "\n".join(lines)

def save_weekly_report(report: Dict[str, Any]) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"investor_weekly_report_{stamp}.md"
    latest = REPORTS_DIR / "investor_weekly_report_latest.md"
    path.write_text(str(report.get("text", "")), encoding="utf-8")
    latest.write_text(str(report.get("text", "")), encoding="utf-8")
    json_path = REPORTS_DIR / "investor_weekly_report_latest.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(path)

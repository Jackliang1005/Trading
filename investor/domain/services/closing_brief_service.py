
#!/usr/bin/env python3
"""Post-market closing brief for OpenClaw Investor."""

from __future__ import annotations

import contextlib
import io
import json
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

import db
from domain.services.risk_report_service import build_risk_report
from domain.services.global_impact_service import build_global_impact_brief
from domain.services.longterm_portfolio_service import build_longterm_snapshot_text, load_longterm_snapshot, summarize_longterm_snapshot

WORKSPACE = Path("/root/.openclaw/workspace")
REPORTS_DIR = WORKSPACE / "reports"


def _init_db_quietly() -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        db.init_db()


def _parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except Exception:
        return None


def _load_events_for_day(target: date, limit: int = 200) -> List[Dict[str, Any]]:
    _init_db_quietly()
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, captured_at, data FROM market_snapshots WHERE snapshot_type='event_alert' ORDER BY captured_at DESC LIMIT ?",
        (max(1, limit),),
    ).fetchall()
    conn.close()
    events: List[Dict[str, Any]] = []
    for row in rows:
        d = _parse_date(row["captured_at"])
        if d != target:
            continue
        try:
            payload = json.loads(row["data"])
        except Exception:
            continue
        payload["_captured_at"] = row["captured_at"]
        payload["_snapshot_id"] = row["id"]
        events.append(payload)
    return events


def _summarize_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    themes: Counter[str] = Counter()
    severity: Counter[str] = Counter()
    for event in events:
        severity[str(event.get("severity") or "unknown")] += 1
        for item in event.get("themes", []) or []:
            name = str(item.get("theme") or "").strip()
            if name:
                themes[name] += 1
    top = sorted(events, key=lambda e: int(e.get("score", 0) or 0), reverse=True)[:6]
    return {"count": len(events), "themes": themes.most_common(8), "severity": severity.most_common(), "top_events": top}


def _load_audit() -> Dict[str, Any]:
    path = REPORTS_DIR / "investor_assistant_capability_audit_latest.json"
    if not path.exists():
        return {"available": False, "blocked": [], "warning": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "error": str(exc), "blocked": [], "warning": []}
    return {
        "available": True,
        "overall": payload.get("overall"),
        "blocked": [item.get("name") for item in payload.get("items", []) if item.get("status") == "blocked"],
        "warning": [item.get("name") for item in payload.get("items", []) if item.get("status") == "warn"],
        "generated_at": payload.get("generated_at", ""),
    }


def _fmt_pairs(items: List[Any], empty: str = "none") -> str:
    if not items:
        return empty
    return ", ".join(f"{name}:{count}" for name, count in items)


def _closing_impact_lines(impact: Dict[str, Any], limit: int = 3) -> List[str]:
    lines: List[str] = []
    for event in (impact.get("urgent_events") or [])[:limit]:
        guidance = event.get("guidance") or {}
        stocks = ", ".join(str(s.get("code")) for s in (event.get("related_stocks") or [])[:5] if s.get("code")) or "none"
        lines.append(f"- {event.get('severity', 'P3')}/{event.get('score', 0)} priority={event.get('priority', 0)} {event.get('title', '')} | A-share={stocks}")
        lines.append(f"  next_watch={guidance.get('watch', '')}")
    return lines or ["- no urgent global impact item"]


def _closing_watchlist_lines(impact: Dict[str, Any], limit: int = 5) -> List[str]:
    lines: List[str] = []
    for item in (impact.get("watchlist") or [])[:limit]:
        holding = " holding" if item.get("holding") else ""
        lines.append(f"- {item.get('code')} {item.get('name')} priority={item.get('priority')} events={item.get('event_count')}{holding}")
    return lines or ["- no event-driven watchlist item"]


def build_closing_brief(target_date: str = "") -> Dict[str, Any]:
    target = date.fromisoformat(target_date) if target_date else date.today()
    events = _summarize_events(_load_events_for_day(target))
    global_impact = build_global_impact_brief(limit=80, min_score=45, top_n=5, use_cache=True, max_cache_minutes=120)
    risk = build_risk_report()
    longterm_summary = summarize_longterm_snapshot(load_longterm_snapshot())
    audit = _load_audit()
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": target.isoformat(),
        "events": events,
        "global_impact": global_impact,
        "risk": risk,
        "longterm": {"summary": longterm_summary, "text": build_longterm_snapshot_text(longterm_summary)},
        "audit": audit,
    }
    payload["text"] = format_closing_brief(payload)
    return payload


def format_closing_brief(payload: Dict[str, Any]) -> str:
    events = payload.get("events") or {}
    risk = payload.get("risk") or {}
    longterm = payload.get("longterm") or {}
    audit = payload.get("audit") or {}
    lines = [
        "OpenClaw Post-market Closing Brief",
        f"date: {payload.get('date')} | generated_at: {payload.get('generated_at')}",
        "",
        "1. Event review",
        f"event_count={events.get('count', 0)} severity={_fmt_pairs(events.get('severity') or [])}",
        f"theme_heat={_fmt_pairs(events.get('themes') or [])}",
    ]
    top = events.get("top_events") or []
    if top:
        lines.append("top_events:")
        for event in top[:5]:
            themes = ", ".join(str(t.get("theme")) for t in (event.get("themes") or [])[:3] if t.get("theme")) or "unmatched"
            lines.append(f"- {event.get('severity','P3')}/{event.get('score',0)} {event.get('title','')} | {themes}")
    else:
        lines.append("top_events: none")
    lines.extend(["", "2. Next-session global impact", *_closing_impact_lines(payload.get("global_impact") or {}, limit=3), "event_watchlist:", *_closing_watchlist_lines(payload.get("global_impact") or {}, limit=5), "", "3. Portfolio risk close"])
    if risk.get("available"):
        lines.extend([
            f"as_of={risk.get('as_of')} age={risk.get('snapshot_age_days')}d positions={risk.get('positions_count')} mv={risk.get('total_market_value'):.2f} pnl={risk.get('total_unrealized_pnl'):.2f}",
            f"cash={risk.get('cash'):.2f}({risk.get('cash_ratio',0)*100:.1f}%) top1={risk.get('top1_ratio',0)*100:.1f}% top3={risk.get('top3_ratio',0)*100:.1f}%",
            "risk_flags=" + ", ".join(risk.get("risk_flags") or []),
        ])
    else:
        lines.append("risk report unavailable")
    lines.extend(["", "4. Long-term portfolio", str(longterm.get("text") or "longterm snapshot unavailable"), "", "5. Runtime blockers"])
    if audit.get("available"):
        lines.append(f"audit={audit.get('overall')} blocked={len(audit.get('blocked') or [])} warn={len(audit.get('warning') or [])}")
        if audit.get("blocked"):
            lines.append("blocked_items=" + ", ".join(str(item) for item in audit.get("blocked") or []))
    else:
        lines.append("audit unavailable")
    lines.extend([
        "",
        "6. Next session checklist",
        "- Review /影响 for ranked global news, portfolio hits and watch actions before next open.",
        "- If risk_flags include concentration or stale snapshot, check /risk before next open.",
        "- Keep Guojin realtime data marked degraded until qmt2http/miniQMT recovers.",
    ])
    return "\n".join(lines)


def save_closing_brief(payload: Dict[str, Any]) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"investor_closing_brief_{stamp}.md"
    latest = REPORTS_DIR / "investor_closing_brief_latest.md"
    text = str(payload.get("text") or format_closing_brief(payload))
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    (REPORTS_DIR / "investor_closing_brief_latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(path)

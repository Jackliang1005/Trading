
#!/usr/bin/env python3
"""Pre-market morning brief for OpenClaw Investor."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from domain.services.global_impact_service import build_global_impact_brief
from domain.services.risk_report_service import build_risk_report
from domain.services.longterm_portfolio_service import build_longterm_snapshot_text, load_longterm_snapshot, summarize_longterm_snapshot

WORKSPACE = Path("/root/.openclaw/workspace")
REPORTS_DIR = WORKSPACE / "reports"


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


def _global_focus_lines(brief: Dict[str, Any], limit: int = 3) -> List[str]:
    lines: List[str] = []
    for event in (brief.get("top_events") or [])[:limit]:
        themes = ", ".join(str(t.get("theme")) for t in (event.get("themes") or [])[:3] if t.get("theme")) or "unmatched"
        stocks = ", ".join(str(s.get("code")) for s in (event.get("related_stocks") or [])[:4] if s.get("code")) or "none"
        lines.append(f"- {event.get('severity', 'P3')}/{event.get('score', 0)} {event.get('title', '')} | themes={themes} | A-share={stocks}")
    return lines or ["- no global event above threshold"]


def _impact_focus_lines(impact: Dict[str, Any], limit: int = 3) -> List[str]:
    lines: List[str] = []
    for event in (impact.get("urgent_events") or [])[:limit]:
        guidance = event.get("guidance") or {}
        stocks = ", ".join(str(s.get("code")) for s in (event.get("related_stocks") or [])[:5] if s.get("code")) or "none"
        lines.append(f"- {event.get('severity', 'P3')}/{event.get('score', 0)} priority={event.get('priority', 0)} {event.get('title', '')}")
        lines.append(f"  A-share={stocks}")
        lines.append(f"  watch={guidance.get('watch', '')}")
    return lines or ["- no urgent global impact item"]


def _watchlist_focus_lines(impact: Dict[str, Any], limit: int = 5) -> List[str]:
    lines: List[str] = []
    for item in (impact.get("watchlist") or [])[:limit]:
        holding = " holding" if item.get("holding") else ""
        lines.append(f"- {item.get('code')} {item.get('name')} priority={item.get('priority')} events={item.get('event_count')}{holding}")
    return lines or ["- no event-driven watchlist item"]


def build_morning_brief() -> Dict[str, Any]:
    global_impact = build_global_impact_brief(limit=60, min_score=45, top_n=5, use_cache=True, max_cache_minutes=120)
    global_brief = {
        "source_count": global_impact.get("source_count"),
        "raw_count": global_impact.get("raw_count"),
        "candidate_count": global_impact.get("candidate_count"),
        "top_events": global_impact.get("urgent_events", []),
    }
    risk = build_risk_report()
    longterm_summary = summarize_longterm_snapshot(load_longterm_snapshot())
    audit = _load_audit()
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "global": global_brief,
        "global_impact": global_impact,
        "risk": risk,
        "longterm": {"summary": longterm_summary, "text": build_longterm_snapshot_text(longterm_summary)},
        "audit": audit,
    }
    payload["text"] = format_morning_brief(payload)
    return payload


def format_morning_brief(payload: Dict[str, Any]) -> str:
    risk = payload.get("risk") or {}
    longterm = payload.get("longterm") or {}
    audit = payload.get("audit") or {}
    lines = [
        "OpenClaw Pre-market Brief",
        f"generated_at: {payload.get('generated_at')}",
        "",
        "1. Global overnight focus",
        f"global_sources={payload.get('global', {}).get('source_count')} raw={payload.get('global', {}).get('raw_count')} candidates={payload.get('global', {}).get('candidate_count')}",
        *_global_focus_lines(payload.get("global") or {}, limit=3),
        "",
        "2. Global impact command center",
        *_impact_focus_lines(payload.get("global_impact") or {}, limit=3),
        "event_watchlist:",
        *_watchlist_focus_lines(payload.get("global_impact") or {}, limit=5),
        "",
        "3. Portfolio risk",
    ]
    if risk.get("available"):
        lines.extend([
            f"as_of={risk.get('as_of')} age={risk.get('snapshot_age_days')}d positions={risk.get('positions_count')} mv={risk.get('total_market_value'):.2f} pnl={risk.get('total_unrealized_pnl'):.2f}",
            f"cash={risk.get('cash'):.2f}({risk.get('cash_ratio', 0)*100:.1f}%) top1={risk.get('top1_ratio', 0)*100:.1f}% top3={risk.get('top3_ratio', 0)*100:.1f}%",
            "risk_flags=" + ", ".join(risk.get("risk_flags") or []),
        ])
    else:
        lines.append("risk report unavailable")
    lines.extend([
        "",
        "4. Long-term portfolio",
        str(longterm.get("text") or "longterm snapshot unavailable"),
        "",
        "5. Runtime blockers",
    ])
    if audit.get("available"):
        lines.append(f"audit={audit.get('overall')} blocked={len(audit.get('blocked') or [])} warn={len(audit.get('warning') or [])}")
        if audit.get("blocked"):
            lines.append("blocked_items=" + ", ".join(str(item) for item in audit.get("blocked") or []))
    else:
        lines.append("audit unavailable")
    lines.extend([
        "",
        "6. Today checklist",
        "- Check /影响 for ranked global news, portfolio hits and watch actions before open.",
        "- Check /risk if concentration or stale snapshot flags appear.",
        "- Treat Guojin realtime account data as degraded until qmt2http/miniQMT recovers.",
    ])
    return "\n".join(lines)


def save_morning_brief(payload: Dict[str, Any]) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"investor_morning_brief_{stamp}.md"
    latest = REPORTS_DIR / "investor_morning_brief_latest.md"
    text = str(payload.get("text") or format_morning_brief(payload))
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    (REPORTS_DIR / "investor_morning_brief_latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(path)

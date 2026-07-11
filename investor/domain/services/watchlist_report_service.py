
#!/usr/bin/env python3
"""Event-driven A-share watchlist report."""

from __future__ import annotations

from collections import defaultdict
import contextlib
import io
from datetime import datetime
from typing import Any, Dict, List

from domain.services.event_service import list_recent_events
from domain.services.risk_report_service import build_risk_report


def _holding_codes() -> set[str]:
    report = build_risk_report()
    codes = set()
    for item in report.get("top_positions", []) or []:
        code = str(item.get("code") or "").upper()
        if code:
            codes.add(code)
            codes.add(code.split(".", 1)[0])
    return codes


def build_watchlist_report(limit_events: int = 80, top_n: int = 12) -> Dict[str, Any]:
    with contextlib.redirect_stdout(io.StringIO()):
        events = list_recent_events(limit=limit_events).get("events", []) or []
    holdings = _holding_codes()
    rows: Dict[str, Dict[str, Any]] = {}
    theme_counts: Dict[str, int] = defaultdict(int)
    for event in events:
        score = int(event.get("score", 0) or 0)
        severity = str(event.get("severity") or "P3")
        title = str(event.get("title") or "")
        themes = [str(t.get("theme")) for t in event.get("themes", []) or [] if t.get("theme")]
        for theme in themes:
            theme_counts[theme] += 1
        for stock in event.get("related_stocks", []) or []:
            code = str(stock.get("code") or "").strip().upper()
            if not code:
                continue
            item = rows.setdefault(code, {
                "code": code,
                "name": str(stock.get("name") or code),
                "reason": str(stock.get("reason") or ""),
                "event_count": 0,
                "score_sum": 0,
                "max_score": 0,
                "themes": defaultdict(int),
                "sample_events": [],
                "holding": code in holdings or code.split(".", 1)[0] in holdings,
            })
            item["event_count"] += 1
            item["score_sum"] += score
            item["max_score"] = max(int(item["max_score"]), score)
            for theme in themes:
                item["themes"][theme] += 1
            if len(item["sample_events"]) < 3:
                item["sample_events"].append({"severity": severity, "score": score, "title": title[:160]})
    watch = []
    for item in rows.values():
        theme_list = sorted(item["themes"].items(), key=lambda x: x[1], reverse=True)
        item["themes"] = [name for name, _ in theme_list[:5]]
        item["priority"] = int(item["score_sum"]) + int(item["max_score"]) + (30 if item.get("holding") else 0)
        watch.append(item)
    watch.sort(key=lambda item: (item["priority"], item["event_count"], item["max_score"]), reverse=True)
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event_count": len(events),
        "theme_heat": sorted(theme_counts.items(), key=lambda x: x[1], reverse=True)[:8],
        "watchlist": watch[:max(1, top_n)],
    }
    report["text"] = format_watchlist_report(report)
    return report


def format_watchlist_report(report: Dict[str, Any]) -> str:
    lines = [
        "OpenClaw Event-driven Watchlist",
        f"generated_at: {report.get('generated_at')} | events={report.get('event_count')}",
        "theme_heat: " + (", ".join(f"{k}:{v}" for k, v in report.get("theme_heat", [])) or "none"),
        "watchlist:",
    ]
    if not report.get("watchlist"):
        lines.append("- no mapped A-share watch items")
        return "\n".join(lines)
    for idx, item in enumerate(report.get("watchlist") or [], 1):
        holding = " holding" if item.get("holding") else ""
        themes = ",".join(item.get("themes") or []) or "unmatched"
        lines.append(f"{idx}. {item['code']} {item.get('name','')} priority={item.get('priority')} events={item.get('event_count')} max_score={item.get('max_score')}{holding}")
        lines.append(f"   themes={themes} reason={item.get('reason','')}")
        samples = item.get("sample_events") or []
        if samples:
            sample = samples[0]
            lines.append(f"   latest: {sample.get('severity')}/{sample.get('score')} {sample.get('title')}")
    return "\n".join(lines)

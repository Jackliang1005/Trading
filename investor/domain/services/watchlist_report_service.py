
#!/usr/bin/env python3
"""Event-driven A-share watchlist report."""

from __future__ import annotations

from collections import defaultdict
import contextlib
import io
from datetime import datetime, timedelta
from typing import Any, Dict, List

from domain.services.event_service import list_recent_events
from domain.services.event_service import _near_duplicate_title
from domain.services.risk_report_service import build_risk_report
from domain.services.concept_momentum_service import THEME_CONCEPT_KEYWORDS, build_concept_momentum_candidates


def _holding_codes() -> set[str]:
    report = build_risk_report()
    codes = set()
    for item in report.get("top_positions", []) or []:
        code = str(item.get("code") or "").upper()
        if code:
            codes.add(code)
            codes.add(code.split(".", 1)[0])
    return codes


def _event_time(event: Dict[str, Any]) -> datetime | None:
    for key in ("published_at", "_captured_at"):
        try:
            return datetime.fromisoformat(str(event.get(key) or "")[:19])
        except (TypeError, ValueError):
            continue
    return None


def _fresh_unique_events(events: List[Dict[str, Any]], max_age_hours: int = 48, now: datetime | None = None) -> tuple[List[Dict[str, Any]], int, int]:
    current = now or datetime.now()
    cutoff = current - timedelta(hours=max_age_hours)
    seen = set()
    seen_titles: List[str] = []
    fresh: List[Dict[str, Any]] = []
    stale = 0
    duplicates = 0
    for event in events:
        published = _event_time(event)
        if published is None or published < cutoff or published > current + timedelta(hours=6):
            stale += 1
            continue
        identity = str(event.get("event_id") or "").strip() or (str(event.get("title") or "").strip(), str(event.get("url") or "").strip())
        title = str(event.get("title") or "")
        if identity in seen or any(_near_duplicate_title(title, previous) for previous in seen_titles):
            duplicates += 1
            continue
        seen.add(identity)
        seen_titles.append(title)
        fresh.append(event)
    return fresh, stale, duplicates


def build_watchlist_report(limit_events: int = 80, top_n: int = 12, max_age_hours: int = 48) -> Dict[str, Any]:
    with contextlib.redirect_stdout(io.StringIO()):
        raw_events = list_recent_events(limit=max(limit_events * 5, limit_events)).get("events", []) or []
    events, stale_excluded, duplicates_excluded = _fresh_unique_events(raw_events, max_age_hours=max_age_hours)
    events = events[:limit_events]
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
                "direct_event_count": 0,
                "score_sum": 0,
                "max_score": 0,
                "themes": defaultdict(int),
                "sample_events": [],
                "holding": code in holdings or code.split(".", 1)[0] in holdings,
            })
            item["event_count"] += 1
            evidence_text = f"{title} {event.get('summary') or ''}".lower()
            direct = any(token and token.lower() in evidence_text for token in (code, code.split('.', 1)[0], item["name"], item["reason"]))
            if direct:
                item["direct_event_count"] += 1
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
        item["mapping_confidence"] = "direct" if item["direct_event_count"] else "theme_only"
        item["priority"] = int(item["max_score"]) + min(int(item["event_count"]), 5) * 5 + min(int(item["direct_event_count"]), 3) * 20 + (30 if item.get("holding") else 0)
        watch.append(item)
    watch.sort(key=lambda item: (item["priority"], item["event_count"], item["max_score"]), reverse=True)
    # Theme-only static stock maps are too broad to be recommendations.  Keep
    # direct headline/company hits, then add stocks that passed the hot-concept
    # and 20/60-day momentum gate.
    direct_watch = [item for item in watch if item.get("direct_event_count", 0) > 0]
    momentum = build_concept_momentum_candidates(
        sorted(theme_counts.items(), key=lambda x: x[1], reverse=True)[:8],
        top_n=max(1, top_n),
    )
    seen_codes = {str(item.get("code") or "") for item in direct_watch}
    for candidate in momentum.get("candidates") or []:
        code = str(candidate.get("code") or "")
        if not code or code in seen_codes:
            continue
        concepts = [str(value or "") for value in candidate.get("concepts") or []]
        candidate_themes = [
            str(theme)
            for theme in momentum.get("themes") or []
            if any(keyword.lower() in concept.lower() for keyword in THEME_CONCEPT_KEYWORDS.get(str(theme), []) for concept in concepts)
        ]
        direct_watch.append({
            "code": code,
            "name": candidate.get("name") or code,
            "reason": "、".join(candidate.get("concepts") or []),
            "event_count": 0,
            "direct_event_count": 0,
            "mapping_confidence": "concept_momentum",
            "themes": candidate_themes,
            "holding": code in holdings or code.split(".", 1)[0] in holdings,
            "momentum_20d": candidate.get("momentum_20d"),
            "momentum_60d": candidate.get("momentum_60d"),
            "trend": candidate.get("trend"),
            "sample_events": [],
        })
        seen_codes.add(code)
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event_count": len(events),
        "raw_event_count": len(raw_events),
        "stale_excluded": stale_excluded,
        "duplicates_excluded": duplicates_excluded,
        "window_hours": max_age_hours,
        "theme_heat": sorted(theme_counts.items(), key=lambda x: x[1], reverse=True)[:8],
        "watchlist": direct_watch[:max(1, top_n)],
        "concept_momentum": momentum,
    }
    report["text"] = format_watchlist_report(report)
    return report


def format_watchlist_report(report: Dict[str, Any]) -> str:
    from domain.services.report_style_service import event_impact_label, join_cn, theme_label

    lines = [
        "👀 事件驱动观察池",
        f"生成时间：{report.get('generated_at')}",
        "",
        "**主题概览**",
        "- " + ("；".join(f"{theme_label(k)} {v} 条" for k, v in report.get("theme_heat", [])) or "暂无通过质量阈值的主题。"),
        "",
        "**候选标的**",
    ]
    if not report.get("watchlist"):
        lines.append("- 暂无同时满足事件映射与质量门槛的A股候选。")
        return "\n".join(lines)
    for idx, item in enumerate(report.get("watchlist") or [], 1):
        holding = "｜当前持仓" if item.get("holding") else ""
        themes = join_cn((theme_label(value) for value in item.get("themes") or []), "主题待确认")
        confidence = "直接映射" if item.get("direct_event_count", 0) else "概念动量筛选"
        lines.append(f"{idx}. **{item.get('name','')}（{item['code']}）**｜{themes}｜{confidence}{holding}")
        if item.get("reason"):
            prefix = "证据链" if item.get("mapping_confidence") == "concept_momentum" else "逻辑"
            lines.append(f"   {prefix}：{themes} → {item.get('reason')}")
        if item.get("mapping_confidence") == "concept_momentum":
            lines.append(f"   动量：20日 {float(item.get('momentum_20d') or 0):+.2f}%｜60日 {float(item.get('momentum_60d') or 0):+.2f}%")
        samples = item.get("sample_events") or []
        if samples:
            sample = samples[0]
            lines.append(f"   依据：{sample.get('title')}（{event_impact_label(sample.get('severity'))}）")
    lines.extend(["", "**使用原则**", "- 观察池不是买入清单；必须再经过概念板块强弱、个股20/60日动量和开盘量价确认。"])
    return "\n".join(lines)

#!/usr/bin/env python3
"""Global breaking-news impact command center for the A-share assistant."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from domain.services.event_service import build_global_event_brief
from domain.services.risk_report_service import build_risk_report
from domain.services.watchlist_report_service import build_watchlist_report
from domain.services.concept_momentum_service import THEME_CONCEPT_KEYWORDS, build_concept_momentum_candidates


WORKSPACE = Path("/root/.openclaw/workspace")
REPORTS_DIR = WORKSPACE / "reports"
GLOBAL_IMPACT_JSON = REPORTS_DIR / "investor_global_impact_latest.json"
GLOBAL_IMPACT_MD = REPORTS_DIR / "investor_global_impact_latest.md"
CACHE_SCHEMA_VERSION = 9


THEME_ACTIONS = {
    "Global Macro": {
        "impact": "\u5b8f\u89c2\u6d41\u52a8\u6027/\u6c47\u7387/\u98ce\u9669\u504f\u597d\u4f20\u5bfc\u5230\u5916\u8d44\u548c\u9ad8\u4f30\u503c\u8d44\u4ea7",
        "watch": "\u79bb\u5cb8\u4eba\u6c11\u5e01\u3001\u7f8e\u503a\u6536\u76ca\u7387\u3001\u9ec4\u91d1\u548c\u7eb3\u6307 ETF",
    },
    "AI Chips": {
        "impact": "\u5168\u7403 AI \u82af\u7247\u548c\u5149\u6a21\u5757\u9884\u671f\u4f20\u5bfc\u5230 A\u80a1\u7b97\u529b\u94fe",
        "watch": "Nvidia/TSMC/ASML \u6307\u5f15\u3001\u51fa\u53e3\u7ba1\u5236\u3001\u5149\u6a21\u5757\u8ba2\u5355\u9884\u671f",
    },
    "AI\u7b97\u529b": {
        "impact": "A\u80a1 AI \u670d\u52a1\u5668\u3001\u5149\u6a21\u5757\u3001\u6db2\u51b7\u548c\u6570\u636e\u4e2d\u5fc3\u76f4\u63a5\u53d7\u5f71\u54cd",
        "watch": "\u8ba2\u5355\u3001\u4ef7\u683c\u3001\u4f9b\u5e94\u9650\u5236\u3001\u4e1a\u7ee9\u6307\u5f15\u662f\u5426\u53d8\u5316",
    },
    "Energy Commodities": {
        "impact": "\u539f\u6cb9/\u9ec4\u91d1/\u6709\u8272/\u822a\u8fd0\u4f20\u5bfc\u5230\u8d44\u6e90\u548c\u5468\u671f\u54c1",
        "watch": "Brent/WTI\u3001\u91d1\u4ef7\u3001\u94dc\u4ef7\u3001\u822a\u8fd0\u8fd0\u4ef7\u7684\u8fde\u7eed\u6027",
    },
    "Geopolitics": {
        "impact": "\u907f\u9669/\u519b\u5de5/\u80fd\u6e90/\u822a\u8fd0\u4e3b\u9898\u5173\u6ce8\u5ea6\u4e0a\u5347",
        "watch": "\u5236\u88c1\u5347\u7ea7\u3001\u4f9b\u5e94\u6270\u52a8\u3001\u907f\u9669\u8d44\u4ea7\u662f\u5426\u653e\u91cf",
    },
    "Global EV": {
        "impact": "\u6d77\u5916 EV/\u7535\u6c60/\u667a\u9a7e\u53d8\u5316\u4f20\u5bfc\u5230\u65b0\u80fd\u6e90\u8f66\u94fe",
        "watch": "Tesla\u3001\u7535\u6c60\u4ef7\u683c\u3001\u667a\u9a7e\u8fdb\u5c55\u548c\u6d77\u5916\u9700\u6c42",
    },
}


def _holding_map() -> Dict[str, Dict[str, Any]]:
    report = build_risk_report()
    rows = report.get("top_positions", []) or []
    holdings: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("code") or "").strip().upper()
        if not code:
            continue
        holdings[code] = row
        holdings[code.split(".", 1)[0]] = row
    return holdings


def _theme_names(event: Dict[str, Any]) -> List[str]:
    return [str(item.get("theme")) for item in event.get("themes", []) or [] if item.get("theme")]


def _stock_hits(event: Dict[str, Any], holdings: Dict[str, Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    related = []
    hits = []
    for stock in event.get("related_stocks", []) or []:
        code = str(stock.get("code") or "").strip().upper()
        if not code:
            continue
        holding = holdings.get(code) or holdings.get(code.split(".", 1)[0])
        row = dict(stock)
        if holding:
            row["holding"] = True
            row["holding_weight"] = holding.get("weight", 0)
            row["holding_pnl"] = holding.get("pnl", 0)
            hits.append(row)
        related.append(row)
    return related, hits


def _event_priority(event: Dict[str, Any], holding_hits: List[Dict[str, Any]]) -> int:
    score = int(event.get("score", 0) or 0)
    severity_bonus = {"P0": 50, "P1": 30, "P2": 10}.get(str(event.get("severity") or "P3"), 0)
    holding_bonus = 45 if holding_hits else 0
    duplicate_penalty = -10 if event.get("is_duplicate") else 0
    return max(0, score + severity_bonus + holding_bonus + duplicate_penalty)


def _unique_join(items: List[str], sep: str = "\uff1b") -> str:
    cleaned = [item for item in items if item]
    return sep.join(dict.fromkeys(cleaned).keys())


def _theme_impact(event: Dict[str, Any]) -> Dict[str, str]:
    impacts = []
    watches = []
    for theme in _theme_names(event):
        cfg = THEME_ACTIONS.get(theme) or {}
        impacts.append(str(cfg.get("impact") or ""))
        watches.append(str(cfg.get("watch") or ""))
    return {
        "impact": _unique_join(impacts) or "\u9700\u7ed3\u5408\u76d8\u524d/\u76d8\u4e2d\u8868\u73b0\u505a\u4e8c\u6b21\u786e\u8ba4",
        "watch": _unique_join(watches) or "\u89c2\u5bdf\u76f8\u5173\u6807\u7684\u653e\u91cf\u3001\u8d44\u91d1\u6d41\u5411\u548c\u540c\u677f\u5757\u6269\u6563",
    }


def _cache_age_minutes(path: Path = GLOBAL_IMPACT_JSON) -> float | None:
    if not path.exists():
        return None
    return round((datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() / 60, 2)


def load_global_impact_cache(max_age_minutes: int = 90) -> Dict[str, Any] | None:
    if not GLOBAL_IMPACT_JSON.exists():
        return None
    age = _cache_age_minutes(GLOBAL_IMPACT_JSON)
    if age is None or age > max_age_minutes:
        return None
    try:
        payload = json.loads(GLOBAL_IMPACT_JSON.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None
    payload["cache"] = {"used": True, "age_minutes": age, "path": str(GLOBAL_IMPACT_JSON)}
    if not payload.get("text"):
        payload["text"] = format_global_impact_brief(payload)
    return payload


def save_global_impact_report(report: Dict[str, Any]) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(report)
    payload["cache"] = {"used": False, "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "path": str(GLOBAL_IMPACT_JSON)}
    text = str(payload.get("text") or format_global_impact_brief(payload))
    payload["text"] = text
    GLOBAL_IMPACT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    GLOBAL_IMPACT_MD.write_text(text, encoding="utf-8")
    return str(GLOBAL_IMPACT_JSON)


def build_global_impact_brief(limit: int = 80, min_score: int = 45, top_n: int = 8, use_cache: bool = False, max_cache_minutes: int = 90, save_cache: bool = True) -> Dict[str, Any]:
    if use_cache:
        cached = load_global_impact_cache(max_age_minutes=max_cache_minutes)
        if cached:
            return cached
    base = build_global_event_brief(limit=limit, min_score=min_score, top_n=max(top_n * 2, 12))
    holdings = _holding_map()
    events = []
    portfolio_hits = []
    for event in base.get("top_events", []) or []:
        related, hits = _stock_hits(event, holdings)
        enriched = dict(event)
        enriched["related_stocks"] = related
        enriched["holding_hits"] = hits
        enriched["priority"] = _event_priority(enriched, hits)
        enriched["themes_text"] = "\u3001".join(_theme_names(enriched)[:4]) or "\u672a\u5339\u914d"
        enriched["guidance"] = _theme_impact(enriched)
        events.append(enriched)
        if hits:
            portfolio_hits.append(enriched)
    events.sort(key=lambda item: (int(item.get("priority", 0)), int(item.get("score", 0))), reverse=True)
    watchlist = build_watchlist_report(limit_events=limit, top_n=6)
    active_themes: Counter[str] = Counter()
    for event in events:
        for theme in _theme_names(event):
            active_themes[theme] += 1
    active_theme_heat = active_themes.most_common(8)
    concept_momentum = build_concept_momentum_candidates(active_theme_heat, top_n=8)
    report = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "base_detected_at": base.get("detected_at"),
        "source_count": base.get("source_count"),
        "raw_count": base.get("raw_count"),
        "candidate_count": base.get("candidate_count"),
        "stale_count": base.get("stale_count", 0),
        "duplicate_raw_count": base.get("duplicate_raw_count", 0),
        "irrelevant_count": base.get("irrelevant_count", 0),
        "holding_count": len({str(v.get("code") or "") for v in holdings.values() if v.get("code")}),
        "urgent_events": events[: max(1, top_n)],
        "portfolio_hits": portfolio_hits[:6],
        "watchlist": watchlist.get("watchlist", [])[:6],
        "watchlist_meta": {
            "event_count": watchlist.get("event_count", 0),
            "raw_event_count": watchlist.get("raw_event_count", 0),
            "stale_excluded": watchlist.get("stale_excluded", 0),
            "duplicates_excluded": watchlist.get("duplicates_excluded", 0),
            "window_hours": watchlist.get("window_hours", 48),
        },
        "theme_heat": active_theme_heat,
        "historical_theme_heat": watchlist.get("theme_heat", []),
        "concept_momentum": concept_momentum,
    }
    report["text"] = format_global_impact_brief(report)
    report["cache"] = {"used": False, "age_minutes": 0, "path": str(GLOBAL_IMPACT_JSON)}
    if save_cache:
        save_global_impact_report(report)
    return report


def _stock_text(stocks: List[Dict[str, Any]], limit: int = 6) -> str:
    parts = []
    for item in stocks[:limit]:
        name = str(item.get("name") or "").strip()
        code = str(item.get("code") or "").strip()
        suffix = ""
        if item.get("holding"):
            weight = item.get("holding_weight", 0) or 0
            try:
                suffix = f"[\u6301\u4ed3 {float(weight) * 100:.1f}%]"
            except Exception:
                suffix = "[\u6301\u4ed3]"
        parts.append(f"{name}({code}){suffix}" if name or code else "")
    return "\u3001".join([p for p in parts if p]) or "\u6682\u65e0\u5185\u7f6e\u6620\u5c04"


def format_global_impact_brief(report: Dict[str, Any]) -> str:
    from domain.services.report_style_service import event_impact_label, event_summary_cn, join_cn, pct, theme_label, trend_label

    def candidate_chain(item: Dict[str, Any], momentum: Dict[str, Any]) -> str:
        concepts = [str(value or "") for value in item.get("concepts") or []]
        themes = []
        for theme in momentum.get("themes") or []:
            keywords = THEME_CONCEPT_KEYWORDS.get(str(theme), [])
            if any(keyword.lower() in concept.lower() for keyword in keywords for concept in concepts):
                themes.append(theme_label(theme))
        return f"{join_cn(themes, '事件主题')} → {join_cn(concepts, '概念待确认')}"

    lines = [
        "🌍 全球事件对A股影响",
        f"生成时间：{report.get('generated_at')}",
        "",
        "**持仓影响**",
    ]
    hits = report.get("portfolio_hits", []) or []
    if hits:
        for event in hits[:3]:
            lines.append(f"- **{event_summary_cn(event.get('title'), _theme_names(event))}**｜{_stock_text(event.get('holding_hits') or [])}")
    else:
        lines.append("- 未发现重要事件直接命中当前持仓。")

    lines.append("\n**重点事件与验证点**")
    displayed = 0
    seen_summaries: set[str] = set()
    for event in report.get("urgent_events", []) or []:
        related = event.get("related_stocks", []) or []
        guidance = event.get("guidance") or {}
        themes = join_cn((theme_label(item) for item in str(event.get('themes_text') or '').split('、')))
        summary = event_summary_cn(event.get('title'), _theme_names(event))
        if summary in seen_summaries:
            continue
        seen_summaries.add(summary)
        displayed += 1
        lines.append(f"{displayed}. **{summary}**｜{themes}｜{event_impact_label(event.get('severity'))}")
        lines.append(f"   传导：{guidance.get('impact')}")
        lines.append(f"   验证：{guidance.get('watch')}")
    watchlist = [item for item in (report.get("watchlist", []) or []) if item.get("direct_event_count", 0) > 0]
    if watchlist:
        lines.append("\n**事件映射标的**")
        for item in watchlist[:6]:
            holding = " [\u6301\u4ed3]" if item.get("holding") else ""
            lines.append(f"- **{item.get('name')}（{item.get('code')}）**{holding}｜需板块与量价二次确认")
    concept_momentum = report.get("concept_momentum") or {}
    lines.append("\n**概念板块动量候选**")
    if not concept_momentum.get("available"):
        lines.append("- 概念或动量数据不可用，本次不推荐个股。")
    elif not concept_momentum.get("candidates"):
        lines.append("- 没有通过 20/60 日正动量与过热过滤的候选")
    else:
        for item in (concept_momentum.get("candidates") or [])[:8]:
            lines.append(
                f"- **{item.get('name')}（{item.get('code')}）**｜{trend_label(item.get('trend'))}｜"
                f"20日 {pct(item.get('momentum_20d'), signed=True)}｜60日 {pct(item.get('momentum_60d'), signed=True)}"
            )
            lines.append(f"  依据：{candidate_chain(item, concept_momentum)}；需再确认板块成交与个股承接。")
    lines.extend(["", "**使用原则**", "- 新闻只决定观察方向；候选还必须经过板块强弱、个股动量与盘中量价确认，不直接追涨。"])
    return "\n".join(lines)

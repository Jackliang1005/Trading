
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
from domain.services.report_style_service import event_impact_label, event_summary_cn, join_cn, money, pct, risk_label, theme_label, trend_label
from domain.services.overseas_market_service import build_overseas_market_snapshot
from domain.services.concept_momentum_service import THEME_CONCEPT_KEYWORDS

WORKSPACE = Path("/root/.openclaw/workspace")
REPORTS_DIR = WORKSPACE / "reports"


def _global_focus_lines(brief: Dict[str, Any], limit: int = 3) -> List[str]:
    lines: List[str] = []
    seen_themes: set[tuple[str, ...]] = set()
    for event in brief.get("top_events") or []:
        signature = tuple(sorted(str(t.get("theme")) for t in event.get("themes") or [] if t.get("theme")))
        if signature and signature in seen_themes:
            continue
        seen_themes.add(signature)
        themes = join_cn((theme_label(t.get("theme")) for t in (event.get("themes") or [])[:3]), "待确认")
        impact = event_impact_label(event.get("severity"))
        lines.append(f"- **{_event_summary(event)}**｜{themes}｜{impact}")
        if len(lines) >= limit:
            break
    return lines or ["- 暂无达到质量阈值的隔夜事件。"]


def _impact_focus_lines(impact: Dict[str, Any], limit: int = 3) -> List[str]:
    lines: List[str] = []
    seen_themes: set[tuple[str, ...]] = set()
    for event in impact.get("urgent_events") or []:
        signature = tuple(sorted(str(t.get("theme")) for t in event.get("themes") or [] if t.get("theme")))
        if signature and signature in seen_themes:
            continue
        seen_themes.add(signature)
        guidance = event.get("guidance") or {}
        lines.append(f"- **{_event_summary(event)}**")
        lines.append(f"  观察：{guidance.get('watch') or '等待盘前量价确认'}")
        if len(lines) // 2 >= limit:
            break
    return lines or ["- 暂无需要提升优先级的全球影响事件。"]


def _watchlist_focus_lines(impact: Dict[str, Any], limit: int = 5) -> List[str]:
    direct_codes = {
        str(stock.get("code") or "").strip()
        for event in (impact.get("urgent_events") or [])[:3]
        for stock in (event.get("related_stocks") or [])
        if stock.get("code")
    }
    lines: List[str] = []
    for item in (impact.get("watchlist") or []):
        if str(item.get("code") or "").strip() not in direct_codes:
            continue
        holding = "｜当前持仓" if item.get("holding") else ""
        lines.append(f"- **{item.get('name')}（{item.get('code')}）**｜本次新闻直接映射{holding}；仍需板块与量价确认。")
        if len(lines) >= limit:
            break
    return lines or ["- 暂无通过事件质量门槛的观察标的。"]


def _event_summary(event: Dict[str, Any]) -> str:
    return event_summary_cn(event.get("title"), (item.get("theme") for item in event.get("themes") or []))


def _candidate_theme_chain(item: Dict[str, Any], momentum: Dict[str, Any]) -> str:
    concepts = [str(value or "") for value in item.get("concepts") or []]
    matched_themes: List[str] = []
    for theme in momentum.get("themes") or []:
        keywords = THEME_CONCEPT_KEYWORDS.get(str(theme), [])
        if any(keyword.lower() in concept.lower() for keyword in keywords for concept in concepts):
            matched_themes.append(theme_label(theme))
    theme_text = join_cn(matched_themes, "事件主题")
    return f"{theme_text} → {join_cn(concepts, '概念待确认')}"


def _morning_conclusion_lines(payload: Dict[str, Any]) -> List[str]:
    conclusions: List[str] = []
    overseas = payload.get("overseas") or {}
    market_items = [item for key in ("us", "asia") for item in ((overseas.get(key) or {}).get("items") or [])]
    if market_items:
        weakest = min(market_items, key=lambda item: float(item.get("change_pct") or 0))
        if float(weakest.get("change_pct") or 0) <= -1:
            conclusions.append(f"- 海外风险偏好偏弱，{weakest.get('name')} {float(weakest.get('change_pct') or 0):+.1f}%；A股开盘先防守，成长方向不抢竞价。")
        elif all(float(item.get("change_pct") or 0) >= 0 for item in market_items):
            conclusions.append("- 海外主要指数整体偏强，但A股仍需等待集合竞价和成交量确认，不直接外推高开。")
    events = (payload.get("global_impact") or {}).get("urgent_events") or []
    if events:
        event = events[0]
        conclusions.append(f"- 隔夜主催化：{_event_summary(event)}；只观察对应板块是否放量，不按新闻标题追涨。")
    risk = payload.get("risk") or {}
    if risk.get("available") and float(risk.get("top1_ratio") or 0) >= 0.30:
        conclusions.append(f"- 组合第一大持仓达 {float(risk.get('top1_ratio') or 0)*100:.1f}%，开盘首要任务仍是降集中度，而不是扩仓。")
    return conclusions[:3] or ["- 关键海外行情或新闻数据不足；开盘前不生成方向性判断。"]


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
    overseas = build_overseas_market_snapshot()
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "global": global_brief,
        "global_impact": global_impact,
        "risk": risk,
        "longterm": {"summary": longterm_summary, "text": build_longterm_snapshot_text(longterm_summary)},
        "overseas": overseas,
    }
    payload["text"] = format_morning_brief(payload)
    return payload


def format_morning_brief(payload: Dict[str, Any]) -> str:
    risk = payload.get("risk") or {}
    longterm = payload.get("longterm") or {}
    overseas = payload.get("overseas") or {}
    lines = [
        "🌅 OpenClaw A股盘前简报",
        f"生成时间：{payload.get('generated_at')}",
        "",
        "**核心结论**",
        *_morning_conclusion_lines(payload),
        "",
        "**隔夜美股与日韩开盘**",
    ]
    for group_name, key in (("美股", "us"), ("日韩", "asia")):
        group = overseas.get(key) or {}
        if group.get("available"):
            summary = "；".join(
                f"{item.get('name')} {pct(item.get('change_pct'), signed=True)}"
                for item in group.get("items") or []
            )
            observed = join_cn((str(item.get("as_of") or "") for item in group.get("items") or []), "时间待确认")
            lines.append(f"- {group_name}（数据时间 {observed}）：{summary}。")
        else:
            lines.append(f"- {group_name}：数据不可用（{group.get('reason') or '原因未知'}），不作走势推断。")
    lines.extend([
        "",
        "**隔夜焦点**",
        *_global_focus_lines(payload.get("global") or {}, limit=3),
        "",
        "**传导路径与验证点**",
        *_impact_focus_lines(payload.get("global_impact") or {}, limit=3),
        "",
        "**盘前观察标的**",
        *_watchlist_focus_lines(payload.get("global_impact") or {}, limit=5),
    ])
    momentum = (payload.get("global_impact") or {}).get("concept_momentum") or {}
    candidates = momentum.get("candidates") or []
    if candidates:
        lines.extend(["", "**概念板块动量候选**"])
        for item in candidates[:5]:
            lines.append(f"- **{item.get('name')}（{item.get('code')}）**｜{trend_label(item.get('trend'))}｜20日 {pct(item.get('momentum_20d'), signed=True)}｜60日 {pct(item.get('momentum_60d'), signed=True)}")
            lines.append(f"  依据：{_candidate_theme_chain(item, momentum)}；需再确认板块成交与个股承接。")
    lines.extend(["", "**组合风险**"])
    if risk.get("available"):
        cash_line = (
            f"- 现金 {pct(risk.get('cash_ratio', 0)*100)}；"
            if risk.get("cash_complete", True)
            else f"- 可验证现金 {money(risk.get('cash'))}；部分账户资产字段异常，不计算完整现金占比；"
        )
        lines.extend([
            f"- 数据日 {risk.get('as_of')}；{risk.get('positions_count')} 只持仓，市值 {money(risk.get('total_market_value'))}，浮动盈亏 {money(risk.get('total_unrealized_pnl'))}。",
            cash_line + f"第一大持仓 {pct(risk.get('top1_ratio', 0)*100)}，前三大持仓 {pct(risk.get('top3_ratio', 0)*100)}。",
            "- " + join_cn((risk_label(item) for item in risk.get("risk_flags") or [])) + "。",
        ])
    else:
        lines.append("- 持仓快照不可用；不生成仓位判断，开盘前须人工核验账户。")
    lines.extend([
        "",
        "**开盘执行顺序**",
        "- 09:25：检查外盘、离岸人民币及主题前排竞价，确认催化是否延续。",
        "- 09:35：持仓弱于板块且无放量承接时，先降风险，不先开新仓。",
        "- 10:30：只保留强于板块、量能确认的方向；弱反弹视为风险处理窗口。",
        "",
        "**数据边界**",
    ])
    lines.append("- 海外指数均标注各自数据时间；行情为空、过期或尚未开盘时不做方向推断。")
    lines.append("- 国金实时链路未恢复稳定前按降级处理；禁止用历史数据冒充实时状态。")
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

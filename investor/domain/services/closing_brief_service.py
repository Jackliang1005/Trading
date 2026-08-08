
#!/usr/bin/env python3
"""Post-market closing brief for OpenClaw Investor."""

from __future__ import annotations

import contextlib
import io
import json
import math
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

import db
from domain.policies.advisor_policy import load_advisor_policy
from domain.services.risk_report_service import build_risk_report
from domain.services.global_impact_service import build_global_impact_brief
from domain.services.longterm_portfolio_service import build_longterm_snapshot_text, load_longterm_snapshot, longterm_plan_status, summarize_longterm_snapshot
from domain.services.market_review_service import build_market_review, format_market_review
from domain.services.event_service import _independent_theme_count, _is_market_relevant_event, _near_duplicate_title
from domain.services.report_style_service import event_impact_label, event_summary_cn, join_cn, source_label, theme_label
from domain.services.concept_momentum_service import THEME_CONCEPT_KEYWORDS

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


def _display_date(value: object) -> str:
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return raw or "未取得"


def _event_session_date(payload: Dict[str, Any], captured_at: str) -> date | None:
    published = str(payload.get("published_at") or "").strip()
    if published:
        parsed = _parse_date(published)
        if parsed:
            return parsed
    try:
        # SQLite datetime('now') is UTC; reports operate in Asia/Shanghai.
        captured_utc = datetime.fromisoformat(str(captured_at)[:19])
        return (captured_utc + timedelta(hours=8)).date()
    except (TypeError, ValueError):
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
    seen = set()
    seen_titles: List[str] = []
    for row in rows:
        try:
            payload = json.loads(row["data"])
        except Exception:
            continue
        if _event_session_date(payload, row["captured_at"]) != target:
            continue
        event_text = f"{payload.get('title', '')} {payload.get('summary', '')}"
        if not _is_market_relevant_event(event_text, _independent_theme_count(payload.get("themes") or [])):
            continue
        identity = str(payload.get("event_id") or "").strip() or (str(payload.get("title") or "").strip(), str(payload.get("url") or "").strip())
        title = str(payload.get("title") or "")
        if identity in seen or any(_near_duplicate_title(title, previous) for previous in seen_titles):
            continue
        seen.add(identity)
        seen_titles.append(title)
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
        published = str(event.get("published_at") or "unknown")
        lines.append(f"- {event.get('severity', 'P3')}/{event.get('score', 0)} priority={event.get('priority', 0)} [{published}] {_clean_event_title(event.get('title', ''))} | A-share={stocks}")
        lines.append(f"  next_watch={guidance.get('watch', '')}")
    return lines or ["- no urgent global impact item"]


def _closing_watchlist_lines(impact: Dict[str, Any], limit: int = 5) -> List[str]:
    lines: List[str] = []
    for item in (impact.get("watchlist") or [])[:limit]:
        holding = " holding" if item.get("holding") else ""
        lines.append(f"- {item.get('code')} {item.get('name')} priority={item.get('priority')} unique_events={item.get('event_count')} direct={item.get('direct_event_count', 0)} mapping={item.get('mapping_confidence', 'theme_only')}{holding}")
    return lines or ["- no event-driven watchlist item"]


def _cn(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _clean_event_title(value: Any) -> str:
    return re.sub(r"^(\d{1,2}):\s+(\d{2})\s+", r"\1:\2 ", _cn(value))


def _market_risk_off(review: Dict[str, Any]) -> bool:
    latest = []
    for item in review.get("indices") or []:
        rows = item.get("rows") or []
        if rows and rows[-1].get("change_pct") is not None:
            latest.append(float(rows[-1]["change_pct"]))
    return review.get("sentiment") == "偏弱" or sum(latest) <= -3


def _theme_names(payload: Dict[str, Any]) -> List[str]:
    events = payload.get("events") or {}
    impact = payload.get("global_impact") or {}
    names: List[str] = []
    for name, _count in events.get("themes") or []:
        if name and name not in names:
            names.append(str(name))
    for name, _count in impact.get("theme_heat") or []:
        if name and name not in names:
            names.append(str(name))
    return names


def _next_session_opportunity_lines(payload: Dict[str, Any], limit: int = 6, policy: Dict[str, Any] | None = None) -> List[str]:
    active_policy = policy or load_advisor_policy()
    impact = payload.get("global_impact") or {}
    risk = payload.get("risk") or {}
    cash_ratio = float(risk.get("cash_ratio") or 0)
    cash_complete = bool(risk.get("cash_complete", True))
    themes = set(_theme_names(payload))
    review = payload.get("market_review") or {}
    risk_flags = set(risk.get("risk_flags") or [])
    risk_off = _market_risk_off(review) or bool({"top1_concentration_high", "top3_concentration_high"} & risk_flags)
    lines: List[str] = []
    trend_labels = {"strong_up": "强势上行", "improving": "动量改善", "positive": "多头延续", "overheated": "短线过热", "weak": "偏弱"}
    concept_momentum = impact.get("concept_momentum") or {}
    for item in (concept_momentum.get("candidates") or [])[:limit]:
        code = _cn(item.get("code"))
        name = _cn(item.get("name"), code)
        concepts = "、".join(str(value) for value in item.get("concepts") or []) or "未命名概念"
        mapped_themes = [
            theme_label(theme)
            for theme in concept_momentum.get("themes") or []
            if any(
                keyword.lower() in concept_name.lower()
                for keyword in THEME_CONCEPT_KEYWORDS.get(str(theme), [])
                for concept_name in item.get("concepts") or []
            )
        ]
        evidence_chain = f"{join_cn(mapped_themes, '事件主题')} → {concepts}"
        stance = "只观察，不追高"
        if risk_off:
            stance = "仅观察；市场或组合风险未解除前不新增仓位"
        elif cash_complete and cash_ratio < float(active_policy["minimum_cash_ratio"]):
            stance = "现金不足，必须先换仓/减仓才可参与"
        else:
            stance = "列入动量候选；仅在概念板块与个股同步放量、且未明显高开时再评估"
        trend = trend_labels.get(str(item.get("trend") or ""), str(item.get("trend") or "未知"))
        lines.extend([
            f"- **{name}（{code}）**｜{evidence_chain}",
            f"  动量：{trend}｜20日 {float(item.get('momentum_20d') or 0):+.2f}%｜60日 {float(item.get('momentum_60d') or 0):+.2f}%｜板块当日 {float(item.get('best_concept_change') or 0):+.2f}%",
            f"  结论：{stance}。",
        ])
    if "AI算力" in themes and not any("AI算力" in line for line in lines):
        action = "仅观察板块强弱，不新增仓位。" if risk_off else "只在板块放量且前排不炸板时再评估，不把主题映射当成个股催化。"
        lines.append(f"- **AI算力链**｜新鲜事件热度靠前，但个股仍是宽泛主题映射。{action}")
    if "Geopolitics" in themes or "Global Macro" in themes:
        lines.append("- **避险/宏观链**｜关注黄金、军工、航运和离岸人民币；仅作风险监测，不因宽泛主题直接开仓。")
    return lines or ["- 暂无通过新鲜度和数据质量校验的事件驱动机会；下一交易日以持仓风险处理为主。"]


def _position_theme_hint(position: Dict[str, Any], themes: List[str]) -> str:
    code = _cn(position.get("code"))
    name = _cn(position.get("name"))
    if code == "513290.SH" or "纳指" in name or "生物科技" in name:
        return "受美股风险偏好、美元利率和生物科技板块影响；与本次全球宏观、地缘风险相关。"
    if code in {"603986.SH", "001309.SZ", "600584.SH"} or any(key in name for key in ("兆易", "德明利", "长电")):
        if "AI算力" in themes:
            return "半导体/存储链与 AI 算力热度有关，但当前组合已经承受亏损，不能用热点直接补仓。"
        return "半导体链持仓，先看板块强弱和量能修复。"
    if code == "000725.SZ" or "京东方" in name:
        return "面板/显示链，偏周期与交易仓属性，重点看开盘资金是否承接。"
    return "按持仓权重、盈亏和板块强弱处理。"


def _closing_action_level(position: Dict[str, Any], policy: Dict[str, Any]) -> str:
    weight = float(position.get("weight") or 0)
    pnl = float(position.get("pnl") or 0)
    if weight >= float(policy["single_position_prepare_ratio"]):
        return "verify"
    if weight >= float(policy["loss_position_review_ratio"]) and pnl < 0:
        return "verify"
    return "observe"


def _position_decisions(risk: Dict[str, Any], payload: Dict[str, Any], policy: Dict[str, Any]) -> List[Dict[str, Any]]:
    decisions: List[Dict[str, Any]] = []
    themes = _theme_names(payload)
    cash_ratio = float(risk.get("cash_ratio") or 0)
    cash_complete = bool(risk.get("cash_complete", True))
    for p in (risk.get("top_positions") or [])[:8]:
        item = dict(p)
        weight = float(item.get("weight") or 0)
        pnl = float(item.get("pnl") or 0)
        sources = list(item.get("sources") or ([item.get("source")] if item.get("source") else []))
        level = _closing_action_level(item, policy)
        target_weight = None
        if weight >= float(policy["single_position_prepare_ratio"]):
            target_weight = float(policy["single_position_reduce_target_ratio"])
            if len(sources) > 1:
                advice = (
                    "不加仓；同一证券分布在多个账户，先逐账户核对可用股数。下一交易日若弱于所属板块或继续放量下跌，"
                    f"再准备把合计权重降至 {target_weight:.0%} 以下，收盘计划不按合计股数生成卖出数量。"
                )
            else:
                total_asset = float(risk.get("effective_total_asset") or 0)
                market_value = float(item.get("market_value") or 0)
                volume = int(item.get("volume") or 0)
                implied_price = market_value / volume if volume > 0 else 0
                excess_value = max(0.0, market_value - total_asset * target_weight)
                suggested_qty = min(volume, int(math.ceil(excess_value / implied_price / 100.0) * 100)) if implied_price > 0 else 0
                quantity_text = f"参考减持约 {suggested_qty} 股（按快照隐含价估算，执行前核对可用股数和实时价）；" if suggested_qty else ""
                advice = (
                    "不加仓；若下一交易日开盘 30 分钟弱于所属板块或继续放量下跌，"
                    f"{quantity_text}目标是把单票权重压回 {target_weight:.0%} 以下。"
                )
        elif weight >= float(policy["loss_position_review_ratio"]) and pnl < 0:
            target_weight = float(policy["loss_position_reduce_target_ratio"])
            advice = "不补亏损仓；只在放量站回板块强势队列后保留，反弹无量则减仓修复组合弹性。"
        elif str(item.get("source") or "") == "trade":
            advice = "按交易仓处理；高开不追，低开无承接先降风险，强于板块才继续观察。"
        elif pnl < 0:
            advice = "先等止跌确认，不用摊低成本替代风控。"
        else:
            advice = "维持观察，除非出现明确板块催化和量能确认。"
        if cash_complete and cash_ratio < float(policy["minimum_cash_ratio"]):
            advice += " 当前现金低于政策下限，任何新机会都必须来自减仓腾挪。"
        decisions.append(
            {
                **item,
                "action_level": level,
                "target_weight": target_weight,
                "requires_account_split": len(sources) > 1,
                "theme_hint": _position_theme_hint(item, themes),
                "advice": advice,
            }
        )
    return decisions


def _position_action_lines(risk: Dict[str, Any], payload: Dict[str, Any], policy: Dict[str, Any] | None = None) -> List[str]:
    if not risk.get("available"):
        return ["- 持仓快照不可用，先补齐 /risk 数据，再给出个股处理。"]
    positions = risk.get("top_positions") or []
    if not positions:
        return ["- 当前没有可处理持仓。"]
    active_policy = policy or load_advisor_policy()
    lines: List[str] = []
    for p in _position_decisions(risk, payload, active_policy):
        code = _cn(p.get("code"))
        name = _cn(p.get("name"), code)
        weight = float(p.get("weight") or 0)
        pnl = float(p.get("pnl") or 0)
        source_names = join_cn((source_label(source) for source in p.get("sources") or []), source_label(p.get("source")))
        level_name = {"observe": "观察", "verify": "核验"}.get(str(p.get("action_level") or "observe"), "观察")
        lines.extend([
            f"- **{name}（{code}）**｜行动 {level_name}｜仓位 {weight*100:.1f}%｜盈亏 {pnl:+,.0f}｜{source_names}",
            f"  建议：{p.get('advice')}",
        ])
    return lines


def _next_session_trigger_lines(risk: Dict[str, Any], policy: Dict[str, Any] | None = None) -> List[str]:
    active_policy = policy or load_advisor_policy()
    cash_ratio = float(risk.get("cash_ratio") or 0)
    cash_complete = bool(risk.get("cash_complete", True))
    top1 = float(risk.get("top1_ratio") or 0)
    top3 = float(risk.get("top3_ratio") or 0)
    lines = [
        "- **09:25**｜核对隔夜外盘、离岸人民币与持仓板块竞价；数据缺失时不做方向判断。",
        "- **09:35**｜持仓弱于板块且没有放量承接时，先处理风险仓，不开新仓。",
        "- **10:30**｜只保留强于板块且量能修复的持仓；弱反弹视为减仓窗口。",
    ]
    if cash_complete and cash_ratio < float(active_policy["minimum_cash_ratio"]):
        lines.append("- **现金约束**｜默认不新增标的，除非先卖出低优先级仓位腾出现金。")
    if top1 >= float(active_policy["single_position_alert_ratio"]) or top3 >= float(active_policy["top3_position_alert_ratio"]):
        lines.append(f"- **组合约束**｜单票 {top1*100:.1f}%、前三大 {top3*100:.1f}%；首要目标是降集中度。")
    return lines


def build_decision_plan(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build a structured next-session action plan from the closing brief payload."""
    risk = payload.get("risk") or {}
    policy = load_advisor_policy()
    return {
        "source": "closing_brief",
        "date": payload.get("date", ""),
        "generated_at": payload.get("generated_at", ""),
        "opportunities": _next_session_opportunity_lines(payload, limit=6, policy=policy),
        "position_actions": _position_action_lines(risk, payload, policy=policy),
        "position_decisions": _position_decisions(risk, payload, policy),
        "opening_triggers": _next_session_trigger_lines(risk, policy=policy),
        "advisor_policy": policy,
        "risk_flags": risk.get("risk_flags") or [],
        "cash_ratio": risk.get("cash_ratio", 0),
        "top1_ratio": risk.get("top1_ratio", 0),
        "top3_ratio": risk.get("top3_ratio", 0),
        "positions": risk.get("top_positions") or [],
    }


def build_closing_brief(target_date: str = "") -> Dict[str, Any]:
    requested_target = date.fromisoformat(target_date) if target_date else date.today()
    market_review = build_market_review(requested_target.isoformat())
    target = _parse_date(market_review.get("as_of")) or requested_target
    events = _summarize_events(_load_events_for_day(target))
    global_impact = build_global_impact_brief(limit=80, min_score=45, top_n=5, use_cache=True, max_cache_minutes=120)
    risk = build_risk_report()
    longterm_summary = summarize_longterm_snapshot(load_longterm_snapshot())
    audit = _load_audit()
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": target.isoformat(),
        "requested_date": requested_target.isoformat(),
        "events": events,
        "global_impact": global_impact,
        "risk": risk,
        "longterm": {"summary": longterm_summary, "text": build_longterm_snapshot_text(longterm_summary)},
        "audit": audit,
        "market_review": market_review,
    }
    payload["decision_plan"] = build_decision_plan(payload)
    payload["text"] = format_closing_brief(payload)
    return payload


def format_closing_brief(payload: Dict[str, Any]) -> str:
    events = payload.get("events") or {}
    risk = payload.get("risk") or {}
    longterm = payload.get("longterm") or {}
    audit = payload.get("audit") or {}
    decision_plan = payload.get("decision_plan") or build_decision_plan(payload)
    review = payload.get("market_review") or {}
    impact = payload.get("global_impact") or {}
    risk_flags = set(risk.get("risk_flags") or [])
    risk_off = _market_risk_off(review) or bool({"top1_concentration_high", "top3_concentration_high"} & risk_flags)
    risk_label = "🔴 风控优先" if risk_off else "🟠 谨慎观察" if risk_flags else "🟢 状态平稳"
    market_label = str(review.get("sentiment") or "未知")
    quality_issues = review.get("quality_issues") or []
    concept = impact.get("concept_momentum") or {}

    lines = [
        f"📊 **A股收盘简报｜{payload.get('date')}**",
        f"{risk_label}　生成于 {str(payload.get('generated_at') or '')[11:16]}",
        "",
        "**📌 核心结论**",
        f"市场情绪 **{market_label}**；组合单票最高 **{float(risk.get('top1_ratio') or 0)*100:.1f}%**、前三大 **{float(risk.get('top3_ratio') or 0)*100:.1f}%**。"
        + ("下一交易日先降集中度，不新增仓位。" if risk_off else "下一交易日等待板块与个股同步确认。"),
        "",
        "**📈 市场收盘**",
    ]
    for item in review.get("indices") or []:
        rows = item.get("rows") or []
        if not rows:
            lines.append(f"- {item.get('name')}：未取得")
            continue
        latest = rows[-1]
        change = latest.get("change_pct")
        change_text = f"{float(change):+.2f}%" if change is not None else "涨跌缺失"
        turnover = item.get("turnover_yi")
        turnover_text = f"｜成交 {float(turnover):.0f} 亿" if turnover is not None else ""
        lines.append(f"- {item.get('name')}　**{float(latest.get('close') or 0):.2f}**　{change_text}{turnover_text}")
    lines.append(
        f"- 涨停 {review.get('limit_up') if review.get('limit_up') is not None else '未取得'}｜"
        f"跌停 {review.get('limit_down') if review.get('limit_down') is not None else '未取得'}｜"
        f"最高连板 {(str(review.get('max_height')) + '板') if review.get('max_height') is not None else '未取得'}"
    )

    lines.extend(["", "**💼 组合风险**"])
    if risk.get("available"):
        cash_text = (
            f"现金 **{float(risk.get('cash') or 0):,.0f}**（{float(risk.get('cash_ratio') or 0)*100:.1f}%）"
            if risk.get("cash_complete", True)
            else f"可验证现金 **{float(risk.get('cash') or 0):,.0f}**（完整占比不可用）"
        )
        lines.extend([
            f"- 市值 **{float(risk.get('total_market_value') or 0):,.0f}**｜{cash_text}",
            f"- 浮动盈亏 **{float(risk.get('total_unrealized_pnl') or 0):+,.0f}**｜快照 {risk.get('as_of')}（{risk.get('snapshot_age_days')} 天）",
        ])
    else:
        lines.append("- ⚠️ 持仓快照不可用，本报告不提供仓位建议。")
    lines.extend(decision_plan.get("position_actions") or [])

    lines.extend(["", "**🎯 下一交易日观察**"])
    lines.extend(decision_plan.get("opportunities") or ["- 暂无通过质量校验的候选。"])

    lines.extend(["", "**⏱️ 开盘执行顺序**"])
    lines.extend(decision_plan.get("opening_triggers") or [])

    lines.extend(["", "**🌍 事件催化**"])
    report_date = str(payload.get("date") or "")[:10]
    urgent = [
        event for event in (impact.get("urgent_events") or [])
        if str(event.get("published_at") or "")[:10] == report_date
    ]
    if urgent:
        for event in urgent[:3]:
            themes = join_cn((theme_label(item.get("theme")) for item in (event.get("themes") or [])[:3]), "未分类")
            summary = event_summary_cn(event.get("title"), (item.get("theme") for item in event.get("themes") or []))
            lines.append(f"- **{summary}**｜{event_impact_label(event.get('severity'))}")
            lines.append(f"  主题：{themes}｜发布时间：{event.get('published_at', '未知')}")
            guidance = event.get("guidance") or {}
            if guidance.get("impact"):
                lines.append(f"  A股传导：{guidance.get('impact')}")
    else:
        lines.append(f"- {report_date} 没有通过新鲜度与相关性校验的重要事件；其他日期事件不混入本交易日复盘。")

    longterm_summary = longterm.get("summary") or {}
    lines.extend(["", "**🧭 长线组合**"])
    if longterm_summary.get("available"):
        lines.append(
            f"- 净值 **{float(longterm_summary.get('nav') or 0):,.2f}**｜现金 {float(longterm_summary.get('cash') or 0):,.2f}（{float(longterm_summary.get('cash_ratio') or 0)*100:.1f}%）｜持仓 {int(longterm_summary.get('holdings_count') or 0)} 只"
        )
        lines.append(f"- {longterm_plan_status(longterm_summary)}｜数据日 {longterm_summary.get('as_of', '未知')}")
    else:
        lines.append("- 长线快照不可用。")

    market_quality = "🟢" if review.get("quality_ok") else "🟡"
    concept_quality = "🟢" if concept.get("available") else "🟡"
    lines.extend([
        "",
        "**🧪 数据可信度**",
        f"- {market_quality} 市场数据：{'完整' if review.get('quality_ok') else '部分缺失｜' + '、'.join({'limit_up_unavailable':'涨停数据未取得','limit_down_unavailable':'跌停数据未取得','limit_ladder_unavailable':'连板数据未取得'}.get(str(x), str(x)) for x in quality_issues)}",
        "- 🟢 事件过滤：已排除旧闻、非市场内容和近似重复；仅展示报告交易日事件。",
        f"- {concept_quality} 概念/动量：概念数据日 {_display_date(concept.get('concept_date'))}｜动量数据日 {_display_date(concept.get('momentum_date'))}｜通过筛选 {len(concept.get('candidates') or [])} 只",
        f"- 🟢 持仓快照：数据日 {risk.get('as_of', '未取得')}｜账户 {join_cn((source_label(item) for item in sorted((risk.get('by_source') or {}).keys())), '未知')}",
    ])
    if risk.get("available") and not risk.get("cash_complete", True):
        lines.append("- 🟠 账户资产：部分账户现金或总资产未通过合理性校验；不据此判断完整现金仓位。")
    if audit.get("blocked") or audit.get("warning"):
        blocked_items = audit.get("blocked") or []
        warning_items = audit.get("warning") or []
        blocked_names = {
            str(item.get("name") if isinstance(item, dict) else item)
            for item in blocked_items
        }
        guojin_only = bool(blocked_names) and blocked_names.issubset(
            {"holdings_account_monitor", "service_health_diagnostics"}
        )
        if guojin_only:
            lines.append("- 🟠 运行能力：国金实时交易链路不可用；相关账户数据已明确降级，不把缺失值写成零。")
        else:
            lines.append(f"- 🟠 运行能力：有 {len(blocked_items)} 项受阻、{len(warning_items)} 项需观察；详情见“能力审计”。")
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

#!/usr/bin/env python3
"""Unified, evidence-first home view for the personal investment advisor."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from domain.services.report_style_service import event_summary_cn, join_cn, money, pct, risk_label, theme_label
from domain.services.risk_report_service import build_risk_report
from domain.services.weekly_report_service import _summarize_intraday_predictions


WORKSPACE = Path("/root/.openclaw/workspace")
REPORTS_DIR = WORKSPACE / "reports"

BLOCKED_LABELS = {
    "holdings_account_monitor": "双账户持仓与资金读取",
    "service_health_diagnostics": "账户与策略服务诊断",
}

ACTION_LABELS = {"observe": "观察", "verify": "核验", "prepare": "准备"}


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_dt(value: object) -> datetime | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except (TypeError, ValueError):
            continue
    return None


def _fresh_decision(payload: Dict[str, Any], current: datetime) -> bool:
    generated = _parse_dt(payload.get("generated_at"))
    if not generated or generated.date() != current.date():
        return False
    return timedelta(0) <= current - generated <= timedelta(hours=4)


def _audit_summary(reports_dir: Path) -> Dict[str, Any]:
    payload = _load_json(reports_dir / "investor_assistant_capability_audit_latest.json")
    blocked = [item for item in payload.get("items") or [] if item.get("status") == "blocked"]
    warnings = [item for item in payload.get("items") or [] if item.get("status") == "warn"]
    return {
        "available": bool(payload),
        "generated_at": payload.get("generated_at", ""),
        "blocked": [BLOCKED_LABELS.get(str(item.get("name") or ""), str(item.get("name") or "其他能力")) for item in blocked],
        "warnings": [BLOCKED_LABELS.get(str(item.get("name") or ""), str(item.get("name") or "其他能力")) for item in warnings],
    }


def _event_summary(reports_dir: Path) -> Dict[str, Any]:
    closing = _load_json(reports_dir / "investor_closing_brief_latest.json")
    event_block = closing.get("events") or {}
    rows = []
    seen_titles: set[str] = set()
    for item in event_block.get("top_events") or []:
        themes = [theme_label(theme.get("theme")) for theme in (item.get("themes") or [])[:3]]
        display_title = event_summary_cn(item.get("title"), themes)
        if display_title in seen_titles:
            continue
        seen_titles.add(display_title)
        rows.append(
            {
                "title": display_title,
                "themes": themes,
                "published_at": item.get("published_at", ""),
                "url": item.get("url", ""),
            }
        )
        if len(rows) >= 3:
            break
    return {
        "as_of": closing.get("date") or str(closing.get("generated_at") or "")[:10],
        "events": rows,
    }


def _decision_summary(reports_dir: Path, current: datetime) -> Dict[str, Any]:
    payload = _load_json(reports_dir / "investor_decision_monitor_latest.json")
    fresh = _fresh_decision(payload, current)
    positions = payload.get("tracked_positions") or [] if fresh else []
    return {
        "available": bool(payload),
        "fresh": fresh,
        "generated_at": payload.get("generated_at", ""),
        "prepared": [item for item in positions if item.get("action_level") == "prepare"],
        "verify": [item for item in positions if item.get("action_level") == "verify"],
    }


def _build_actions(risk: Dict[str, Any], decision: Dict[str, Any], events: Dict[str, Any]) -> List[Dict[str, str]]:
    actions: List[Dict[str, str]] = []
    for item in decision.get("prepared") or []:
        hint = item.get("execution_hint") or {}
        detail = str(hint.get("note") or item.get("suggestion") or "形成降风险计划，执行前核对实时价格和可用数量。")
        actions.append({"level": "prepare", "text": f"{item.get('name') or item.get('code')}：{detail}"})
    for item in decision.get("verify") or []:
        actions.append({"level": "verify", "text": f"{item.get('name') or item.get('code')}：{item.get('suggestion') or '补齐实时证据后再判断。'}"})

    flags = set(str(flag) for flag in risk.get("risk_flags") or [])
    if "top1_concentration_high" in flags:
        actions.append({"level": "verify", "text": "下一交易时段优先核验第一大持仓相对强弱与承接；集中度未改善前不扩张新仓。"})
    if not risk.get("cash_complete"):
        actions.append({"level": "verify", "text": "恢复并回读缺失账户的资产字段；完整现金不可验证前，不判断资金充足或不足。"})
    if not risk.get("position_coverage_complete", True):
        actions.append({"level": "verify", "text": "重新采集双账户持仓并核对同代码跨账户明细；覆盖完整前不按当前明细计算精确总仓位。"})
    if risk.get("stale_account_sources"):
        actions.append({"level": "verify", "text": "历史账户持仓只用于保持组合连续性；恢复实时接口并逐账户回读后，才能升级到准备层级。"})
    if events.get("events"):
        actions.append({"level": "observe", "text": f"跟踪“{events['events'][0]['title']}”的盘前延续性，只在板块与量价同时确认后升级。"})
    if not actions:
        actions.append({"level": "observe", "text": "当前没有达到核验或准备门槛的动作，继续等待新证据。"})
    return actions[:6]


def build_advisor_brief(now: datetime | None = None, reports_dir: Path = REPORTS_DIR) -> Dict[str, Any]:
    current = now or datetime.now()
    risk = build_risk_report()
    audit = _audit_summary(reports_dir)
    events = _event_summary(reports_dir)
    decision = _decision_summary(reports_dir, current)
    intraday = _summarize_intraday_predictions(current.date() - timedelta(days=6), current.date(), reports_dir=reports_dir)
    actions = _build_actions(risk, decision, events)
    overall_level = "prepare" if any(item["level"] == "prepare" for item in actions) else (
        "verify" if any(item["level"] == "verify" for item in actions) else "observe"
    )
    brief = {
        "generated_at": current.strftime("%Y-%m-%d %H:%M:%S"),
        "overall_action_level": overall_level,
        "risk": risk,
        "audit": audit,
        "events": events,
        "decision": decision,
        "intraday": intraday,
        "actions": actions,
    }
    brief["text"] = format_advisor_brief(brief)
    return brief


def format_advisor_brief(brief: Dict[str, Any]) -> str:
    risk = brief.get("risk") or {}
    policy = risk.get("advisor_policy") or {}
    audit = brief.get("audit") or {}
    events = brief.get("events") or {}
    decision = brief.get("decision") or {}
    intraday = brief.get("intraday") or {}
    overall = str(brief.get("overall_action_level") or "observe")
    lines = [
        "🧭 OpenClaw 投顾总览",
        f"生成时间：{brief.get('generated_at')}",
        "",
        "**当前结论**",
        f"- 当前最高行动层级：**{ACTION_LABELS.get(overall, '观察')}**。只有证据完整的持仓才会进入“准备”，本报告不会自动下单。",
    ]
    blocked = audit.get("blocked") or []
    if blocked:
        lines.append(f"- 系统能力仍有 {len(blocked)} 项阻断：{join_cn(blocked)}；相关数据按降级口径处理。")
    else:
        lines.append("- 最近能力审计没有阻断项；行情和账户结论仍以各自数据时间为准。")
    policy_status = {"system_default": "系统默认", "user_confirmed": "用户已确认"}.get(
        str(policy.get("profile_status") or "system_default"),
        "自定义",
    )
    lines.append(
        f"- 风险政策：单票 {pct(float(policy.get('single_position_alert_ratio', 0.30)) * 100)} 预警 / "
        f"{pct(float(policy.get('single_position_reduce_target_ratio', 0.25)) * 100)} 降风险目标，"
        f"前三持仓 {pct(float(policy.get('top3_position_alert_ratio', 0.70)) * 100)} 预警，"
        f"最低现金参考 {pct(float(policy.get('minimum_cash_ratio', 0.03)) * 100)}（{policy_status}）。"
    )

    lines.extend(["", "**账户与组合风险**"])
    if risk.get("available"):
        lines.append(
            f"- 数据日 {risk.get('as_of') or '未知'}；已知 {int(risk.get('positions_count', 0) or 0)} 条持仓明细，"
            f"明细市值 {money(risk.get('total_market_value'))}，浮动盈亏 {money(risk.get('total_unrealized_pnl'))}。"
        )
        if risk.get("cash_complete"):
            lines.append(f"- 现金 {money(risk.get('cash'))}（{pct(float(risk.get('cash_ratio') or 0) * 100)}）。")
        else:
            lines.append(f"- 可验证现金 {money(risk.get('cash'))}；部分账户不可验证，不计算完整现金占比。")
        concentration_prefix = "" if risk.get("position_coverage_complete", True) else "按已知明细计算，"
        lines.append(
            f"- {concentration_prefix}第一大持仓 {pct(float(risk.get('top1_ratio') or 0) * 100)}，"
            f"前三大持仓 {pct(float(risk.get('top3_ratio') or 0) * 100)}；"
            f"{join_cn((risk_label(flag) for flag in risk.get('risk_flags') or []), '未发现重大结构风险')}。"
        )
        if not risk.get("position_coverage_complete", True):
            lines.append(
                f"- 账户资产口径市值比持仓明细多 {money(risk.get('position_market_value_gap'))}；"
                "当前持仓数量和集中度不是完整组合结论。"
            )
    else:
        lines.append("- 持仓风险快照不可用，本次不输出账户或仓位结论。")

    lines.extend(["", "**值得关注的事件**"])
    if events.get("events"):
        for item in events.get("events") or []:
            lines.append(f"- **{item.get('title')}**｜{join_cn(item.get('themes') or [], '主题待核验')}")
        lines.append(f"- 事件来源于最近收盘简报，数据日 {events.get('as_of') or '未知'}；开盘后仍需验证板块和量价。")
    else:
        lines.append("- 最近收盘简报没有通过质量门槛的事件，不为凑数生成主题。")

    lines.extend(["", "**下一步行动**"])
    for item in brief.get("actions") or []:
        lines.append(f"- [{ACTION_LABELS.get(item.get('level'), '观察')}] {item.get('text')}")

    lines.extend(["", "**验证闭环**"])
    if int(intraday.get("total", 0) or 0):
        lines.append(
            f"- 最近 7 日完成 {intraday.get('total')} 次日内方向验证：精确正确率 {intraday.get('exact_rate')}%，"
            f"含接近结果可用率 {intraday.get('usable_rate')}%。"
        )
        lines.append("- 这些历史样本只评价日内方向；未完成策略归因的样本不进入权重更新。")
    else:
        lines.append("- 最近 7 日暂无可用的日内方向验证，不输出命中率。")
    if decision.get("fresh"):
        lines.append(f"- 盘中决策快照：{decision.get('generated_at')}，可用于当前行动分层。")
    elif decision.get("available"):
        lines.append(f"- 最近盘中决策快照为 {decision.get('generated_at') or '时间未知'}，已过当前时效，仅作历史记录。")
    else:
        lines.append("- 尚无盘中决策快照；进入交易时段后再生成逐仓动作。")

    lines.extend(
        [
            "",
            "**边界**",
            "- 观察＝等待证据；核验＝补齐行情、账户或数量；准备＝可形成数量参考，仍需人工确认。",
            "- 数据不可用、过期或账户不完整时，系统不会补齐现金、成交或实时持仓结论。",
        ]
    )
    return "\n".join(lines)

#!/usr/bin/env python3
"""Long-term portfolio aggregation service (read-only from trading snapshot)."""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Dict, Optional


DEFAULT_LONGTERM_SNAPSHOT_PATH = (
    Path(os.environ.get("LONGTERM_SNAPSHOT_PATH", ""))
    if os.environ.get("LONGTERM_SNAPSHOT_PATH")
    else Path("/root/.openclaw/workspace/trading/trading_data/longterm/investor_longterm_snapshot.json")
)


def load_longterm_snapshot(path: Optional[str] = None) -> Optional[Dict]:
    target = Path(path).expanduser().resolve() if path else DEFAULT_LONGTERM_SNAPSHOT_PATH
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    payload.setdefault("_snapshot_path", str(target))
    return payload


def summarize_longterm_snapshot(snapshot: Optional[Dict]) -> Dict:
    if not snapshot:
        return {
            "available": False,
            "as_of": "",
            "updated_at": "",
            "nav": 0.0,
            "cash": 0.0,
            "cash_ratio": 0.0,
            "holdings_count": 0,
            "actions_count": 0,
            "execution_matched": False,
            "completed_actions_count": 0,
            "unfilled_actions_count": None,
            "rejected_actions_count": 0,
            "top_positions": [],
            "rejected_reason_summary": [],
            "snapshot_path": str(DEFAULT_LONGTERM_SNAPSHOT_PATH),
        }

    portfolio = snapshot.get("portfolio", {}) or {}
    latest_plan = snapshot.get("latest_plan", {}) or {}
    execution = snapshot.get("execution_monitor", {}) or {}
    actions_count = int(latest_plan.get("actions_count", 0) or 0)
    execution_matched = bool(
        latest_plan.get("trade_date")
        and str(execution.get("latest_plan_trade_date") or "") == str(latest_plan.get("trade_date") or "")
    )
    unfilled = int(execution.get("unfilled_plan_actions_count", 0) or 0) if execution_matched else None
    return {
        "available": True,
        "as_of": str(snapshot.get("as_of", "") or ""),
        "updated_at": str(snapshot.get("updated_at", "") or ""),
        "nav": float(portfolio.get("nav", 0) or 0),
        "cash": float(portfolio.get("cash", 0) or 0),
        "cash_ratio": float(portfolio.get("cash_ratio", 0) or 0),
        "holdings_count": int(portfolio.get("holdings_count", 0) or 0),
        "actions_count": actions_count,
        "execution_matched": execution_matched,
        "completed_actions_count": max(0, actions_count - int(unfilled or 0)) if execution_matched else 0,
        "unfilled_actions_count": unfilled,
        "rejected_actions_count": int(latest_plan.get("rejected_actions_count", 0) or 0),
        "top_positions": list(portfolio.get("top_positions", []) or []),
        "rejected_reason_summary": list(latest_plan.get("rejected_reason_summary", []) or []),
        "snapshot_path": str(snapshot.get("_snapshot_path", DEFAULT_LONGTERM_SNAPSHOT_PATH)),
    }


def longterm_snapshot_freshness(as_of: str, *, today: Optional[date] = None) -> str:
    raw = str(as_of or "").strip()[:10]
    if not raw:
        return "快照日期未知，不能视为当前组合状态。"
    try:
        snapshot_day = date.fromisoformat(raw)
    except ValueError:
        return "快照日期格式异常，不能视为当前组合状态。"
    current_day = today or date.today()
    age_days = (current_day - snapshot_day).days
    if age_days == 0:
        return ""
    if age_days > 0:
        return f"该快照距今 {age_days} 天，仅代表 {raw} 的模拟组合状态。"
    return f"快照日期晚于当前日期 {abs(age_days)} 天，日期口径冲突，仅作排障参考。"


def build_longterm_snapshot_text(summary: Dict) -> str:
    if not summary.get("available"):
        return "暂无可用的长线模拟组合快照，暂时无法判断组合状态。"
    as_of = str(summary.get("as_of") or "未知")
    nav = float(summary.get("nav", 0) or 0)
    cash = float(summary.get("cash", 0) or 0)
    cash_ratio = float(summary.get("cash_ratio", 0) or 0)
    holdings_count = int(summary.get("holdings_count", 0) or 0)
    rejected_count = int(summary.get("rejected_actions_count", 0) or 0)
    line = (
        f"数据日 {as_of}；净值 {nav:,.2f} 元；现金 {cash:,.2f} 元"
        f"（{cash_ratio:.1%}）；持仓 {holdings_count} 只；"
        f"{longterm_plan_status(summary)}，风控拒绝 {rejected_count} 笔。"
    )
    top_positions = summary.get("top_positions", []) or []
    if top_positions:
        top_text = "、".join(
            f"{item.get('name') or item.get('code', '')}（{float(item.get('weight', 0) or 0):.1%}）"
            for item in top_positions[:5]
        )
        line += f"主要持仓：{top_text}。"
    elif holdings_count == 0:
        line += "当前为空仓模拟状态。"
    freshness = longterm_snapshot_freshness(as_of)
    if freshness:
        line += freshness
    return line


def longterm_plan_status(summary: Dict) -> str:
    actions = int(summary.get("actions_count", 0) or 0)
    if actions <= 0:
        return "最近计划无调整"
    if summary.get("execution_matched"):
        completed = int(summary.get("completed_actions_count", 0) or 0)
        unfilled = int(summary.get("unfilled_actions_count", 0) or 0)
        if unfilled == 0:
            return f"最近计划 {actions} 笔，均已回报完成"
        return f"最近计划 {actions} 笔，已完成 {completed} 笔、尚未完成 {unfilled} 笔"
    return f"最近计划 {actions} 笔，执行回报待核验"


def longterm_reason_cn(reason: object) -> str:
    """Translate internal rebalance rejection codes for user-facing reports."""
    text = str(reason or "").strip()
    mappings = (
        ("not_in_target_universe", "不在目标股票池"),
        ("min_trade_amount", "低于最小交易金额"),
        ("cash_buffer_violation", "现金安全垫不足"),
        ("industry_cap_violation", "行业集中度超限"),
        ("theme_cap_violation", "主题集中度超限"),
        ("risk_budget_volatility_violation", "组合波动预算超限"),
        ("risk_budget_drawdown_violation", "组合回撤预算超限"),
    )
    for prefix, label in mappings:
        if text.startswith(prefix):
            details = re.search(r"\((.+)\)$", text)
            return f"{label}（{details.group(1)}）" if details else label
    return "其他风控限制" if text else "原因未说明"


def format_longterm_rejected_reasons(summary: Dict, limit: int = 6) -> str:
    rows = summary.get("rejected_reason_summary", []) or []
    return "、".join(
        f"{longterm_reason_cn(item.get('reason'))} {int(item.get('count', 0) or 0)} 笔"
        for item in rows[: max(1, int(limit))]
    )

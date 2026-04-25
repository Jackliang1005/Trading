#!/usr/bin/env python3
"""Assistant-facing service facade for analyze/dashboard/feedback flows."""

from __future__ import annotations

from datetime import datetime
from typing import Dict

import db
from domain.repository import get_analysis_context_repository
from domain.services.analysis_context_service import (
    normalize_analysis_context_summary,
    normalize_trade_decision_summary,
)
from domain.services.evolution_service import generate_system_prompt, load_strategy_config
from domain.services.prediction_prompt_service import build_market_context_text
from domain.services.prediction_service import load_prediction_snapshot_data
from knowledge_base import build_few_shot_prompt, build_rag_context
from live_monitor.collectors.trade_decision_collector import collect_trade_decisions


def analyze(query: str, model: str = "", session_id: str = "") -> Dict:
    """Core analysis entry used by agent bridge and compatibility main.py."""
    db.init_db()

    rag_context = build_rag_context(query)
    few_shot = build_few_shot_prompt()
    system_prompt = generate_system_prompt()

    snapshot_data = load_prediction_snapshot_data()
    market_context = build_market_context_text(snapshot_data) if snapshot_data else ""
    market_data_source = str(snapshot_data.get("_source", "none")) if snapshot_data else "none"

    context_repo = get_analysis_context_repository()
    analysis_context_summary = normalize_analysis_context_summary(context_repo.summarize_bundle())

    trade_decisions = collect_trade_decisions()
    trade_decision_summary = normalize_trade_decision_summary(
        (trade_decisions.get("summary", {}) or {}),
        log_date=(trade_decisions.get("system_log", {}) or {}).get("log_date", ""),
    )
    trade_decision_focus = {
        "log_date": (trade_decisions.get("system_log", {}) or {}).get("log_date", ""),
        "strategy": (trade_decisions.get("system_log", {}) or {}).get("strategy", ""),
        "final_candidates": ((trade_decisions.get("system_log", {}) or {}).get("final_candidates", []) or [])[:10],
        "submitted_buys": ((trade_decisions.get("system_log", {}) or {}).get("submitted_buys", []) or [])[:10],
        "filled_buys": ((trade_decisions.get("system_log", {}) or {}).get("filled_buys", []) or [])[:10],
        "watchlists": ((trade_decisions.get("watchlists", {}) or {}).get("entries", []) or [])[:10],
    }

    trade_context_text = (
        "## 交易决策上下文\n"
        f"- log_date: {trade_decision_summary.get('log_date', '') or 'N/A'}\n"
        f"- strategy: {trade_decision_summary.get('strategy', '') or 'N/A'}\n"
        f"- candidates: {trade_decision_summary.get('final_candidate_count', 0)}\n"
        f"- submitted: {trade_decision_summary.get('submitted_buy_count', 0)}\n"
        f"- filled: {trade_decision_summary.get('filled_buy_count', 0)}\n"
        f"- watchlists: {trade_decision_summary.get('watchlist_count', 0)}"
    )

    full_context = f"""
{system_prompt}

{rag_context}

{few_shot}

{market_context}

{trade_context_text}
""".strip()

    return {
        "system_prompt": system_prompt,
        "rag_context": rag_context,
        "few_shot": few_shot,
        "market_context": market_context,
        "market_data_source": market_data_source,
        "analysis_context_summary": analysis_context_summary,
        "trade_decision_summary": trade_decision_summary,
        "trade_decision_focus": trade_decision_focus,
        "full_context": full_context,
        "query": query,
        "model": model,
        "session_id": session_id,
    }


def record_prediction(
    target: str,
    direction: str,
    confidence: float,
    reasoning: str,
    strategy: str = "technical",
    model: str = "",
    predicted_change: float = None,
    target_name: str = "",
) -> int:
    """Record one prediction with current market price snapshot."""
    from data_collector import fetch_market_quotes

    current_price = None
    quotes = fetch_market_quotes(target)
    if quotes and not quotes[0].get("error"):
        current_price = quotes[0].get("price")

    pid = db.add_prediction(
        target=target,
        direction=direction,
        confidence=confidence,
        reasoning=reasoning,
        strategy_used=strategy,
        model_used=model,
        predicted_change=predicted_change,
        actual_price=current_price,
        target_name=target_name,
    )
    print(f"📝 预测已记录 [ID:{pid}] {target} {direction} (置信度:{confidence:.0%})")
    return pid


def record_feedback(action: str, prediction_id: int = None, reason: str = "", comment: str = "") -> None:
    """Record user feedback for a prediction."""
    db.add_feedback(action, prediction_id, "", reason, comment)
    print(f"📝 反馈已记录: {action}")


def dashboard() -> str:
    """Render status dashboard text."""
    config = load_strategy_config()
    strategies = db.get_strategies()
    rules = db.get_rules()
    overall = db.get_overall_stats()
    snapshot_data = load_prediction_snapshot_data()
    market_source = snapshot_data.get("_source", "") if snapshot_data else ""

    lines = [
        "# 🦞 OpenClaw Investor 状态看板",
        f"**更新时间：** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 📊 整体表现",
        f"- 总预测: {overall.get('total', 0) or 0}",
        f"- 正确: {overall.get('correct', 0) or 0}",
        f"- 胜率: {overall.get('win_rate', 0) or 0}%",
        f"- 平均分: {overall.get('avg_score', 0) or 0}",
        "",
        "## ⚖️ 策略权重",
    ]

    for item in strategies:
        weight = config.get("weights", {}).get(item["name"], item["weight"])
        lines.append(
            f"- **{item['name']}**: 权重 {weight:.0%} | 胜率 {item.get('win_rate', 0):.1f}% | "
            f"预测 {item.get('total_predictions', 0)} 次"
        )

    lines.append(f"\n## 📏 活跃规则: {len([rule for rule in rules if rule.get('enabled')])} 条")
    for rule in rules[:5]:
        lines.append(f"- {rule['rule_text']} (置信度: {rule.get('confidence', 0):.0%})")
    if len(rules) > 5:
        lines.append(f"  ... 共 {len(rules)} 条")

    lines.append("\n## 📡 最新数据快照")
    if snapshot_data:
        if market_source == "research_packets":
            lines.append(f"- 数据源: research_packets (hits={snapshot_data.get('_packet_hits', 0)})")
        else:
            lines.append(f"- 采集时间: {snapshot_data.get('_captured_at', '?')}")
        for quote in snapshot_data.get("quotes", [])[:4]:
            if not quote.get("error"):
                lines.append(
                    f"- {quote.get('name', '?')}: {quote.get('price', '?')} ({quote.get('change_percent', '?')}%)"
                )
        trading_summary = snapshot_data.get("qmt_trading_summary", {}) or {}
        if trading_summary:
            lines.append(
                f"- 持仓: {trading_summary.get('positions_count', 0)} 只 | "
                f"成交: {trading_summary.get('today_trade_count', 0)} 笔 | "
                f"未实现盈亏: {trading_summary.get('total_unrealized_pnl', 0)}"
            )
    else:
        lines.append("- 暂无数据，请先运行数据采集")

    return "\n".join(lines)

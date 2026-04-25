#!/usr/bin/env python3
"""Agent bridge for context-first analysis workflows."""

from __future__ import annotations

from typing import Dict

import db
from domain.services.analysis_context_service import (
    normalize_analysis_context_summary,
    normalize_trade_decision_summary,
)
from domain.services.assistant_service import analyze


def build_analysis_payload(query: str, model: str = "", session_id: str = "") -> Dict:
    """Build structured analysis payload for agent-side usage."""
    db.init_db()
    return analyze(query=query, model=model, session_id=session_id)


def build_context_markdown(query: str, model: str = "", session_id: str = "") -> str:
    """Build markdown context for agent prompt injection."""
    payload = build_analysis_payload(query=query, model=model, session_id=session_id)
    summary = normalize_analysis_context_summary(payload.get("analysis_context_summary", {}) or {})
    trade_summary = normalize_trade_decision_summary(payload.get("trade_decision_summary", {}) or {})
    trade_focus = payload.get("trade_decision_focus", {}) or {}
    packet_types = ", ".join(summary.get("packet_types", []) or [])
    packet_types = packet_types or "none"
    final_candidates = trade_focus.get("final_candidates", []) or []
    filled_buys = trade_focus.get("filled_buys", []) or []
    return (
        "## 🦞 OpenClaw Investor 增强上下文\n\n"
        f"### 分析上下文摘要\n"
        f"- 数据源: {payload.get('market_data_source', 'unknown')}\n"
        f"- packets: {summary.get('packet_hits', 0)} ({packet_types})\n"
        f"- quotes: {summary.get('quote_count', 0)}\n"
        f"- positions: {summary.get('positions_count', 0)}\n"
        f"- trades(today): {summary.get('today_trade_count', 0)}\n\n"
        "### 交易决策摘要\n"
        f"- log_date: {trade_summary.get('log_date', '') or 'N/A'}\n"
        f"- strategy: {trade_summary.get('strategy', '') or 'N/A'}\n"
        f"- candidates: {trade_summary.get('final_candidate_count', 0)}"
        f" ({', '.join(final_candidates) if final_candidates else '无'})\n"
        f"- submitted: {trade_summary.get('submitted_buy_count', 0)}\n"
        f"- filled: {trade_summary.get('filled_buy_count', 0)}"
        f" ({', '.join(str(code) for code in filled_buys) if filled_buys else '无'})\n"
        f"- watchlists: {trade_summary.get('watchlist_count', 0)}\n\n"
        f"{payload.get('market_context', '').strip()}\n\n"
        f"{payload.get('rag_context', '').strip()}\n\n"
        f"{payload.get('few_shot', '').strip()}"
    ).strip()

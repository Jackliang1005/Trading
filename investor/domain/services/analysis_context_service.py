#!/usr/bin/env python3
"""Shared analysis context summary helpers."""

from __future__ import annotations

from typing import Dict


ANALYSIS_CONTEXT_DEFAULTS = {
    "as_of_date": "",
    "packet_hits": 0,
    "packet_types": [],
    "has_portfolio_snapshot": False,
    "quote_count": 0,
    "has_flow": False,
    "has_market_regime": False,
    "positions_count": 0,
    "today_trade_count": 0,
    "today_order_count": 0,
    "total_unrealized_pnl": 0,
}


TRADE_DECISION_SUMMARY_DEFAULTS = {
    "log_date": "",
    "strategy": "",
    "signal_count": 0,
    "final_candidate_count": 0,
    "submitted_buy_count": 0,
    "filled_buy_count": 0,
    "watchlist_count": 0,
}


def normalize_analysis_context_summary(summary: Dict, as_of_date: str = "") -> Dict:
    payload = dict(ANALYSIS_CONTEXT_DEFAULTS)
    payload.update(summary or {})
    if as_of_date and not payload.get("as_of_date"):
        payload["as_of_date"] = as_of_date
    return payload


def normalize_trade_decision_summary(summary: Dict, log_date: str = "") -> Dict:
    payload = dict(TRADE_DECISION_SUMMARY_DEFAULTS)
    for key in payload:
        if key in (summary or {}):
            payload[key] = (summary or {}).get(key)
    if log_date and not payload.get("log_date"):
        payload["log_date"] = log_date
    return payload

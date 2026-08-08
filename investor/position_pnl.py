#!/usr/bin/env python3
"""Resolve cumulative position P&L from heterogeneous broker fields."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple


def _number(item: Dict[str, Any], keys: Iterable[str]) -> Tuple[float | None, str]:
    for key in keys:
        if key not in item or item.get(key) is None:
            continue
        try:
            return float(item.get(key)), key
        except (TypeError, ValueError):
            continue
    return None, ""


def resolve_position_pnl(item: Dict[str, Any]) -> Dict[str, Any]:
    """Return cumulative unrealized P&L, its basis, and consistency evidence."""
    market_value, _ = _number(item, ("market_value", "m_dMarketValue"))
    volume, _ = _number(item, ("volume", "m_nVolume", "total_volume", "current_volume"))
    average_price, _ = _number(item, ("avg_price", "m_dAvgPrice", "cost_price", "open_price", "m_dOpenPrice"))
    cumulative, cumulative_key = _number(item, ("position_profit", "m_dPositionProfit", "profit_loss"))
    normalized, normalized_key = _number(item, ("unrealized_pnl",))
    floating, floating_key = _number(item, ("float_profit", "m_dFloatProfit"))

    derived = None
    cost_value = None
    if market_value is not None and volume is not None and volume > 0 and average_price is not None and average_price > 0:
        cost_value = average_price * volume
        derived = market_value - cost_value

    tolerance = max(5.0, abs(market_value or 0.0) * 0.005)
    cumulative_cost_conflict = bool(derived is not None and cumulative is not None and abs(cumulative - derived) > tolerance)
    if cumulative_cost_conflict:
        pnl = min(float(cumulative), float(derived))
        basis = "conservative_conflict_min"
    elif cumulative is not None:
        pnl = cumulative
        basis = cumulative_key
    elif derived is not None:
        pnl = derived
        basis = "market_value_minus_cost"
    elif normalized is not None:
        pnl = normalized
        basis = normalized_key
    elif floating is not None:
        pnl = floating
        basis = f"{floating_key}_fallback"
    else:
        pnl = 0.0
        basis = "missing"

    if cost_value is None and market_value is not None:
        inferred_cost = market_value - pnl
        cost_value = inferred_cost if inferred_cost > 0 else None
    pnl_pct = (pnl / cost_value * 100.0) if cost_value and cost_value > 0 else None
    return {
        "pnl": round(pnl, 4),
        "pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
        "basis": basis,
        "cost_value": round(cost_value, 4) if cost_value is not None else None,
        "derived_pnl": round(derived, 4) if derived is not None else None,
        "daily_float_profit": floating,
        "daily_float_basis": floating_key,
        "cumulative_cost_conflict": cumulative_cost_conflict,
    }

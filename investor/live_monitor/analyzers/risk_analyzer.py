#!/usr/bin/env python3
"""Trading risk analyzer for live monitor.

Monitors position concentration, sector exposure, drawdown,
and single-stock adverse movements.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

# -- risk thresholds --
SINGLE_POSITION_WARN_RATIO = 0.20   # >20% of total asset → P1
SINGLE_POSITION_CRIT_RATIO = 0.30  # >30% of total asset → P0
SECTOR_CONCENTRATION_WARN_RATIO = 0.40  # single sector >40% of holdings → P1
INTRADAY_DRAWDOWN_WARN_PCT = 3.0   # >3% intraday drawdown → P1
INTRADAY_DRAWDOWN_CRIT_PCT = 5.0   # >5% intraday drawdown → P0
STOCK_ADVERSE_MOVE_PCT = 5.0       # single stock ±5% move → P1
LOW_FILL_RATE_WARN = 0.50          # <50% fill rate with submissions → P2


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def analyze_position_concentration(
    positions: Sequence[Dict],
    total_asset: float,
) -> List[Dict]:
    """Check single-position concentration vs total asset."""
    if not total_asset or total_asset <= 0:
        return []
    incidents = []
    for pos in positions:
        code = str(pos.get("stock_code", pos.get("code", "")) or "")
        name = str(pos.get("stock_name", pos.get("name", "")) or "")
        mv = _safe_float(pos.get("market_value", 0))
        if not mv or not code:
            continue
        ratio = mv / total_asset
        if ratio > SINGLE_POSITION_CRIT_RATIO:
            incidents.append(
                {
                    "severity": "P0",
                    "kind": "position_concentration_critical",
                    "signature": f"position_concentration::{code}",
                    "summary": f"{code}{' ' + name if name else ''} 仓位集中度过高 "
                    f"({ratio:.1%} > {SINGLE_POSITION_CRIT_RATIO:.0%})",
                    "evidence": {
                        "code": code,
                        "name": name,
                        "market_value": mv,
                        "total_asset": total_asset,
                        "concentration_ratio": round(ratio, 4),
                    },
                }
            )
        elif ratio > SINGLE_POSITION_WARN_RATIO:
            incidents.append(
                {
                    "severity": "P1",
                    "kind": "position_concentration_warning",
                    "signature": f"position_concentration::{code}",
                    "summary": f"{code}{' ' + name if name else ''} 仓位集中度偏高 "
                    f"({ratio:.1%} > {SINGLE_POSITION_WARN_RATIO:.0%})",
                    "evidence": {
                        "code": code,
                        "name": name,
                        "market_value": mv,
                        "total_asset": total_asset,
                        "concentration_ratio": round(ratio, 4),
                    },
                }
            )
    return incidents


def analyze_sector_concentration(
    positions: Sequence[Dict],
    sector_info: Dict[str, str] | None = None,
) -> List[Dict]:
    """Check sector/industry concentration in holdings."""
    if not positions:
        return []

    # Attempt to tag positions with sectors from available data
    sector_mv: Dict[str, float] = {}
    sector_stocks: Dict[str, List[str]] = {}
    total_mv = 0.0
    untagged_mv = 0.0

    for pos in positions:
        code = str(pos.get("stock_code", pos.get("code", "")) or "")
        mv = _safe_float(pos.get("market_value", 0))
        if not mv:
            continue
        total_mv += mv
        # Try to get sector from position metadata
        sector = str(
            pos.get("sector")
            or pos.get("industry")
            or pos.get("sector_name")
            or (sector_info or {}).get(code, "")
            or ""
        ).strip()

        if sector and sector != "未知":
            sector_mv[sector] = sector_mv.get(sector, 0) + mv
            sector_stocks.setdefault(sector, []).append(code)
        else:
            untagged_mv += mv

    if not total_mv:
        return []

    incidents = []
    for sector, mv in sorted(sector_mv.items(), key=lambda x: -x[1]):
        ratio = mv / total_mv
        if ratio > SECTOR_CONCENTRATION_WARN_RATIO:
            stocks_str = ",".join(sector_stocks.get(sector, [])[:6])
            incidents.append(
                {
                    "severity": "P1",
                    "kind": "sector_concentration",
                    "signature": f"sector_concentration::{sector}",
                    "summary": f"板块 '{sector}' 持仓集中度 {ratio:.1%} (> {SECTOR_CONCENTRATION_WARN_RATIO:.0%})，"
                    f"涉及: {stocks_str}",
                    "evidence": {
                        "sector": sector,
                        "market_value": round(mv, 2),
                        "total_market_value": round(total_mv, 2),
                        "concentration_ratio": round(ratio, 4),
                        "stocks": sector_stocks.get(sector, []),
                    },
                }
            )

    # If >50% untagged, report as data gap
    if total_mv > 0 and untagged_mv / total_mv > 0.5:
        incidents.append(
            {
                "severity": "P2",
                "kind": "sector_data_gap",
                "signature": "sector_data_gap",
                "summary": f"超过50%持仓市值({untagged_mv/total_mv:.0%})无法识别行业板块，"
                "板块集中度监控不完整",
                "evidence": {
                    "untagged_market_value": round(untagged_mv, 2),
                    "total_market_value": round(total_mv, 2),
                    "untagged_ratio": round(untagged_mv / total_mv, 4),
                },
            }
        )

    return incidents


def analyze_intraday_drawdown(
    positions: Sequence[Dict],
    previous_total_value: float | None = None,
) -> List[Dict]:
    """Detect significant intraday drawdown from peak."""
    if previous_total_value is None or previous_total_value <= 0:
        return []

    current_value = sum(
        _safe_float(pos.get("market_value", 0))
        + _safe_float(pos.get("unrealized_pnl", pos.get("profit_loss", 0)))
        for pos in (positions or [])
    )

    if current_value <= 0:
        return []

    drawdown_pct = (previous_total_value - current_value) / previous_total_value * 100

    incidents = []
    if drawdown_pct > INTRADAY_DRAWDOWN_CRIT_PCT:
        incidents.append(
            {
                "severity": "P0",
                "kind": "intraday_drawdown_critical",
                "signature": "intraday_drawdown",
                "summary": f"日内回撤 {drawdown_pct:.1f}% (>{INTRADAY_DRAWDOWN_CRIT_PCT}%)，"
                f"从 {previous_total_value:,.0f} 到 {current_value:,.0f}",
                "evidence": {
                    "previous_value": round(previous_total_value, 2),
                    "current_value": round(current_value, 2),
                    "drawdown_pct": round(drawdown_pct, 2),
                },
            }
        )
    elif drawdown_pct > INTRADAY_DRAWDOWN_WARN_PCT:
        incidents.append(
            {
                "severity": "P1",
                "kind": "intraday_drawdown_warning",
                "signature": "intraday_drawdown",
                "summary": f"日内回撤 {drawdown_pct:.1f}% (>{INTRADAY_DRAWDOWN_WARN_PCT}%)，"
                f"从 {previous_total_value:,.0f} 到 {current_value:,.0f}",
                "evidence": {
                    "previous_value": round(previous_total_value, 2),
                    "current_value": round(current_value, 2),
                    "drawdown_pct": round(drawdown_pct, 2),
                },
            }
        )

    return incidents


def analyze_stock_moves(
    positions: Sequence[Dict],
    quotes: Sequence[Dict] | None = None,
) -> List[Dict]:
    """Detect unusually large single-stock moves in held positions."""
    if not positions:
        return []

    incidents = []
    quote_map = {}
    if quotes:
        for q in quotes:
            code = str(q.get("code", q.get("stock_code", "")) or "")
            if code:
                quote_map[code] = q

    for pos in positions:
        code = str(pos.get("stock_code", pos.get("code", "")) or "")
        name = str(pos.get("stock_name", pos.get("name", "")) or "")

        # Check position's own change_pct field first
        change_pct = _safe_float(
            pos.get("change_percent")
            or pos.get("change_pct")
            or pos.get("profit_loss_ratio")
        )
        # Fallback to quote data
        if not change_pct and code in quote_map:
            change_pct = _safe_float(quote_map[code].get("change_percent", 0))

        if abs(change_pct) >= STOCK_ADVERSE_MOVE_PCT:
            direction = "上涨" if change_pct > 0 else "下跌"
            incidents.append(
                {
                    "severity": "P1",
                    "kind": "stock_adverse_move",
                    "signature": f"stock_move::{code}",
                    "summary": f"持仓 {code}{' ' + name if name else ''} 日内{direction} "
                    f"{abs(change_pct):.1f}% (>{STOCK_ADVERSE_MOVE_PCT}%)",
                    "evidence": {
                        "code": code,
                        "name": name,
                        "change_pct": round(change_pct, 2),
                        "market_value": _safe_float(pos.get("market_value", 0)),
                    },
                }
            )

    return incidents


def analyze_execution_quality(
    submitted_count: int,
    filled_count: int,
    skipped_count: int,
    skipped_reasons: Sequence[Dict] | None = None,
) -> List[Dict]:
    """Check execution quality: fill rate and skip patterns."""
    incidents = []

    if submitted_count > 0:
        fill_rate = filled_count / submitted_count
        if fill_rate < LOW_FILL_RATE_WARN:
            incidents.append(
                {
                    "severity": "P2",
                    "kind": "low_fill_rate",
                    "signature": "low_fill_rate",
                    "summary": f"买入成交率偏低: {filled_count}/{submitted_count} "
                    f"({fill_rate:.0%} < {LOW_FILL_RATE_WARN:.0%})",
                    "evidence": {
                        "submitted_count": submitted_count,
                        "filled_count": filled_count,
                        "fill_rate": round(fill_rate, 4),
                        "skipped_count": skipped_count,
                    },
                }
            )

    # Check for dominant skip reason (single reason > 50% of skips)
    if skipped_reasons and skipped_count >= 3:
        for item in skipped_reasons[:5]:
            reason = str(item.get("reason", "") or "")
            count = int(item.get("count", 0) or 0)
            if count / skipped_count > 0.5:
                incidents.append(
                    {
                        "severity": "P2",
                        "kind": "dominant_skip_reason",
                        "signature": f"skip_reason::{reason}",
                        "summary": f"过滤原因 '{reason}' 占比 {count}/{skipped_count} "
                        f"({count/skipped_count:.0%})，建议审查过滤规则",
                        "evidence": {
                            "reason": reason,
                            "count": count,
                            "total_skipped": skipped_count,
                            "ratio": round(count / skipped_count, 4),
                        },
                    }
                )

    return incidents


def analyze_risk(
    qmt_trade_state: Dict | None = None,
    trade_decisions: Dict | None = None,
    previous_total_value: float | None = None,
) -> List[Dict]:
    """Main entry point: run all risk checks and return incidents.

    Parameters
    ----------
    qmt_trade_state : dict
        Output from qmt_trade_state_collector (servers list with positions).
    trade_decisions : dict
        Output from trade_decision_collector (candidates, submitted, filled, skipped).
    previous_total_value : float, optional
        Previous total portfolio value for drawdown comparison.

    Returns
    -------
    list of incident dicts
    """
    incidents: List[Dict] = []

    # Extract positions from qmt trade state
    all_positions: List[Dict] = []
    total_asset = 0.0
    if qmt_trade_state:
        for server_item in qmt_trade_state.get("servers", []) or []:
            summary = server_item.get("summary", {}) or {}
            asset = summary.get("asset", {}) or {}
            positions_data = summary.get("positions", {}) or {}
            server_total = _safe_float(asset.get("total_asset", 0))
            if server_total > total_asset:
                total_asset = server_total
            for pos in positions_data.get("items", positions_data.get("data", [])) or []:
                if isinstance(pos, dict):
                    all_positions.append(pos)

    # Position concentration
    if all_positions and total_asset:
        incidents.extend(analyze_position_concentration(all_positions, total_asset))

    # Sector concentration
    if all_positions:
        incidents.extend(analyze_sector_concentration(all_positions))

    # Intraday drawdown
    if all_positions:
        incidents.extend(analyze_intraday_drawdown(all_positions, previous_total_value))

    # Stock adverse moves
    if all_positions:
        incidents.extend(analyze_stock_moves(all_positions))

    # Execution quality
    if trade_decisions:
        decision_log = trade_decisions.get("system_log", {}) or {}
        submitted = len(decision_log.get("submitted_buys", []) or [])
        filled = len(decision_log.get("filled_buys", []) or [])
        skipped_items = decision_log.get("skipped_buys", []) or []
        reconciliation = trade_decisions.get("_reconciliation", {}) or {}
        reason_summary = reconciliation.get("skipped_reason_summary", {}).get("overall", []) or []

        if submitted or skipped_items:
            incidents.extend(
                analyze_execution_quality(
                    submitted,
                    filled,
                    len(skipped_items),
                    reason_summary,
                )
            )

    return incidents

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .models import (
    LongTermSettings,
    ManualExecutionItem,
    PortfolioState,
    RebalanceActionItem,
    RebalancePlan,
    RebalanceRejectedItem,
    SimPosition,
    StockCandidate,
)
from .industry_normalizer import extract_themes
from .industry_policy import industry_cap_for, theme_cap_for

logger = logging.getLogger(__name__)


def _normalize_weights(settings: LongTermSettings) -> Tuple[float, float, float, float]:
    values = [
        max(0.0, float(settings.score_value_weight)),
        max(0.0, float(settings.score_quality_weight)),
        max(0.0, float(settings.score_growth_weight)),
        max(0.0, float(settings.score_risk_weight)),
    ]
    total = sum(values)
    if total <= 0:
        return 0.30, 0.30, 0.25, 0.15
    return tuple(item / total for item in values)


def _score_candidate(item: StockCandidate, settings: LongTermSettings) -> float:
    value_w, quality_w, growth_w, risk_w = _normalize_weights(settings)
    score = (
        item.value_score * value_w
        + item.quality_score * quality_w
        + item.growth_score * growth_w
        + (100.0 - item.risk_score) * risk_w
    )
    return max(0.0, min(100.0, score))


def _risk_score_to_volatility_proxy(risk_score: float) -> float:
    clipped = max(0.0, min(100.0, float(risk_score)))
    # 8%~30% annualized volatility proxy.
    return 0.08 + (clipped / 100.0) * 0.22


def _risk_score_to_drawdown_proxy(risk_score: float) -> float:
    clipped = max(0.0, min(100.0, float(risk_score)))
    # 10%~38% max drawdown proxy.
    return 0.10 + (clipped / 100.0) * 0.28


def _portfolio_risk_budget(
    projected_values: Dict[str, float],
    nav: float,
    risk_score_map: Dict[str, float],
) -> Tuple[float, float]:
    if nav <= 0:
        return 0.0, 0.0
    weighted_vol = 0.0
    weighted_dd = 0.0
    for code, value in projected_values.items():
        weight = max(0.0, float(value)) / nav
        score = float(risk_score_map.get(code, 50.0))
        weighted_vol += weight * _risk_score_to_volatility_proxy(score)
        weighted_dd += weight * _risk_score_to_drawdown_proxy(score)
    return weighted_vol, weighted_dd


def _resolve_reference_price(
    code: str,
    quotes: Dict[str, Dict],
    position_price_map: Dict[str, float],
) -> float:
    quote = quotes.get(code, {}) or {}
    px = float(quote.get("price", 0) or 0)
    if px > 0:
        return px
    fallback = float(position_price_map.get(code, 0) or 0)
    if fallback > 0:
        return fallback
    return 0.0


def _safe_weight_map(candidates: List[StockCandidate], settings: LongTermSettings) -> Dict[str, float]:
    active = [item for item in candidates if item.status in ("active", "watch", "candidate")]
    if not active:
        return {}
    scored = sorted(active, key=lambda item: _score_candidate(item, settings), reverse=True)
    top = scored[: max(1, int(settings.max_holdings or 12))]
    raw_scores = {item.code: max(0.0, _score_candidate(item, settings)) for item in top}
    total = sum(raw_scores.values())
    if total <= 0:
        equal = 1.0 / max(1, len(top))
        return {item.code: equal for item in top}
    weights = {item.code: raw_scores.get(item.code, 0.0) / total for item in top}
    return weights


# ---------------------------------------------------------------------------
# Market trend detection
# ---------------------------------------------------------------------------


def _detect_market_trend(quotes: Dict[str, Dict], settings: LongTermSettings) -> Tuple[bool, int]:
    """Detect bull/bear market and return (is_bull, effective_holdings).

    Uses the configured market trend index (default CSI 500).
    Bull: index > MA60 and MA20 > MA60. Bear: otherwise.
    Returns (is_bull, effective_holdings).
    """
    market_idx = str(getattr(settings, "market_trend_index", "000905.XSHG") or "000905.XSHG")
    idx_data = quotes.get(market_idx, {})
    closes = list(idx_data.get("history_close", []) or [])

    if len(closes) >= 60:
        arr = np.array(closes[-60:], dtype=float)
        ma60 = float(np.mean(arr[-60:]))
        ma20 = float(np.mean(arr[-20:]))
        is_bull = arr[-1] > ma60 and ma20 > ma60
        effective_holdings = int(getattr(settings, "rotation_max_holdings", settings.max_holdings) or 5) if is_bull else int(settings.max_holdings_bear or 3)
        logger.info("Market trend: %s close=%.0f MA60=%.0f bull=%s holdings=%d",
                     market_idx, arr[-1], ma60, is_bull, effective_holdings)
        return is_bull, effective_holdings
    else:
        logger.warning("Insufficient history for market trend detection: %d points", len(closes))
        return True, int(getattr(settings, "rotation_max_holdings", settings.max_holdings) or 5)


# ---------------------------------------------------------------------------
# Heat Rotation helpers
# ---------------------------------------------------------------------------


def _hrs_weights(settings: LongTermSettings) -> Tuple[float, float, float, float]:
    ha = max(0.0, float(settings.hrs_heat_accel_w))
    sm = max(0.0, float(settings.hrs_sector_mom_w))
    pt = max(0.0, float(settings.hrs_price_trend_w))
    lq = max(0.0, float(settings.hrs_liquidity_w))
    total = ha + sm + pt + lq
    if total <= 0:
        return 0.35, 0.25, 0.20, 0.20
    return ha / total, sm / total, pt / total, lq / total


def _compute_hrs(scan_row: Dict, settings: LongTermSettings) -> float:
    """Compute Heat Rotation Score from scan row fields.

    When THS heat data is missing (heat_accel=0, sector_mom=0), uses
    neutral defaults (50) so stocks without THS coverage aren't penalized.
    """
    ha, sm, pt, lq = _hrs_weights(settings)
    heat_accel = float(scan_row.get("heat_accel", 0.0) or 0.0)
    sector_mom = float(scan_row.get("sector_momentum", 0.0) or 0.0)
    price_trend = float(scan_row.get("price_trend", 0.0) or 0.0)
    liquidity = float(scan_row.get("liquidity_score", 0.0) or 0.0)

    # If no THS/concept coverage, use neutral defaults instead of zero
    has_ths = float(scan_row.get("ths_heat_score", -1) or -1) >= 0
    if not has_ths or heat_accel == 0.0:
        heat_accel = 1.0  # neutral: 50 + 1.0*20 = 70 (slight positive bias for coverage gap)
    if sector_mom == 0.0:
        sector_mom = 50.0  # neutral

    # Scale heat_accel into 0-100 (typical range -1 to +2 → 25-100)
    heat_accel_scaled = max(0.0, min(100.0, 50.0 + heat_accel * 20.0))
    score = heat_accel_scaled * ha + sector_mom * sm + price_trend * pt + liquidity * lq
    return round(max(0.0, min(100.0, score)), 3)


def _extract_themes_from_scan(scan_row: Dict) -> List[str]:
    """Extract hot concept names from scan row."""
    concepts = list(scan_row.get("ths_hot_concepts", []) or [])
    return [str(c).strip() for c in concepts if str(c).strip()]


def _theme_strength(theme_name: str, all_scan_rows: List[Dict]) -> float:
    """Compute aggregate strength of a theme across all candidates."""
    total = 0.0
    count = 0
    for row in all_scan_rows:
        themes = _extract_themes_from_scan(row)
        if theme_name in themes:
            total += float(row.get("score", 0.0) or 0.0)
            count += 1
    return round(total / max(count, 1), 3)


def _theme_allocation(
    candidates: List[StockCandidate],
    scan_rows: List[Dict],
    settings: LongTermSettings,
) -> Dict[str, float]:
    """Allocate capital weight to top themes.

    Returns {theme_name: theme_weight}.
    """
    max_themes = max(1, int(settings.max_themes or 5))
    # Collect all theme -> candidates mapping
    code_row_map: Dict[str, Dict] = {}
    for row in scan_rows:
        code_row_map[str(row.get("code", "")).upper()] = row

    theme_candidates: Dict[str, List[str]] = {}
    for c in candidates:
        code = str(c.code).upper()
        row = code_row_map.get(code, {})
        for theme in _extract_themes_from_scan(row):
            theme_candidates.setdefault(theme, [])
            if code not in theme_candidates[theme]:
                theme_candidates[theme].append(code)

    if not theme_candidates:
        return {}

    # Score each theme: avg_strength × log1p(count) for diminishing breadth bonus
    # This prevents one giant concept from dominating allocation
    theme_scores: Dict[str, float] = {}
    for theme, codes in theme_candidates.items():
        strength = _theme_strength(theme, scan_rows)
        breadth_bonus = np.log1p(float(len(codes)))
        theme_scores[theme] = strength * breadth_bonus

    # Take top themes
    ranked = sorted(theme_scores.items(), key=lambda x: x[1], reverse=True)[:max_themes]
    total = sum(v for _, v in ranked) or 1.0
    return {theme: round(v / total, 6) for theme, v in ranked}


def _theme_based_weight_map(
    candidates: List[StockCandidate],
    scan_rows: List[Dict],
    settings: LongTermSettings,
) -> Dict[str, float]:
    """Build weight map using theme allocation + individual HRS scores.

    Returns {code: target_weight}.
    """
    code_row_map: Dict[str, Dict] = {}
    for row in scan_rows:
        code_row_map[str(row.get("code", "")).upper()] = row

    # Filter candidates: must have min_heat_entry, and not exceed max_heat_entry
    min_heat = float(settings.min_heat_entry or 15.0)
    max_heat = float(getattr(settings, "max_heat_entry", 75.0) or 75.0)
    rsi_max = float(getattr(settings, "rsi_max_entry", 75.0) or 75.0)
    exclude_star = bool(getattr(settings, "exclude_star_board", True))
    gem_universe = bool(getattr(settings, "gem_universe", False))
    gem_board = str(getattr(settings, "gem_board_prefix", "30") or "30")

    eligible = []
    for c in candidates:
        code = str(c.code).upper()
        row = code_row_map.get(code, {})

        # GEM-only mode: only consider GEM stocks for new buys
        if gem_universe and not code.startswith(gem_board):
            continue

        # Exclude STAR board (688xxx) — market orders require limit price protection
        if exclude_star and code.startswith("688"):
            continue

        heat_score = float(row.get("ths_heat_score", 0.0) or 0.0)

        # Heat filter: relaxed for GEM stocks (no THS coverage → heat_score=0)
        if gem_universe and code.startswith(gem_board):
            if heat_score > max_heat:
                continue  # skip overbought
            # Don't filter by min_heat for GEM (THS data often missing)
        else:
            if heat_score < min_heat:
                continue
            if heat_score > max_heat:
                continue  # skip overbought stocks

        rsi = float(row.get("rsi14", 0.0) or 0.0)
        if rsi > 0 and rsi > rsi_max:
            continue  # skip overbought (RSI > 75)
        eligible.append(c)

    if not eligible:
        eligible = list(candidates)  # fallback to all

    # Compute HRS per candidate
    hrs_map: Dict[str, float] = {}
    for c in eligible:
        code = str(c.code).upper()
        row = code_row_map.get(code, {})
        hrs_map[code] = _compute_hrs(row, settings)

    # Theme allocation
    theme_weights = _theme_allocation(eligible, scan_rows, settings)
    max_per_theme = max(1, int(settings.max_per_theme or 2))
    single_name_cap = float(settings.single_name_cap)
    max_holdings = max(1, int(getattr(settings, "rotation_max_holdings", settings.max_holdings) or 5))

    # Assign each candidate to its strongest theme
    code_theme: Dict[str, str] = {}
    theme_best: Dict[str, List[str]] = {}
    for c in eligible:
        code = str(c.code).upper()
        row = code_row_map.get(code, {})
        themes = _extract_themes_from_scan(row)
        # Find best matching theme in our allocation
        best_theme = ""
        best_weight = 0.0
        for t in themes:
            if t in theme_weights and theme_weights[t] > best_weight:
                best_weight = theme_weights[t]
                best_theme = t
        if best_theme:
            code_theme[code] = best_theme
            theme_best.setdefault(best_theme, [])
            theme_best[best_theme].append(code)

    # Within each theme, take top N by HRS
    selected: List[str] = []
    for theme, codes in theme_best.items():
        ranked = sorted(codes, key=lambda c: hrs_map.get(c, 0.0), reverse=True)[:max_per_theme]
        selected.extend(ranked)

    # Fallback: if no theme assignment, take top by HRS
    unassigned = [c for c in eligible if str(c.code).upper() not in selected]
    unassigned_sorted = sorted(unassigned, key=lambda c: hrs_map.get(str(c.code).upper(), 0.0), reverse=True)
    remaining_slots = max_holdings - len(selected)
    if remaining_slots > 0:
        selected.extend([str(uc.code).upper() for uc in unassigned_sorted[:remaining_slots]])

    selected = selected[:max_holdings]

    # Build weights: 50% equal-weight base + 50% HRS proportional
    n = float(max(1, len(selected)))
    weights: Dict[str, float] = {}
    hrses = {code: max(0.0, hrs_map.get(code, 0.0)) for code in selected}
    total_hrs = sum(hrses.values())
    if total_hrs <= 0:
        total_hrs = n

    for code in selected:
        hrs_w = hrses[code] / total_hrs
        weights[code] = 0.50 / n + 0.50 * hrs_w

    # Normalize and apply single_name_cap
    total_w = sum(weights.values()) or 1.0
    weights = {c: min(w / total_w, single_name_cap) for c, w in weights.items()}
    # Re-normalize after capping
    total_w2 = sum(weights.values()) or 1.0
    weights = {c: round(w / total_w2, 6) for c, w in weights.items()}

    return weights


def _generate_exit_signals(
    portfolio: PortfolioState,
    quotes: Dict[str, Dict],
    scan_rows: List[Dict],
    settings: LongTermSettings,
    trade_date: str,
) -> List[RebalanceActionItem]:
    """Generate sell signals for holdings based on 5 rotation exit rules.

    Rules:
    1. Heat decay: heat_score < entry_heat * heat_exit_decay, or heat < abs_min
    2. Trend break: close < MA10 and MA5 < MA10
    3. Max hold days exceeded
    4. Trailing stop: price < highest_price * (1 - trailing_stop_pct)
    5. Theme exit: holding's themes have fallen out of top hot concepts
    """
    if not portfolio.positions:
        return []

    code_row_map: Dict[str, Dict] = {}
    for row in scan_rows:
        code_row_map[str(row.get("code", "")).upper()] = row

    heat_exit_decay = float(settings.heat_exit_decay or 0.6)
    min_heat_abs = 30.0
    max_hold_days = int(settings.max_hold_days or 15)
    trailing_stop_pct = float(settings.trailing_stop_pct or 0.08)

    # Determine top themes from scan
    all_themes: Dict[str, float] = {}
    for row in scan_rows:
        for theme in _extract_themes_from_scan(row):
            all_themes[theme] = all_themes.get(theme, 0.0) + float(row.get("score", 0.0) or 0.0)
    top_themes = set(t for t, _ in sorted(all_themes.items(), key=lambda x: x[1], reverse=True)[:5])

    exit_signals: List[RebalanceActionItem] = []

    for pos in portfolio.positions:
        if pos.quantity <= 0:
            continue
        code = str(pos.code).upper()
        row = code_row_map.get(code, {})
        px = float(row.get("close", 0) or row.get("price", 0) or 0)
        if px <= 0:
            quote = quotes.get(code, {}) or {}
            px = float(quote.get("price", 0) or 0)
        if px <= 0:
            px = float(pos.last_price or pos.cost_price)
        if px <= 0:
            continue

        reasons: List[str] = []
        current_value = float(pos.quantity) * px

        # Rule 1: Heat decay
        heat_score = float(row.get("ths_heat_score", 0.0) or 0.0)
        entry_heat = float(getattr(pos, "entry_heat", 0) or 0)
        if entry_heat > 0:
            if heat_score < entry_heat * heat_exit_decay:
                reasons.append(f"heat_decay({heat_score:.1f}<{entry_heat*heat_exit_decay:.1f})")
        elif heat_score < min_heat_abs and heat_score > 0:
            reasons.append(f"heat_too_low({heat_score:.1f}<{min_heat_abs})")

        # Rule 2: Trend break (with buffer — 3% below MA10 to avoid noise)
        ma5 = float(row.get("ma5", 0) or 0)
        ma10 = float(row.get("ma10", 0) or 0)
        close = float(row.get("close", 0) or px)
        trend_break_pct = float(getattr(settings, "trend_break_pct", 0.03) or 0.03)
        if close > 0 and ma5 > 0 and ma10 > 0:
            if close < ma10 * (1.0 - trend_break_pct) and ma5 < ma10:
                reasons.append(f"trend_break(close={close:.2f}<ma10={ma10:.2f},ma5<ma10)")

        # Rule 3: Max hold days
        entry_date = str(getattr(pos, "entry_date", "") or "")
        if entry_date:
            try:
                e_dt = datetime.strptime(entry_date, "%Y-%m-%d")
                t_dt = datetime.strptime(trade_date, "%Y-%m-%d")
                held_days = (t_dt - e_dt).days
                if held_days > max_hold_days:
                    reasons.append(f"max_hold_days({held_days}>{max_hold_days})")
            except Exception:
                pass

        # Rule 4: Trailing stop
        highest = float(getattr(pos, "highest_price", 0) or 0)
        if highest > 0 and px < highest * (1.0 - trailing_stop_pct):
            reasons.append(f"trailing_stop({px:.2f}<{highest*(1-trailing_stop_pct):.2f})")

        # Rule 5: Theme exit
        pos_themes = _extract_themes_from_scan(row)
        if pos_themes and top_themes:
            if not any(t in top_themes for t in pos_themes):
                reasons.append(f"theme_exit({','.join(pos_themes[:2])})")

        if reasons:
            exit_signals.append(
                RebalanceActionItem(
                    code=code,
                    name=pos.name or code,
                    action="sell",
                    reference_price=round(px, 3),
                    target_weight=0.0,
                    current_weight=round(current_value / max(portfolio.nav, 1.0), 6),
                    delta_shares=-int(pos.quantity),
                    estimated_amount=round(current_value, 2),
                    score=0.0,
                    reason="|".join(reasons),
                )
            )

    return exit_signals


def mark_to_market(portfolio: PortfolioState, quotes: Dict[str, Dict]) -> PortfolioState:
    pos = []
    for item in portfolio.positions:
        quote = quotes.get(item.code) or quotes.get(item.code.upper()) or {}
        last_price = float(quote.get("price", 0) or 0)
        if last_price <= 0:
            last_price = float(item.last_price or item.cost_price)
        pos.append(replace(item, last_price=last_price))
    return PortfolioState(
        as_of=portfolio.as_of,
        initial_capital=portfolio.initial_capital,
        cash=portfolio.cash,
        available_cash=portfolio.available_cash,
        frozen_cash=portfolio.frozen_cash,
        positions=pos,
        target_weights=dict(portfolio.target_weights),
    )


def build_rebalance_plan(
    trade_date: str,
    candidates: List[StockCandidate],
    portfolio: PortfolioState,
    quotes: Dict[str, Dict],
    settings: LongTermSettings,
    *,
    rotation_scan: Optional[Dict] = None,
) -> Tuple[RebalancePlan, PortfolioState]:
    portfolio_mtm = mark_to_market(portfolio, quotes)

    rotation_mode = bool(getattr(settings, "rotation_mode", False))
    if rotation_mode and rotation_scan:
        # --- Rotation mode path ---
        scan_rows = list((rotation_scan.get("ranked") or []) or [])
        if not scan_rows:
            scan_rows = list((rotation_scan.get("top_candidates") or []) or [])

        # Detect market trend and adjust max_holdings
        is_bull, effective_holdings = _detect_market_trend(quotes, settings)
        # Temporarily override max_holdings for bear market
        saved_holdings = settings.max_holdings
        settings.max_holdings = effective_holdings
        try:
            target_weights = _theme_based_weight_map(candidates, scan_rows, settings)
        finally:
            settings.max_holdings = saved_holdings
        if not target_weights:
            target_weights = _safe_weight_map(candidates, settings)

        # Generate exit signals for current holdings
        exit_signals_list = _generate_exit_signals(
            portfolio_mtm, quotes, scan_rows, settings, trade_date
        )

        # Build plan with rotation weights
        return _build_rebalance_plan_from_weights(
            trade_date=trade_date,
            candidates=candidates,
            portfolio=portfolio_mtm,
            quotes=quotes,
            settings=settings,
            target_weights=target_weights,
            extra_sells=exit_signals_list,
            source_label="rotation-engine",
        )

    # --- Original path (non-rotation) ---
    target_weights = _safe_weight_map(candidates, settings)
    if not target_weights:
        target_weights = dict(portfolio_mtm.target_weights)

    return _build_rebalance_plan_from_weights(
        trade_date=trade_date,
        candidates=candidates,
        portfolio=portfolio_mtm,
        quotes=quotes,
        settings=settings,
        target_weights=target_weights,
        source_label="longterm-sim-engine",
    )


def _build_rebalance_plan_from_weights(
    trade_date: str,
    candidates: List[StockCandidate],
    portfolio: PortfolioState,
    quotes: Dict[str, Dict],
    settings: LongTermSettings,
    target_weights: Dict[str, float],
    *,
    extra_sells: Optional[List[RebalanceActionItem]] = None,
    source_label: str = "longterm-sim-engine",
) -> Tuple[RebalancePlan, PortfolioState]:
    """Build a RebalancePlan from pre-computed target weights and optional extra sells."""
    single_name_cap = float(settings.single_name_cap)
    cash_buffer_ratio = float(settings.cash_buffer_ratio)
    rebalance_threshold = float(settings.rebalance_threshold)
    max_industry_weight = float(settings.max_industry_weight)
    min_trade_amount = float(settings.min_trade_amount)
    max_portfolio_volatility = float(settings.max_portfolio_volatility)
    max_portfolio_drawdown = float(settings.max_portfolio_drawdown)

    capped_weights: Dict[str, float] = {}
    for code, weight in target_weights.items():
        capped_weights[code] = min(weight, single_name_cap)
    weight_sum = sum(capped_weights.values())
    if weight_sum > (1.0 - cash_buffer_ratio) and weight_sum > 0:
        scale = (1.0 - cash_buffer_ratio) / weight_sum
        capped_weights = {code: weight * scale for code, weight in capped_weights.items()}

    nav = max(portfolio.nav, 1.0)
    current_values = {item.code: item.market_value for item in portfolio.positions}
    current_weights = {code: value / nav for code, value in current_values.items()}
    name_map = {item.code: item.name for item in portfolio.positions}
    name_map.update({item.code: item.name for item in candidates})
    industry_map = {item.code: (item.industry or "UNKNOWN") for item in candidates}
    for item in portfolio.positions:
        industry_map.setdefault(item.code, "UNKNOWN")
    score_map = {item.code: _score_candidate(item, settings) for item in candidates}
    risk_score_map = {item.code: float(item.risk_score) for item in candidates}
    theme_map: Dict[str, List[str]] = {}
    for item in candidates:
        code = str(item.code).upper()
        tags = [str(x).strip() for x in (item.tags or []) if str(x).strip()]
        themes = extract_themes(tags)
        if not themes:
            industry = str(item.industry or "")
            if "-" in industry:
                tail = industry.split("-", 1)[1].strip()
                if tail:
                    themes = [tail]
        theme_map[code] = themes
    for item in portfolio.positions:
        risk_score_map.setdefault(item.code, 50.0)
        theme_map.setdefault(item.code, [])
    position_price_map = {item.code: float(item.last_price or item.cost_price or 0) for item in portfolio.positions}

    raw_actions: List[RebalanceActionItem] = []
    for code, target_weight in capped_weights.items():
        current_weight = current_weights.get(code, 0.0)
        if abs(target_weight - current_weight) < rebalance_threshold:
            continue
        px = _resolve_reference_price(code=code, quotes=quotes, position_price_map=position_price_map)
        if px <= 0:
            continue
        target_value = target_weight * nav
        current_value = current_values.get(code, 0.0)
        delta_value = target_value - current_value
        delta_shares = int(delta_value / px)
        if delta_shares == 0:
            continue
        action = "buy" if delta_shares > 0 else "sell"
        estimated_amount = abs(delta_shares) * px
        reason = (
            f"target_weight={target_weight:.2%}, current_weight={current_weight:.2%}, "
            f"score={score_map.get(code, 0.0):.1f}"
        )
        raw_actions.append(
            RebalanceActionItem(
                code=code,
                name=name_map.get(code, code),
                action=action,
                reference_price=round(px, 3),
                target_weight=round(target_weight, 6),
                current_weight=round(current_weight, 6),
                delta_shares=delta_shares,
                estimated_amount=round(estimated_amount, 2),
                score=round(score_map.get(code, 0.0), 2),
                reason=reason,
            )
        )

    # Auto-generate exit suggestions for holdings not in target set.
    target_codes = set(capped_weights.keys())
    for item in portfolio.positions:
        if item.code in target_codes:
            continue
        if item.quantity <= 0:
            continue
        px = _resolve_reference_price(code=item.code, quotes=quotes, position_price_map=position_price_map)
        if px <= 0:
            continue
        estimated_amount = abs(int(item.quantity)) * float(px)
        raw_actions.append(
            RebalanceActionItem(
                code=item.code,
                name=item.name,
                action="sell",
                reference_price=float(px),
                target_weight=0.0,
                current_weight=round(current_weights.get(item.code, 0.0), 6),
                delta_shares=-int(item.quantity),
                estimated_amount=round(estimated_amount, 2),
                score=0.0,
                reason="not_in_target_universe",
            )
        )

    # Prepend extra sells (e.g. rotation exit signals) — they take priority
    if extra_sells:
        existing_codes = {a.code for a in raw_actions}
        for es in extra_sells:
            if es.code not in existing_codes:
                raw_actions.insert(0, es)

    # Constraint pass: min trade amount, cash buffer, industry cap.
    actions: List[RebalanceActionItem] = []
    rejected_actions: List[RebalanceRejectedItem] = []
    projected_values = dict(current_values)
    projected_cash = float(portfolio.available_cash or portfolio.cash)
    min_cash_required = nav * cash_buffer_ratio
    ordered = sorted(raw_actions, key=lambda x: (0 if x.action == "sell" else 1, -abs(x.estimated_amount)))
    for item in ordered:
        if item.estimated_amount < min_trade_amount:
            rejected_actions.append(
                RebalanceRejectedItem(
                    code=item.code,
                    name=item.name,
                    action=item.action,
                    reference_price=item.reference_price,
                    delta_shares=item.delta_shares,
                    estimated_amount=item.estimated_amount,
                    reason=f"min_trade_amount({item.estimated_amount:.2f}<{min_trade_amount:.2f})",
                )
            )
            continue

        current_code_value = float(projected_values.get(item.code, 0.0))
        delta_value = float(item.delta_shares) * float(item.reference_price)
        next_code_value = max(0.0, current_code_value + delta_value)
        industry = industry_map.get(item.code, "UNKNOWN")

        if item.action == "buy":
            buy_cost = abs(delta_value)
            if projected_cash - buy_cost < min_cash_required:
                rejected_actions.append(
                    RebalanceRejectedItem(
                        code=item.code,
                        name=item.name,
                        action=item.action,
                        reference_price=item.reference_price,
                        delta_shares=item.delta_shares,
                        estimated_amount=item.estimated_amount,
                        reason="cash_buffer_violation",
                    )
                )
                continue
            projected_industry = 0.0
            for code, value in projected_values.items():
                if industry_map.get(code, "UNKNOWN") == industry:
                    projected_industry += float(value)
            projected_industry += max(0.0, delta_value)
            projected_industry_weight = projected_industry / nav if nav > 0 else 0.0
            cap_limit = industry_cap_for(industry, settings)
            if projected_industry_weight > cap_limit:
                rejected_actions.append(
                    RebalanceRejectedItem(
                        code=item.code,
                        name=item.name,
                        action=item.action,
                        reference_price=item.reference_price,
                        delta_shares=item.delta_shares,
                        estimated_amount=item.estimated_amount,
                        reason=f"industry_cap_violation({industry}:{projected_industry_weight:.2%}>{cap_limit:.2%})",
                    )
                )
                continue
            item_themes = list(theme_map.get(item.code, []) or [])
            for theme in item_themes:
                projected_theme = 0.0
                for code, value in projected_values.items():
                    if theme in (theme_map.get(code, []) or []):
                        projected_theme += float(value)
                projected_theme += max(0.0, delta_value)
                projected_theme_weight = projected_theme / nav if nav > 0 else 0.0
                theme_cap = theme_cap_for(theme, settings)
                if projected_theme_weight > theme_cap:
                    rejected_actions.append(
                        RebalanceRejectedItem(
                            code=item.code,
                            name=item.name,
                            action=item.action,
                            reference_price=item.reference_price,
                            delta_shares=item.delta_shares,
                            estimated_amount=item.estimated_amount,
                            reason=f"theme_cap_violation({theme}:{projected_theme_weight:.2%}>{theme_cap:.2%})",
                        )
                    )
                    item_themes = []
                    break
            if not item_themes and (theme_map.get(item.code, []) or []):
                continue
            trial_values = dict(projected_values)
            trial_values[item.code] = next_code_value
            projected_volatility, projected_drawdown = _portfolio_risk_budget(
                projected_values=trial_values,
                nav=nav,
                risk_score_map=risk_score_map,
            )
            if projected_volatility > max_portfolio_volatility:
                rejected_actions.append(
                    RebalanceRejectedItem(
                        code=item.code,
                        name=item.name,
                        action=item.action,
                        reference_price=item.reference_price,
                        delta_shares=item.delta_shares,
                        estimated_amount=item.estimated_amount,
                        reason=(
                            "risk_budget_volatility_violation("
                            f"{projected_volatility:.2%}>{max_portfolio_volatility:.2%})"
                        ),
                    )
                )
                continue
            if projected_drawdown > max_portfolio_drawdown:
                rejected_actions.append(
                    RebalanceRejectedItem(
                        code=item.code,
                        name=item.name,
                        action=item.action,
                        reference_price=item.reference_price,
                        delta_shares=item.delta_shares,
                        estimated_amount=item.estimated_amount,
                        reason=(
                            "risk_budget_drawdown_violation("
                            f"{projected_drawdown:.2%}>{max_portfolio_drawdown:.2%})"
                        ),
                    )
                )
                continue
            projected_cash -= buy_cost
        else:
            projected_cash += abs(delta_value)

        projected_values[item.code] = next_code_value
        actions.append(item)

    plan = RebalancePlan(
        plan_id=datetime.now().strftime("%H%M%S%f"),
        trade_date=trade_date,
        source=source_label,
        constraints={
            "single_name_cap": single_name_cap,
            "cash_buffer_ratio": cash_buffer_ratio,
            "rebalance_threshold": rebalance_threshold,
            "max_industry_weight": max_industry_weight,
            "industry_cap_mode_tiered": 1.0
            if str(getattr(settings, "industry_cap_mode", "single")).strip().lower() == "tiered"
            else 0.0,
            "core_industry_cap": float(getattr(settings, "core_industry_cap", max_industry_weight)),
            "satellite_industry_cap": float(getattr(settings, "satellite_industry_cap", max_industry_weight)),
            "max_theme_weight": float(getattr(settings, "max_theme_weight", 0.6)),
            "theme_cap_mode_tiered": 1.0
            if str(getattr(settings, "theme_cap_mode", "single")).strip().lower() == "tiered"
            else 0.0,
            "core_theme_cap": float(getattr(settings, "core_theme_cap", 0.75)),
            "satellite_theme_cap": float(getattr(settings, "satellite_theme_cap", 0.4)),
            "min_trade_amount": min_trade_amount,
            "max_portfolio_volatility": max_portfolio_volatility,
            "max_portfolio_drawdown": max_portfolio_drawdown,
        },
        actions=actions,
        rejected_actions=rejected_actions,
    )
    portfolio.target_weights = capped_weights
    portfolio.as_of = trade_date
    return plan, portfolio


def apply_manual_executions(portfolio: PortfolioState, records: List[ManualExecutionItem]) -> PortfolioState:
    by_code = {item.code: item for item in portfolio.positions}
    cash = float(portfolio.cash)

    for rec in records:
        if rec.quantity <= 0 or rec.price <= 0:
            continue
        code = rec.code.upper()
        pos = by_code.get(code)
        if pos is None:
            pos = SimPosition(code=code, name=rec.name or code, quantity=0, cost_price=rec.price, last_price=rec.price)
            by_code[code] = pos
        amount = rec.price * rec.quantity
        fee = max(0.0, rec.fee)

        if rec.side == "buy":
            new_qty = pos.quantity + rec.quantity
            new_cost = (pos.cost_price * pos.quantity + amount + fee) / max(new_qty, 1)
            pos.quantity = new_qty
            pos.cost_price = round(new_cost, 4)
            pos.last_price = rec.price
            cash -= (amount + fee)
        else:
            sell_qty = min(pos.quantity, rec.quantity)
            if sell_qty <= 0:
                continue
            effective_fee = fee * (float(sell_qty) / float(rec.quantity))
            pos.quantity -= sell_qty
            pos.last_price = rec.price
            cash += (rec.price * sell_qty - effective_fee)

    next_positions = [item for item in by_code.values() if item.quantity > 0]
    return PortfolioState(
        as_of=portfolio.as_of,
        initial_capital=portfolio.initial_capital,
        cash=round(cash, 2),
        available_cash=round(max(0.0, cash - float(portfolio.frozen_cash or 0.0)), 2),
        frozen_cash=float(portfolio.frozen_cash or 0.0),
        positions=sorted(next_positions, key=lambda x: x.market_value, reverse=True),
        target_weights=dict(portfolio.target_weights),
    )

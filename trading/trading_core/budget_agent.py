from typing import Dict, List

from .models import SelectionResult, StockDataSnapshot


def _volatility_unit(data: StockDataSnapshot) -> float:
    intraday_range = max(float(data.high or 0) - float(data.low or 0), 0.0)
    base = max(float(data.price or 0) * 0.0035, 0.01)
    if intraday_range > 0:
        base = max(base, intraday_range * 0.22)
    if data.pre_close > 0 and data.amplitude_pct > 0:
        base = max(base, float(data.pre_close) * (float(data.amplitude_pct) / 100.0) * 0.18)
    return round(base, 3)


def allocate_t_budget(
    selections: List[SelectionResult],
    data_snapshots: Dict[str, StockDataSnapshot],
    holding_values: Dict[str, float],
    floating_cash_budget: float,
    max_single_t_ratio_of_holding: float = 0.35,
    risk_adjustments: Dict[str, float] | None = None,
) -> List[SelectionResult]:
    positive = [item for item in selections if item.action in ("focus", "watch") and item.score > 0]
    total_score = sum(item.score for item in positive)
    if total_score <= 0:
        return selections

    updated: List[SelectionResult] = []
    for item in selections:
        data = data_snapshots.get(item.code)
        if item not in positive or not data or data.price <= 0:
            item.buy_budget_amount = 0.0
            item.sell_budget_amount = 0.0
            item.suggested_buy_shares = 0
            item.suggested_sell_shares = 0
            updated.append(item)
            continue
        raw_buy_budget = max(0.0, floating_cash_budget) * (item.score / total_score)
        if item.action == "watch":
            raw_buy_budget *= 0.5
        max_buy_budget = max(0.0, floating_cash_budget) * (0.45 if item.action == "focus" else 0.2)
        buy_budget = min(raw_buy_budget, max_buy_budget)

        holding_value = float(holding_values.get(item.code, 0.0) or 0.0)
        sell_budget = holding_value * max_single_t_ratio_of_holding

        if item.am_mode == "sell_only" and item.pm_mode == "sell_only":
            buy_budget = 0.0
        if item.am_mode == "observe_only" and item.pm_mode == "observe_only":
            buy_budget = 0.0
            sell_budget = 0.0
        if item.buy_blocked:
            buy_budget = 0.0

        unit = _volatility_unit(data)
        risk_factor = float((risk_adjustments or {}).get(item.code, 1.0) or 1.0)
        account_risk_budget = max(0.0, floating_cash_budget) * (0.012 if item.action == "focus" else 0.006) * (item.score / 100) * risk_factor
        risk_budget = min(buy_budget, account_risk_budget)
        sell_risk_budget = min(sell_budget, account_risk_budget * 1.2)
        buy_budget_shares = int(buy_budget / data.price / 100) * 100 if data.price > 0 else 0
        sell_budget_shares = int(sell_budget / data.price / 100) * 100 if data.price > 0 else 0
        buy_risk_shares = int(risk_budget / unit / 100) * 100 if unit > 0 else buy_budget_shares
        sell_risk_shares = int(sell_risk_budget / unit / 100) * 100 if unit > 0 else sell_budget_shares
        buy_lot_shares = min(buy_budget_shares, buy_risk_shares if buy_risk_shares > 0 else buy_budget_shares)
        sell_lot_shares = min(sell_budget_shares, sell_risk_shares if sell_risk_shares > 0 else sell_budget_shares)
        if buy_budget > 0 and buy_lot_shares <= 0:
            buy_lot_shares = 100
        if sell_budget > 0 and sell_lot_shares <= 0:
            sell_lot_shares = 100
        item.buy_budget_amount = round(max(0.0, buy_budget), 2)
        item.sell_budget_amount = round(max(0.0, sell_budget), 2)
        item.suggested_buy_shares = max(0, buy_lot_shares)
        item.suggested_sell_shares = max(0, sell_lot_shares)
        updated.append(item)
    return updated

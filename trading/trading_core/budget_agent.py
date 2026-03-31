from typing import Dict, List

from .models import SelectionResult, StockDataSnapshot


def allocate_t_budget(
    selections: List[SelectionResult],
    data_snapshots: Dict[str, StockDataSnapshot],
    holding_values: Dict[str, float],
    floating_cash_budget: float,
    max_single_t_ratio_of_holding: float = 0.35,
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

        buy_lot_shares = int(buy_budget / data.price / 100) * 100 if data.price > 0 else 0
        sell_lot_shares = int(sell_budget / data.price / 100) * 100 if data.price > 0 else 0
        item.buy_budget_amount = round(max(0.0, buy_budget), 2)
        item.sell_budget_amount = round(max(0.0, sell_budget), 2)
        item.suggested_buy_shares = max(0, buy_lot_shares)
        item.suggested_sell_shares = max(0, sell_lot_shares)
        updated.append(item)
    return updated

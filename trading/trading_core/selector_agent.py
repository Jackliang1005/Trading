from datetime import datetime
from typing import Dict, List

from .budget_agent import allocate_t_budget
from .models import MarketRegimeReport, SelectionResult, StockDataSnapshot, StockNewsSnapshot, StockRule
from .paths import FOCUS_LIST_PATH, UNIVERSE_STATE_PATH
from .storage import atomic_write_json, load_daily_plan, load_json, save_daily_plan


def _level(score: int) -> str:
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 45:
        return "C"
    return "D"


def _ratio_for_score(score: int, regime: str) -> float:
    if score < 45:
        return 0.0
    if regime == "panic":
        return 0.25 if score >= 70 else 0.1
    if regime == "weak":
        return 0.25 if score >= 65 else 0.15
    if regime == "strong":
        return 0.5 if score >= 70 else 0.25
    return 0.4 if score >= 70 else 0.2


def _event_weight(rule: StockRule, tag: Dict) -> int:
    strategy = rule.strategy or ""
    tag_name = tag.get("tag", "")
    direction = tag.get("direction", "neutral")
    if tag_name == "war_risk":
        if "逆T" in strategy or rule.allow_market_panic_reverse_t:
            return 6 if direction == "negative" else 0
        return -8 if direction == "negative" else 0
    if tag_name == "policy_support":
        if "顺T" in strategy or "箱体" in strategy:
            return 8 if direction == "positive" else 0
        return 4 if direction == "positive" else 0
    if tag_name == "earnings_pressure":
        return -10 if direction == "negative" else 0
    if tag_name == "order_growth":
        return 6 if direction == "positive" else 0
    if tag_name == "sector_rotation":
        return 5 if direction == "positive" else -3
    return 0


def _decide_halfday_modes(rule: StockRule, market_regime: MarketRegimeReport, news: StockNewsSnapshot, score: int) -> tuple[str, str]:
    strategy = rule.strategy or ""
    if score < 45:
        return "observe_only", "observe_only"
    if market_regime.regime == "panic":
        if "逆T" in strategy or rule.allow_market_panic_reverse_t:
            return "reverse_t_only", "sell_only"
        return "sell_only", "sell_only"
    if news.stock_sentiment == "negative" and news.sector_sentiment == "negative":
        return "sell_only", "observe_only"
    if any(tag["tag"] == "war_risk" and tag["direction"] == "negative" for tag in news.event_tags):
        return "sell_only", "observe_only"
    if any(tag["tag"] == "policy_support" and tag["direction"] == "positive" for tag in news.event_tags):
        return "trend_t_only", "trend_t_only"
    if "顺T" in strategy:
        return "trend_t_only", "box_t_only"
    if "逆T" in strategy:
        return "reverse_t_only", "observe_only"
    if "箱体" in strategy:
        return "box_t_only", "box_t_only"
    return "observe_only", "observe_only"


def select_focus_list(
    rules: List[StockRule],
    data_snapshots: Dict[str, StockDataSnapshot],
    news_snapshots: Dict[str, StockNewsSnapshot],
    market_regime: MarketRegimeReport,
    floating_cash_budget: float = 100000.0,
    max_single_t_ratio_of_holding: float = 0.35,
) -> List[SelectionResult]:
    results: List[SelectionResult] = []
    holding_values = {
        rule.code: float(rule.base_position) * float(data_snapshots[rule.code].price)
        for rule in rules
        if rule.code in data_snapshots
    }
    for rule in rules:
        data = data_snapshots[rule.code]
        news = news_snapshots[rule.code]
        score = 35
        reasons = []
        if data.amplitude_pct >= 4.0:
            score += 18
            reasons.append(f"振幅{data.amplitude_pct:.1f}%适合做T")
        elif data.amplitude_pct >= 2.0:
            score += 10
            reasons.append(f"振幅{data.amplitude_pct:.1f}%可做T")
        else:
            score -= 12
            reasons.append("振幅偏小")
        if data.main_net_inflow > 0:
            score += 10
            reasons.append(f"资金流入{data.main_net_inflow:.0f}万")
        elif data.main_net_inflow < 0:
            score -= 8
            reasons.append(f"资金流出{abs(data.main_net_inflow):.0f}万")
        score += news.stock_score * 3
        score += news.sector_score * 2
        score += news.macro_score
        for tag in news.event_tags:
            weighted = _event_weight(rule, tag)
            if weighted:
                score += weighted
                reasons.append(f"事件{tag.get('tag')}影响{weighted:+d}")
        if news.stock_sentiment == "positive":
            reasons.append("个股消息偏正面")
        elif news.stock_sentiment == "negative":
            reasons.append("个股消息偏负面")
        if news.sector_sentiment == "positive":
            reasons.append("板块消息偏正面")
        elif news.sector_sentiment == "negative":
            reasons.append("板块消息偏负面")
        if news.macro_sentiment == "positive":
            reasons.append("宏观事件偏正面")
        elif news.macro_sentiment == "negative":
            reasons.append("宏观事件偏负面")
        if "顺T" in rule.strategy and market_regime.regime == "strong":
            score += 8
            reasons.append("顺T策略匹配强/震荡市")
        if "逆T" in rule.strategy and market_regime.allow_reverse_t:
            score += 12
            reasons.append("逆T策略匹配恐慌反抽")
        if market_regime.regime == "panic" and "顺T" in rule.strategy:
            score -= 10
            reasons.append("恐慌市顺T降级")
        score = max(0, min(score, 100))
        ratio = _ratio_for_score(score, market_regime.regime)
        suggested_shares = int(rule.per_trade_shares * ratio / 0.25) if ratio > 0 else 0
        am_mode, pm_mode = _decide_halfday_modes(rule, market_regime, news, score)
        if ratio >= 0.25:
            action = "focus"
        elif ratio > 0:
            action = "watch"
        else:
            action = "avoid"
        explanation_parts = []
        if news.event_tags:
            explanation_parts.append(
                "事件标签: " + ",".join(f"{tag['tag']}:{tag['direction']}/{tag['horizon']}" for tag in news.event_tags[:3])
            )
        if news.stock_items:
            explanation_parts.append(f"个股: {news.stock_items[0].get('title', '')}")
        if news.sector_items:
            explanation_parts.append(f"板块: {news.sector_items[0].get('title', '')}")
        if news.macro_items:
            explanation_parts.append(f"宏观: {news.macro_items[0].get('title', '')}")
        results.append(SelectionResult(
            code=rule.code,
            name=rule.name,
            action=action,
            am_mode=am_mode,
            pm_mode=pm_mode,
            score=score,
            level=_level(score),
            buy_budget_amount=0.0,
            sell_budget_amount=0.0,
            suggested_t_ratio=round(ratio, 2),
            suggested_buy_shares=max(0, suggested_shares),
            suggested_sell_shares=max(0, suggested_shares),
            reason=" / ".join(reasons[:5]),
            explanation=" | ".join(explanation_parts[:3]),
            regime=market_regime.regime,
        ))
    ranked = sorted(results, key=lambda item: item.score, reverse=True)
    return allocate_t_budget(
        ranked,
        data_snapshots,
        holding_values,
        floating_cash_budget=floating_cash_budget,
        max_single_t_ratio_of_holding=max_single_t_ratio_of_holding,
    )


def save_selection_outputs(results: List[SelectionResult]) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    focus_payload = {
        "updated_at": now,
        "focus": [item.__dict__ for item in results if item.action == "focus"][:3],
        "watch": [item.__dict__ for item in results if item.action == "watch"][:5],
        "avoid": [item.__dict__ for item in results if item.action == "avoid"][:10],
    }
    universe_payload = {
        "updated_at": now,
        "stocks": [item.__dict__ for item in results],
    }
    atomic_write_json(FOCUS_LIST_PATH, focus_payload)
    atomic_write_json(UNIVERSE_STATE_PATH, universe_payload)


def sync_selection_to_daily_plan(rules: List[StockRule], results: List[SelectionResult]) -> Dict:
    plan = load_daily_plan()
    by_code = {item.code: item for item in results}
    output = []
    for rule in rules:
        selection = by_code.get(rule.code)
        if not selection:
            continue
        watch_mode = ""
        enabled = selection.action in ("focus", "watch")
        if selection.action == "watch":
            watch_mode = "light"
        if selection.action == "avoid":
            watch_mode = "light"
        output.append({
            "code": rule.code,
            "name": rule.name,
            "enabled": enabled,
            "per_trade_shares": max(selection.suggested_buy_shares, selection.suggested_sell_shares, rule.per_trade_shares),
            "watch_mode": watch_mode,
            "selection_action": selection.action,
            "selection_am_mode": selection.am_mode,
            "selection_pm_mode": selection.pm_mode,
            "selection_score": selection.score,
            "selection_buy_budget_amount": selection.buy_budget_amount,
            "selection_sell_budget_amount": selection.sell_budget_amount,
            "selection_ratio": selection.suggested_t_ratio,
            "selection_buy_shares": selection.suggested_buy_shares,
            "selection_sell_shares": selection.suggested_sell_shares,
            "selection_reason": selection.reason,
            "selection_explanation": selection.explanation,
            "note": f"selector:{selection.action}/{selection.am_mode}->{selection.pm_mode} {selection.reason}",
        })
    plan["stocks"] = output
    save_daily_plan(plan, source="selector-preopen")
    return plan


def load_selection_map() -> Dict[str, Dict]:
    data = load_json(UNIVERSE_STATE_PATH, {"updated_at": "", "stocks": []})
    result = {}
    for item in data.get("stocks", []):
        code = str(item.get("code", "")).strip()
        if code:
            result[code] = item
    return result


def format_selection_report(results: List[SelectionResult]) -> str:
    lines = ["盘前持仓做T筛选"]
    focus_count = sum(1 for item in results if item.action == "focus")
    watch_count = sum(1 for item in results if item.action == "watch")
    avoid_count = sum(1 for item in results if item.action == "avoid")
    lines.append(f"重点{focus_count} 观察{watch_count} 回避{avoid_count}")
    for item in results[:6]:
        lines.append(
            f"{item.code} {item.name} [{item.action}] 分数{item.score} "
            f"模式{item.am_mode}->{item.pm_mode} 买预算{item.buy_budget_amount:.0f} 卖预算{item.sell_budget_amount:.0f} "
            f"仓位{item.suggested_t_ratio:.0%} 买股数{item.suggested_buy_shares} 卖股数{item.suggested_sell_shares} "
            f"{item.reason}"
        )
    return "\n".join(lines)

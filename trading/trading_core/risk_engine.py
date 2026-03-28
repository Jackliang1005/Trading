from typing import Dict

from .models import MarketRegimeReport, Playbook, RiskDecision, StockRule


def _stock_intraday_position(portfolio_state: Dict, code: str) -> Dict:
    return portfolio_state.get("stocks", {}).get(code, {})


def evaluate_risk(
    rule: StockRule,
    playbook: Playbook,
    market_regime: MarketRegimeReport,
    portfolio_state: Dict,
    state: Dict,
    action: str,
    score: int,
) -> RiskDecision:
    flags = []
    if action == "wait":
        return RiskDecision(False, False, "当前无执行动作", flags)
    if action == "buy" and not playbook.allow_buy:
        return RiskDecision(False, False, "当前playbook不允许买入", ["playbook_buy_blocked"])
    if action == "sell" and not playbook.allow_sell:
        return RiskDecision(False, False, "当前playbook不允许卖出", ["playbook_sell_blocked"])
    if action == "buy" and not market_regime.allow_buy:
        return RiskDecision(False, False, "当前市场不允许主动买入", ["market_buy_blocked"])
    if action == "sell" and not market_regime.allow_sell:
        return RiskDecision(False, False, "当前市场不允许主动卖出", ["market_sell_blocked"])

    intraday = state.setdefault("intraday", {}).get(rule.code, {})
    if intraday.get("auto_trade_count", 0) >= 1:
        flags.append("daily_auto_trade_limit")
    if intraday.get("round_trip_count", 0) >= playbook.max_round_trips:
        flags.append("round_trip_limit")

    stock_state = _stock_intraday_position(portfolio_state, rule.code)
    if action == "sell" and int(stock_state.get("available_to_sell", 0) or 0) < int(rule.per_trade_shares):
        return RiskDecision(False, False, "可卖底仓不足，禁止自动卖出", ["insufficient_sellable"])
    if action == "buy" and market_regime.regime == "panic" and not market_regime.allow_reverse_t:
        return RiskDecision(False, False, "恐慌盘未开放逆T", ["panic_reverse_blocked"])

    allow_auto = score >= 65 and not flags
    if market_regime.risk_level == "high" and action == "buy" and score < 75:
        allow_auto = False
        flags.append("high_risk_buy_requires_stronger_score")
    reason = "允许执行"
    if flags:
        reason = f"人工可看，自动受限: {','.join(flags)}"
    return RiskDecision(True, allow_auto, reason, flags)

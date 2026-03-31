from typing import Dict, Tuple

from .analysis import detect_panic_rebound
from .models import (
    DecisionResult,
    IntradayAnalysis,
    LearningProfile,
    MarketRegimeReport,
    Playbook,
    StockRule,
)
from .risk_engine import evaluate_risk


def _calc_level(score: int) -> str:
    if score >= 80:
        return "S"
    if score >= 65:
        return "A"
    if score >= 45:
        return "B"
    return "C"


def _derive_action(
    rule: StockRule,
    playbook: Playbook,
    analysis: IntradayAnalysis,
    quote: Dict,
    market_regime: MarketRegimeReport,
    learning: LearningProfile,
) -> Tuple[str, str]:
    price = float(quote.get("price", 0) or 0)
    risk_band = max(analysis.risk_unit * 0.35 * max(learning.entry_tolerance, 0.8), price * 0.002 if price > 0 else 0.0)
    near_buy = analysis.t_buy_target > 0 and price <= analysis.t_buy_target + risk_band
    near_sell = analysis.t_sell_target > 0 and price >= analysis.t_sell_target - risk_band
    if playbook.name == "trend_sell_rebuy":
        if analysis.structure not in ("trend_up", "range"):
            return "wait", "顺T只在趋势回踩或强箱体结构下执行"
        if near_sell:
            return "sell", "顺T先卖后买"
        if near_buy and price >= max(analysis.vwap - analysis.risk_unit * 0.8, 0):
            return "buy", "回落到支撑尝试接回"
        return "wait", "未到顺T关键位"
    if playbook.name in ("panic_reverse_t", "reverse_t"):
        if analysis.structure not in ("panic_rebound", "range") and not detect_panic_rebound(rule, quote, type("obj", (), {"regime": market_regime.regime})()):
            return "wait", "逆T只做恐慌反抽或超跌回转"
        if detect_panic_rebound(rule, quote, type("obj", (), {"regime": market_regime.regime})()):
            return "buy", "弱市恐慌反抽逆T"
        if near_buy and market_regime.allow_reverse_t:
            return "buy", "逆T低吸窗口"
        if near_sell:
            return "sell", "逆T反抽到目标位"
        return "wait", "逆T条件不充分"
    if playbook.name == "box_range_t":
        if analysis.structure != "range":
            return "wait", "箱体T只在震荡结构中执行"
        if near_buy:
            return "buy", "箱体下沿低吸"
        if near_sell:
            return "sell", "箱体上沿减仓"
        return "wait", "仍在箱体中部"
    if near_sell:
        return "sell", "观察票只允许减仓"
    return "wait", "观察票不主动开仓"


def make_decision(
    rule: StockRule,
    playbook: Playbook,
    analysis: IntradayAnalysis,
    quote: Dict,
    market_regime: MarketRegimeReport,
    learning: LearningProfile,
    portfolio_state: Dict,
    state: Dict,
    selection_mode: str = "",
    planned_buy_shares: int = 0,
    planned_sell_shares: int = 0,
) -> DecisionResult:
    action, action_reason = _derive_action(rule, playbook, analysis, quote, market_regime, learning)
    if selection_mode == "sell_only" and action == "buy":
        action, action_reason = "wait", "今日模式仅允许卖出"
    elif selection_mode == "reverse_t_only" and action == "sell" and playbook.name == "trend_sell_rebuy":
        action, action_reason = "wait", "今日模式偏逆T，不做顺T先卖"
    elif selection_mode == "trend_t_only" and action == "buy" and playbook.name in ("panic_reverse_t", "reverse_t"):
        action, action_reason = "wait", "今日模式偏顺T，不做逆T低吸"
    elif selection_mode == "observe_only":
        action, action_reason = "wait", "今日模式仅观察"
    score = 30
    reasons = [action_reason]
    if analysis.confidence == "高":
        score += 25
        reasons.append("分时共振强")
    elif analysis.confidence == "中":
        score += 15
        reasons.append("分时共振中等")
    if analysis.t_spread_pct >= 2.0:
        score += 20
        reasons.append(f"价差{analysis.t_spread_pct:.1f}%")
    elif analysis.t_spread_pct >= 1.2:
        score += 10
        reasons.append(f"价差{analysis.t_spread_pct:.1f}%")
    if analysis.structure == "trend_up":
        score += 10
        reasons.append("趋势回踩结构")
    elif analysis.structure == "range":
        score += 8
        reasons.append("箱体回转结构")
    elif analysis.structure == "panic_rebound":
        score += 12
        reasons.append("恐慌反抽结构")
    elif analysis.structure == "trend_down":
        score -= 15
        reasons.append("结构偏弱")
    if market_regime.regime == "strong" and action == "sell":
        score -= 8
        reasons.append("强市减少卖出冲动")
    if market_regime.regime in ("weak", "panic") and action == "buy":
        score -= 5
        reasons.append("弱市买入需更谨慎")
    if learning.bias == "aggressive":
        score += 8
        reasons.append("近端实盘反馈正向")
    elif learning.bias == "defensive":
        score -= 10
        reasons.append("近端实盘反馈偏弱")
    if learning.preferred_structure and learning.preferred_structure == analysis.structure:
        score += 6
        reasons.append(f"结构复盘偏好({analysis.structure})")
    score = max(0, min(score, 100))
    risk = evaluate_risk(
        rule,
        playbook,
        market_regime,
        portfolio_state,
        state,
        action,
        score,
        planned_buy_shares=planned_buy_shares,
        planned_sell_shares=planned_sell_shares,
    )
    execution_price = float(quote.get("price", 0) or 0)
    trigger_price = analysis.t_buy_target if action == "buy" else (analysis.t_sell_target if action == "sell" else 0.0)
    target_price = analysis.t_sell_target if action == "buy" else (analysis.t_buy_target if action == "sell" else 0.0)
    stop_price = 0.0
    if action == "buy":
        stop_floor = max(analysis.t_buy_target - analysis.risk_unit * 1.2, 0.0) if analysis.t_buy_target > 0 else 0.0
        stop_price = max(rule.stop_loss, stop_floor) if stop_floor > 0 else rule.stop_loss
    elif action == "sell" and analysis.t_sell_target > 0:
        stop_price = max(analysis.t_sell_target + analysis.risk_unit * 0.8, execution_price * 1.01)
    hold_minutes = 90 if action == "buy" else 60 if action == "sell" else 0
    level = _calc_level(score)
    if not risk.allowed:
        action = "wait"
    return DecisionResult(
        action=action,
        score=score,
        level=level,
        reason=" / ".join(reasons + ([risk.reason] if risk.reason else [])),
        playbook_name=playbook.name,
        trigger_price=round(trigger_price, 2) if trigger_price else 0.0,
        execution_price=round(execution_price, 2) if execution_price else 0.0,
        target_price=round(target_price, 2) if target_price else 0.0,
        stop_price=round(stop_price, 2) if stop_price else 0.0,
        hold_minutes=hold_minutes,
        allow_auto_trade=risk.allow_auto_trade and risk.allowed,
        risk_flags=risk.risk_flags,
    )


def format_decision_message(
    rule: StockRule,
    decision: DecisionResult,
    market_regime: MarketRegimeReport,
    learning: LearningProfile,
    analysis: IntradayAnalysis,
) -> str:
    action_text = {"buy": "T买", "sell": "T卖", "wait": "观望"}.get(decision.action, decision.action)
    trigger_line = f"{decision.trigger_price:.2f}" if decision.trigger_price else "-"
    target_line = f"{decision.target_price:.2f}" if decision.target_price else "-"
    stop_line = f"{decision.stop_price:.2f}" if decision.stop_price else "-"
    risk_line = ",".join(decision.risk_flags) if decision.risk_flags else "none"
    return (
        f"📌 体系化做T决策\n"
        f"股票：{rule.name} ({rule.code})\n"
        f"Playbook：{decision.playbook_name}\n"
        f"市场状态：{market_regime.regime} / 风险{market_regime.risk_level}\n"
        f"结构：{analysis.structure} / 波动单位{analysis.risk_unit:.3f}\n"
        f"建议动作：{action_text} {rule.per_trade_shares if decision.action in ('buy', 'sell') else 0}股\n"
        f"触发价：{trigger_line}\n"
        f"当前执行价：{decision.execution_price:.2f}\n"
        f"目标价：{target_line}\n"
        f"失效价：{stop_line}\n"
        f"评分：{decision.score}/100 ({decision.level})\n"
        f"原因：{decision.reason}\n"
        f"预计持有：{decision.hold_minutes}分钟\n"
        f"自动交易：{'是' if decision.allow_auto_trade else '否'}\n"
        f"学习：样本{learning.sample_count} 胜率{learning.win_rate:.0%} 均值{learning.avg_profit:.2f} 风险系数{learning.risk_factor:.2f}"
        f"{' 偏好结构' + learning.preferred_structure if learning.preferred_structure else ''}\n"
        f"风险标记：{risk_line}"
    )

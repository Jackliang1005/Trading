from typing import Dict

from .models import MarketContext, MarketRegimeReport


def build_market_regime(market: MarketContext) -> MarketRegimeReport:
    avg = market.avg_change_pct
    weakest = min(market.index_changes.values()) if market.index_changes else 0.0
    strongest = max(market.index_changes.values()) if market.index_changes else 0.0
    triggers = []
    regime = "neutral"
    risk_level = "medium"
    score = 50
    allow_buy = True
    allow_sell = True
    allow_reverse_t = False

    if avg <= -2.0 or weakest <= -3.0:
        regime = "panic"
        risk_level = "high"
        score = 20
        allow_buy = True
        allow_sell = True
        allow_reverse_t = True
        triggers.append("指数进入恐慌区")
    elif avg <= -1.0 or weakest <= -2.0:
        regime = "weak"
        risk_level = "high"
        score = 35
        allow_reverse_t = False
        triggers.append("指数明显走弱")
    elif avg >= 1.2 or strongest >= 2.0:
        regime = "strong"
        risk_level = "low"
        score = 75
        allow_reverse_t = False
        triggers.append("指数共振偏强")
    else:
        regime = "range"
        risk_level = "medium"
        score = 55
        allow_reverse_t = False
        triggers.append("指数震荡")

    summary = (
        f"市场regime={regime} risk={risk_level} score={score} "
        f"allow_buy={'yes' if allow_buy else 'no'} allow_sell={'yes' if allow_sell else 'no'} "
        f"allow_reverse_t={'yes' if allow_reverse_t else 'no'}"
    )
    return MarketRegimeReport(
        regime=regime,
        risk_level=risk_level,
        score=score,
        allow_buy=allow_buy,
        allow_sell=allow_sell,
        allow_reverse_t=allow_reverse_t,
        summary=summary,
        triggers=triggers,
    )

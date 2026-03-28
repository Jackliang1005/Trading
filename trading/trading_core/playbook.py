from .models import Playbook, StockRule


def select_playbook(rule: StockRule) -> Playbook:
    strategy = rule.strategy or ""
    if "顺T" in strategy:
        return Playbook(
            name="trend_sell_rebuy",
            style="顺T",
            priority=90,
            allow_buy=True,
            allow_sell=True,
            prefer_first="sell",
            max_round_trips=1,
            notes=["优先先卖后买", "不追高补仓"],
        )
    if "逆T" in strategy:
        return Playbook(
            name="panic_reverse_t" if rule.allow_market_panic_reverse_t else "reverse_t",
            style="逆T",
            priority=70,
            allow_buy=True,
            allow_sell=True,
            prefer_first="buy",
            max_round_trips=1,
            notes=["只做一次反抽", "承接失败就撤"],
        )
    if "箱体" in strategy:
        return Playbook(
            name="box_range_t",
            style="箱体",
            priority=80,
            allow_buy=True,
            allow_sell=True,
            prefer_first="wait",
            max_round_trips=2,
            notes=["只做上下沿", "中间位置不动"],
        )
    return Playbook(
        name="observe_only",
        style="观察",
        priority=20,
        allow_buy=False,
        allow_sell=True,
        prefer_first="wait",
        max_round_trips=0,
        notes=["默认观察，不主动开仓"],
    )

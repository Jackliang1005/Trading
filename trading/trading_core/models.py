from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass
class IntradayAnalysis:
    vwap: float
    pivot_highs: List[Tuple[float, str]]
    pivot_lows: List[Tuple[float, str]]
    yesterday_high: float
    yesterday_low: float
    yesterday_close: float
    yesterday_pp: float
    yesterday_s1: float
    yesterday_r1: float
    ma20: float
    ma60: float
    t_buy_target: float
    t_sell_target: float
    t_spread_pct: float
    confidence: str
    bar_count: int


@dataclass
class StockRule:
    code: str
    name: str
    cost_price: float
    base_position: int
    per_trade_shares: int
    buy_range: Tuple[float, float]
    sell_range: Tuple[float, float]
    stop_loss: float
    strategy: str
    note: str
    watch_mode: str = ""
    preopen_risk_mode: str = ""
    avoid_reverse_t: bool = False
    abandon_buy_below: float = 0.0
    allow_rebound_watch_after_stop: bool = False
    rebound_buy_above: float = 0.0
    allow_market_panic_reverse_t: bool = True
    panic_rebound_pct: float = 0.8
    sector_tags: List[str] = None
    enabled: bool = True


@dataclass
class MarketContext:
    regime: str
    avg_change_pct: float
    index_changes: Dict[str, float]


@dataclass
class LearningProfile:
    sample_count: int
    win_rate: float
    avg_profit: float
    bias: str = "neutral"


@dataclass
class MarketRegimeReport:
    regime: str
    risk_level: str
    score: int
    allow_buy: bool
    allow_sell: bool
    allow_reverse_t: bool
    summary: str
    triggers: List[str]


@dataclass
class Playbook:
    name: str
    style: str
    priority: int
    allow_buy: bool
    allow_sell: bool
    prefer_first: str
    max_round_trips: int
    notes: List[str]


@dataclass
class RiskDecision:
    allowed: bool
    allow_auto_trade: bool
    reason: str
    risk_flags: List[str]


@dataclass
class DecisionResult:
    action: str
    score: int
    level: str
    reason: str
    playbook_name: str
    trigger_price: float
    execution_price: float
    target_price: float
    stop_price: float
    hold_minutes: int
    allow_auto_trade: bool
    risk_flags: List[str]


@dataclass
class StockDataSnapshot:
    code: str
    name: str
    price: float
    change_percent: float
    amount: float
    high: float
    low: float
    open: float
    pre_close: float
    amplitude_pct: float
    main_net_inflow: float
    flow_bias: str


@dataclass
class StockNewsSnapshot:
    code: str
    stock_score: int
    stock_sentiment: str
    stock_items: List[Dict]
    sector_score: int
    sector_sentiment: str
    sector_items: List[Dict]
    macro_score: int
    macro_sentiment: str
    macro_items: List[Dict]
    event_tags: List[Dict]


@dataclass
class SelectionResult:
    code: str
    name: str
    action: str
    am_mode: str
    pm_mode: str
    score: int
    level: str
    buy_budget_amount: float
    sell_budget_amount: float
    suggested_t_ratio: float
    suggested_buy_shares: int
    suggested_sell_shares: int
    reason: str
    explanation: str
    regime: str

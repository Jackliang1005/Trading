from dataclasses import dataclass, field
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
    structure: str = "neutral"
    risk_unit: float = 0.0
    intraday_trend_pct: float = 0.0
    day_range_pct: float = 0.0
    forecast_source: str = ""
    forecast_enabled: bool = False
    forecast_horizon: int = 0
    forecast_end_price: float = 0.0
    forecast_high_price: float = 0.0
    forecast_low_price: float = 0.0
    forecast_return_pct: float = 0.0
    forecast_bias: str = "neutral"
    forecast_confidence: str = "低"
    forecast_summary: str = ""


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
    buy_blocked: bool = False
    buy_block_reason: str = ""


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
    risk_factor: float = 1.0
    entry_tolerance: float = 1.0
    preferred_structure: str = ""


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
    forecast_bias: str = "neutral"
    forecast_return_pct: float = 0.0


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
    overseas_peer_score: int = 0
    overseas_peer_sentiment: str = "neutral"
    overseas_peer_items: List[Dict] = field(default_factory=list)
    overseas_peer_block_buy: bool = False
    overseas_peer_block_reason: str = ""


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
    buy_blocked: bool = False
    buy_block_reason: str = ""
    learning_risk_factor: float = 1.0
    learning_preferred_structure: str = ""
    score_breakdown: Dict = field(default_factory=dict)

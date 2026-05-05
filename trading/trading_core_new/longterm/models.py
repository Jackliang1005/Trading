from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Literal, Optional

logger = logging.getLogger(__name__)


CandidateStatus = Literal["candidate", "watch", "active", "exit", "cooldown"]
PlanAction = Literal["buy", "sell", "hold"]


@dataclass
class StockCandidate:
    code: str
    name: str
    status: CandidateStatus = "candidate"
    value_score: float = 50.0
    quality_score: float = 50.0
    growth_score: float = 50.0
    risk_score: float = 50.0
    industry: str = "UNKNOWN"
    thesis: str = ""
    tags: List[str] = field(default_factory=list)
    updated_at: str = ""

    @property
    def composite_score(self) -> float:
        base = self.value_score * 0.30 + self.quality_score * 0.30 + self.growth_score * 0.25
        risk_penalty = (100.0 - self.risk_score) * 0.15
        return max(0.0, min(100.0, base + risk_penalty))


@dataclass
class SimPosition:
    code: str
    name: str
    quantity: int
    cost_price: float
    last_price: float = 0.0

    @property
    def market_value(self) -> float:
        return max(0.0, float(self.quantity) * float(self.last_price or self.cost_price))


@dataclass
class PortfolioState:
    as_of: str
    initial_capital: float
    cash: float
    available_cash: float = 0.0
    frozen_cash: float = 0.0
    positions: List[SimPosition] = field(default_factory=list)
    target_weights: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cash = float(self.cash or 0.0)
        raw_frozen = float(self.frozen_cash or 0.0)
        self.frozen_cash = max(0.0, raw_frozen)
        if raw_frozen < 0:
            logger.warning("PortfolioState frozen_cash < 0, clamped to 0: raw=%s", raw_frozen)
        if self.available_cash <= 0:
            if self.frozen_cash > self.cash:
                logger.warning(
                    "PortfolioState frozen_cash > cash, available_cash will be clamped to 0: frozen=%s cash=%s",
                    self.frozen_cash,
                    self.cash,
                )
            self.available_cash = max(0.0, self.cash - self.frozen_cash)
        else:
            self.available_cash = max(0.0, float(self.available_cash))
        if self.available_cash > self.cash:
            logger.warning(
                "PortfolioState available_cash > cash, clamped to cash: available=%s cash=%s",
                self.available_cash,
                self.cash,
            )
            self.available_cash = self.cash

    @property
    def holdings_value(self) -> float:
        return sum(item.market_value for item in self.positions)

    @property
    def nav(self) -> float:
        return self.cash + self.holdings_value


@dataclass
class RebalanceActionItem:
    code: str
    name: str
    action: PlanAction
    reference_price: float
    target_weight: float
    current_weight: float
    delta_shares: int
    estimated_amount: float
    score: float
    reason: str


@dataclass
class RebalanceRejectedItem:
    code: str
    name: str
    action: PlanAction
    reference_price: float
    delta_shares: int
    estimated_amount: float
    reason: str


@dataclass
class RebalancePlan:
    plan_id: str
    trade_date: str
    source: str
    constraints: Dict[str, float]
    actions: List[RebalanceActionItem] = field(default_factory=list)
    rejected_actions: List[RebalanceRejectedItem] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass
class ManualExecutionItem:
    trade_date: str
    code: str
    name: str
    side: Literal["buy", "sell"]
    price: float
    quantity: int
    fee: float = 0.0
    note: str = ""


@dataclass
class LongTermSettings:
    score_value_weight: float = 0.30
    score_quality_weight: float = 0.30
    score_growth_weight: float = 0.25
    score_risk_weight: float = 0.15
    max_holdings: int = 12
    single_name_cap: float = 0.20
    cash_buffer_ratio: float = 0.08
    rebalance_threshold: float = 0.02
    max_industry_weight: float = 0.40
    industry_cap_mode: str = "single"  # single | tiered
    core_industry_cap: float = 0.50
    satellite_industry_cap: float = 0.30
    core_industries: List[str] = field(
        default_factory=lambda: [
            "银行",
            "非银金融",
            "计算机",
            "医药生物",
            "食品饮料",
            "电力设备",
        ]
    )
    max_theme_weight: float = 0.60
    theme_cap_mode: str = "single"  # single | tiered
    core_theme_cap: float = 0.75
    satellite_theme_cap: float = 0.40
    core_themes: List[str] = field(default_factory=lambda: ["AI算力", "半导体", "新能源"])
    min_trade_amount: float = 3000.0
    max_portfolio_volatility: float = 0.28
    max_portfolio_drawdown: float = 0.32
    # Post-market scanner thresholds (configurable)
    scan_atr_min: float = 0.04
    scan_rsi_min: float = 55.0
    scan_rsi_max: float = 70.0
    scan_turnover_min: float = 5.0
    scan_turnover_max: float = 20.0
    scan_bias_ma5_abs_max: float = 0.05
    scan_volume_ma5_multiplier: float = 1.2
    scan_heat_bonus_cap: float = 8.0
    scan_heat_bonus_scale: float = 0.08
    scan_fallback_change_bonus_floor: float = -3.0
    scan_fallback_change_bonus_cap: float = 5.0
    scan_fallback_change_divisor: float = 2.0
    scan_fallback_amount_divisor: float = 200_000_000.0
    scan_fallback_amount_bonus_cap: float = 6.0
    scan_fallback_volume_divisor: float = 10_000_000.0
    scan_fallback_volume_bonus_cap: float = 3.0
    # THS hotness score coefficients
    ths_heat_limit_up_weight: float = 2.0
    ths_heat_change_weight: float = 18.0
    # Heat Rotation Strategy (rotation_mode)
    rotation_mode: bool = False
    max_themes: int = 5
    max_concepts: int = 20         # how many concepts to fetch/track
    max_per_theme: int = 2
    rotation_max_holdings: int = 5      # max hold in bull markets
    max_holdings_bear: int = 3         # reduced in bear market
    max_hold_days: int = 40        # was 15; wider hold for trend following
    min_heat_entry: float = 15.0   # was 40.0; lower bar with RSI + trend filters
    max_heat_entry: float = 75.0   # skip overbought stocks
    rsi_max_entry: float = 75.0    # RSI > 75 = skip
    heat_exit_decay: float = 0.35  # was 0.6; less aggressive decay exit
    trailing_stop_pct: float = 0.18 # was 0.08; wider stop
    trend_break_pct: float = 0.03  # 3% below MA10 to trigger trend_break
    hrs_heat_accel_w: float = 0.20 # was 0.35; less weight on heat accel
    hrs_sector_mom_w: float = 0.10 # was 0.25
    hrs_price_trend_w: float = 0.40 # was 0.20; more weight on trend quality
    hrs_liquidity_w: float = 0.30  # was 0.20; more weight on liquidity
    heat_accel_window: int = 5
    entry_delay_days: int = 0
    rebalance_freq: int = 10       # trading days between rebalances
    exclude_star_board: bool = True # exclude 688xxx (can't market-order)
    market_trend_index: str = "000905.XSHG"  # CSI 500 for trend detection
    # GEM small-cap universe (创业板小市值)
    gem_universe: bool = False               # enable GEM-only universe
    gem_board_prefix: str = "30"             # GEM code prefix
    gem_market_cap_min: float = 5.0          # 最小市值 (亿)
    gem_market_cap_max: float = 50.0         # 最大市值 (亿)
    gem_revenue_yoy_min: float = 0.15        # 营收同比增长最低
    gem_min_listing_days: int = 375          # 上市最少天数
    gem_universe_limit: int = 200            # 候选池上限
    gem_rank_start: int = 10                 # 跳过前N只 (最小市值可能有问题)
    gem_final_pool_size: int = 20            # 最终候选池大小
    # Data lifecycle retention
    retention_plan_days: int = 180
    retention_scan_days: int = 120
    retention_decision_days: int = 180
    retention_keep_min_files: int = 30
    retention_delete_tmp_older_than_hours: int = 24


def dataclass_to_dict(obj):
    return asdict(obj)


def now_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")

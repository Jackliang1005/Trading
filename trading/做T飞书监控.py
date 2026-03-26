#!/usr/bin/env python3
"""
做T半自动监控

功能:
1. 调用本地东方财富行情脚本获取实时价格
2. 按配置判断买入 / 卖出 / 风险提醒
3. 通过 qmt2http 分钟线做首次穿越边缘检测, 避免区间内反复报警
4. 触发止损/风险信号时, 自动搜索东财妙想资讯附带异动原因
5. 通过 openclaw message send 发送飞书通知

说明:
- 这是半自动提醒, 不会自动下单
- 通知只代表触发到你的预设区间, 不代表必须成交
"""

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


BASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = BASE_DIR.parent / "skills" / "eastmoney-quotes"
STATE_PATH = BASE_DIR / "trading_data" / "monitor_state.json"
DEFAULT_CONFIG = BASE_DIR / "做T监控配置.json"
DAILY_PLAN_PATH = BASE_DIR / "今日交易计划.json"

# qmt2http 配置
QMT2HTTP_BASE_URL = os.environ.get("QMT2HTTP_BASE_URL", "http://150.158.31.115")
QMT2HTTP_API_TOKEN = os.environ.get("QMT2HTTP_API_TOKEN", "")
QMT2HTTP_TIMEOUT = int(os.environ.get("QMT2HTTP_TIMEOUT", "10"))

# 东方财富妙想配置
MX_APIKEY = os.environ.get("MX_APIKEY", "")
MX_API_BASE = "https://mkapi2.dfcfs.com/finskillshub"


sys.path.insert(0, str(SKILL_DIR))
from eastmoney_quotes import get_quotes  # type: ignore


# ==============================================================================
# qmt2http 分钟线：首次穿越检测
# ==============================================================================

def _qmt_code(code: str) -> str:
    """6位数字代码 → QMT代码格式 (600519 → 600519.SH)"""
    if "." in code:
        return code
    if code.startswith(("6", "5", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _qmt_request(path: str, method: str = "POST", payload: dict = None) -> Optional[dict]:
    """发送 qmt2http 请求，失败静默返回 None（不阻断主流程）"""
    url = f"{QMT2HTTP_BASE_URL}{path}"
    data = json.dumps(payload).encode() if payload else None
    headers = {"Content-Type": "application/json"}
    if QMT2HTTP_API_TOKEN:
        headers["Authorization"] = f"Bearer {QMT2HTTP_API_TOKEN}"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=QMT2HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except (HTTPError, URLError, OSError):
        return None


def fetch_minute_prices(code: str, lookback: int = 5) -> Optional[List[float]]:
    """
    从 qmt2http 获取最近 N 根分钟线的收盘价。
    返回价格列表（从旧到新），失败返回 None。
    """
    now = datetime.now()
    # 计算 lookback 分钟前的时间作为起始
    start_h = now.hour
    start_m = max(now.minute - lookback, 0)
    if now.minute < lookback:
        start_h = max(start_h - 1, 9)
        start_m = 60 - (lookback - now.minute)
    start_hm = f"{start_h:02d}:{start_m:02d}"

    result = _qmt_request("/rpc/data_fetcher", payload={
        "method": "get_intraday_minute_data",
        "params": {
            "code": _qmt_code(code),
            "date_str": now.strftime("%Y%m%d"),
            "start_hm": start_hm,
        }
    })
    if not result or not result.get("success"):
        return None

    data = result.get("data")
    if not data:
        return None

    # data 可能是 list[dict] 或 dict，提取收盘价
    prices = []
    if isinstance(data, list):
        for bar in data:
            if isinstance(bar, dict):
                p = bar.get("close", bar.get("lastPrice", bar.get("price")))
                if p is not None:
                    prices.append(float(p))
            elif isinstance(bar, (int, float)):
                prices.append(float(bar))
    elif isinstance(data, dict):
        # 可能是 {code: [bars]} 格式
        for key, val in data.items():
            if isinstance(val, list):
                for bar in val:
                    if isinstance(bar, dict):
                        p = bar.get("close", bar.get("lastPrice"))
                        if p is not None:
                            prices.append(float(p))
                break

    return prices if prices else None


def fetch_full_intraday_bars(code: str) -> Optional[List[Dict]]:
    """
    获取当日全部1分钟K线（从09:30到当前），返回完整 OHLCV。
    返回 List[Dict]，每个 dict 含 time, open, high, low, close, volume。
    失败返回 None，不阻断主流程。
    """
    now = datetime.now()
    result = _qmt_request("/rpc/data_fetcher", payload={
        "method": "get_intraday_minute_data",
        "params": {
            "code": _qmt_code(code),
            "date_str": now.strftime("%Y%m%d"),
            "start_hm": "09:30",
        }
    })
    if not result or not result.get("success"):
        return None

    data = result.get("data")
    if not data:
        return None

    bars = []
    raw_list = None
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        # 可能是 {code: [bars]} 格式
        for key, val in data.items():
            if isinstance(val, list):
                raw_list = val
                break

    if not raw_list:
        return None

    for bar in raw_list:
        if not isinstance(bar, dict):
            continue
        b = {
            "time": bar.get("time", bar.get("datetime", "")),
            "open": _safe_float(bar.get("open", bar.get("openPrice"))) or 0.0,
            "high": _safe_float(bar.get("high", bar.get("highPrice"))) or 0.0,
            "low": _safe_float(bar.get("low", bar.get("lowPrice"))) or 0.0,
            "close": _safe_float(bar.get("close", bar.get("lastPrice", bar.get("price")))) or 0.0,
            "volume": _safe_float(bar.get("volume", bar.get("vol"))) or 0.0,
        }
        if b["close"] > 0:
            bars.append(b)

    return bars if bars else None


def fetch_yesterday_daily(code: str) -> Optional[Dict]:
    """
    获取前日日K线数据（high/low/close）。
    调用 get_history_data，period=1d, count=2。
    返回 Dict 含 high, low, close，失败返回 None。
    """
    result = _qmt_request("/rpc/data_fetcher", payload={
        "method": "get_history_data",
        "params": {
            "code": _qmt_code(code),
            "period": "1d",
            "count": 2,
        }
    })
    if not result or not result.get("success"):
        return None

    data = result.get("data")
    if not data:
        return None

    raw_list = None
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, list):
                raw_list = val
                break

    if not raw_list or not raw_list:
        return None

    # 取倒数第二根（前日），如果只有一根就取那一根
    bar = raw_list[-2] if len(raw_list) >= 2 else raw_list[-1]
    if not isinstance(bar, dict):
        return None

    high = _safe_float(bar.get("high", bar.get("highPrice")))
    low = _safe_float(bar.get("low", bar.get("lowPrice")))
    close = _safe_float(bar.get("close", bar.get("lastPrice")))

    if high is None or low is None or close is None:
        return None

    return {"high": high, "low": low, "close": close}


def fetch_auction_snapshot(code: str) -> Optional[Dict]:
    """
    从 qmt2http 获取单只股票当日竞价数据，并统一常用字段。
    """
    result = _qmt_request("/rpc/data_fetcher", payload={
        "method": "get_auction_data",
        "params": {
            "codes": [_qmt_code(code)],
            "date": datetime.now().strftime("%Y%m%d"),
        }
    })
    if not result or not result.get("success"):
        return None

    data = result.get("data")
    if not data:
        return None

    item = None
    if isinstance(data, list) and data:
        item = data[0]
    elif isinstance(data, dict):
        qmt_code = _qmt_code(code)
        if qmt_code in data:
            item = data[qmt_code]
        elif code in data:
            item = data[code]
        elif len(data) == 1:
            item = next(iter(data.values()))

    if not isinstance(item, dict):
        return None

    auction_price = (
        _safe_float(item.get("auction_price"))
        or _safe_float(item.get("match_price"))
        or _safe_float(item.get("current_price"))
        or _safe_float(item.get("price"))
        or _safe_float(item.get("open_price"))
        or _safe_float(item.get("open"))
    )
    pre_close = (
        _safe_float(item.get("pre_close"))
        or _safe_float(item.get("prev_close"))
        or _safe_float(item.get("yclose"))
        or _safe_float(item.get("last_close"))
    )
    change_percent = (
        _safe_float(item.get("change_percent"))
        or _safe_float(item.get("pct_chg"))
        or _safe_float(item.get("change_pct"))
        or _safe_float(item.get("chg_pct"))
    )
    if change_percent is None and auction_price is not None and pre_close:
        change_percent = (auction_price - pre_close) / pre_close * 100

    volume = (
        _safe_float(item.get("volume"))
        or _safe_float(item.get("auction_volume"))
        or _safe_float(item.get("matched_volume"))
    )

    if auction_price is None and change_percent is None:
        return None

    return {
        "auction_price": auction_price,
        "pre_close": pre_close,
        "change_percent": change_percent,
        "volume": volume,
        "raw": item,
    }


def classify_zone(price: float, rule) -> str:
    """将价格分类到所处区域"""
    if price <= rule.stop_loss:
        return "risk"
    if rule.buy_range[0] <= price <= rule.buy_range[1]:
        return "buy"
    if rule.sell_range[0] <= price <= rule.sell_range[1]:
        return "sell"
    if price < rule.buy_range[0]:
        return "below_buy"
    if rule.buy_range[1] < price < rule.sell_range[0]:
        return "between"
    return "above_sell"


# ==============================================================================
# 分时分析引擎：VWAP、枢轴、均线、综合做T目标
# ==============================================================================

@dataclass
class IntradayAnalysis:
    vwap: float
    pivot_highs: List[Tuple[float, str]]   # [(price, time), ...]
    pivot_lows: List[Tuple[float, str]]    # [(price, time), ...]
    yesterday_high: float
    yesterday_low: float
    yesterday_close: float
    yesterday_pp: float                     # 前日经典枢轴 Pivot Point
    yesterday_s1: float                     # 前日支撑1
    yesterday_r1: float                     # 前日压力1
    ma20: float                             # 20分钟均线
    ma60: float                             # 60分钟均线
    t_buy_target: float                     # 综合做T买入参考价
    t_sell_target: float                    # 综合做T卖出参考价
    t_spread_pct: float                     # 预期价差百分比
    confidence: str                         # 高/中/低
    bar_count: int                          # 已分析K线数


def _find_pivots(bars: List[Dict], window: int = 3) -> Tuple[List[Tuple[float, str]], List[Tuple[float, str]]]:
    """
    滑窗法检测分时K线的局部高点和低点。
    window=3 表示一个点需要比左右各 window 根K线的 high/low 都高/低。
    """
    highs = []
    lows = []
    n = len(bars)
    for i in range(window, n - window):
        is_high = True
        is_low = True
        for j in range(1, window + 1):
            if bars[i]["high"] <= bars[i - j]["high"] or bars[i]["high"] <= bars[i + j]["high"]:
                is_high = False
            if bars[i]["low"] >= bars[i - j]["low"] or bars[i]["low"] >= bars[i + j]["low"]:
                is_low = False
        if is_high:
            highs.append((bars[i]["high"], str(bars[i].get("time", ""))))
        if is_low:
            lows.append((bars[i]["low"], str(bars[i].get("time", ""))))
    return highs, lows


def _compute_ma(bars: List[Dict], period: int) -> float:
    """计算最近 period 根K线的收盘价均线，数据不足时用全部数据。"""
    if not bars:
        return 0.0
    subset = bars[-period:] if len(bars) >= period else bars
    closes = [b["close"] for b in subset]
    return sum(closes) / len(closes)


def _weighted_avg(values_weights: List[Tuple[float, float]]) -> float:
    """加权平均，跳过值为 0 的项。"""
    total_w = 0.0
    total_v = 0.0
    for val, weight in values_weights:
        if val > 0:
            total_v += val * weight
            total_w += weight
    return total_v / total_w if total_w > 0 else 0.0


def _assess_confidence(values: List[float], threshold_pct: float = 0.5) -> str:
    """
    评估多个价位指标的聚集程度。
    3个以上指标在 threshold_pct% 范围内聚集 → 高
    2个聚集 → 中
    否则 → 低
    """
    valid = [v for v in values if v > 0]
    if len(valid) < 2:
        return "低"
    cluster_count = 0
    for i in range(len(valid)):
        for j in range(i + 1, len(valid)):
            mid = (valid[i] + valid[j]) / 2
            if mid > 0 and abs(valid[i] - valid[j]) / mid * 100 <= threshold_pct:
                cluster_count += 1
    if cluster_count >= 3:
        return "高"
    elif cluster_count >= 1:
        return "中"
    return "低"


def compute_intraday_analysis(
    code: str,
    rule: "StockRule",
    bars: List[Dict],
    yesterday: Optional[Dict],
) -> Optional[IntradayAnalysis]:
    """
    核心分时分析函数。
    bars: 当日1分钟K线列表 (OHLCV)
    yesterday: 前日日K线 {high, low, close}，可为 None
    """
    if not bars or len(bars) < 15:
        return None

    # 1. VWAP: sum(typical_price * volume) / sum(volume)
    total_tp_vol = 0.0
    total_vol = 0.0
    for bar in bars:
        tp = (bar["high"] + bar["low"] + bar["close"]) / 3
        vol = bar["volume"]
        total_tp_vol += tp * vol
        total_vol += vol
    vwap = total_tp_vol / total_vol if total_vol > 0 else bars[-1]["close"]

    # 2. 枢轴点检测
    pivot_highs, pivot_lows = _find_pivots(bars, window=3)

    # 3. 前日枢轴
    yd_high = yesterday["high"] if yesterday else 0.0
    yd_low = yesterday["low"] if yesterday else 0.0
    yd_close = yesterday["close"] if yesterday else 0.0
    if yd_high > 0 and yd_low > 0 and yd_close > 0:
        pp = (yd_high + yd_low + yd_close) / 3
        s1 = 2 * pp - yd_high
        r1 = 2 * pp - yd_low
    else:
        pp = s1 = r1 = 0.0

    # 4. 分钟均线
    ma20 = _compute_ma(bars, 20)
    ma60 = _compute_ma(bars, 60)

    # 5. 综合目标价
    # 支撑侧：最近支撑枢轴, VWAP下轨, 前日S1, MA支撑
    recent_support = pivot_lows[-1][0] if pivot_lows else 0.0
    vwap_lower = vwap * 0.995  # VWAP 下方 0.5%
    ma_support = min(ma20, ma60) if ma20 > 0 and ma60 > 0 else (ma20 or ma60)

    t_buy_target = _weighted_avg([
        (recent_support, 0.30),
        (vwap_lower, 0.25),
        (s1, 0.25),
        (ma_support, 0.20),
    ])

    # 压力侧：最近压力枢轴, VWAP上轨, 前日R1, MA压力
    recent_resistance = pivot_highs[-1][0] if pivot_highs else 0.0
    vwap_upper = vwap * 1.005  # VWAP 上方 0.5%
    ma_resistance = max(ma20, ma60) if ma20 > 0 and ma60 > 0 else (ma20 or ma60)

    t_sell_target = _weighted_avg([
        (recent_resistance, 0.30),
        (vwap_upper, 0.25),
        (r1, 0.25),
        (ma_resistance, 0.20),
    ])

    # 价差百分比
    if t_buy_target > 0 and t_sell_target > t_buy_target:
        t_spread_pct = (t_sell_target - t_buy_target) / t_buy_target * 100
    else:
        t_spread_pct = 0.0

    # 6. 置信度
    buy_indicators = [v for v in [recent_support, vwap_lower, s1, ma_support] if v > 0]
    sell_indicators = [v for v in [recent_resistance, vwap_upper, r1, ma_resistance] if v > 0]
    buy_conf = _assess_confidence(buy_indicators)
    sell_conf = _assess_confidence(sell_indicators)
    # 综合置信度取较低的
    conf_rank = {"高": 2, "中": 1, "低": 0}
    overall_conf = buy_conf if conf_rank.get(buy_conf, 0) <= conf_rank.get(sell_conf, 0) else sell_conf

    return IntradayAnalysis(
        vwap=round(vwap, 2),
        pivot_highs=pivot_highs,
        pivot_lows=pivot_lows,
        yesterday_high=yd_high,
        yesterday_low=yd_low,
        yesterday_close=yd_close,
        yesterday_pp=round(pp, 2),
        yesterday_s1=round(s1, 2),
        yesterday_r1=round(r1, 2),
        ma20=round(ma20, 2),
        ma60=round(ma60, 2),
        t_buy_target=round(t_buy_target, 2),
        t_sell_target=round(t_sell_target, 2),
        t_spread_pct=round(t_spread_pct, 1),
        confidence=overall_conf,
        bar_count=len(bars),
    )


def adjust_for_strategy(analysis: IntradayAnalysis, rule: "StockRule") -> IntradayAnalysis:
    """
    根据策略差异化调整分析结果。
    - 顺T优先: T-buy 需确认回落到 VWAP 下方
    - 轻仓逆T: T-buy 置信度降一级
    - 箱体震荡: 中间位置降低置信度
    - 趋势观察(light): 仅保留 T-sell 信号
    """
    strategy = rule.strategy or ""
    watch_mode = (rule.watch_mode or "").lower()
    conf_rank = {"高": 2, "中": 1, "低": 0}
    rank_conf = {2: "高", 1: "中", 0: "低"}

    if watch_mode == "light":
        # 趋势观察：不生成 T-buy 信号
        analysis.t_buy_target = 0.0
        analysis.confidence = "低"
    elif "顺T" in strategy:
        # 顺T优先：增强 T-sell 信号，T-buy 需回落到 VWAP 下方
        if analysis.t_buy_target > analysis.vwap:
            analysis.t_buy_target = round(analysis.vwap * 0.997, 2)
    elif "逆T" in strategy:
        # 轻仓逆T：T-buy 需缩量企稳确认，置信度降一级
        rank = conf_rank.get(analysis.confidence, 0)
        analysis.confidence = rank_conf.get(max(rank - 1, 0), "低")
    elif "箱体" in strategy:
        # 箱体震荡：如果买卖目标价差太小，降低置信度
        if analysis.t_spread_pct < 1.5:
            analysis.confidence = "低"

    return analysis


# ==============================================================================
# 动态区间 + T 信号格式化
# ==============================================================================

def get_analysis_phase(now: Optional[datetime] = None) -> str:
    """
    返回当前分时分析阶段:
    - bootstrap: 09:25-09:45 数据太少不分析
    - first_wave: 09:45-10:00 早盘第一波，数据开始够用
    - steady: 10:00-14:30 稳态分析阶段
    - winddown: 14:30-15:00 尾盘收敛
    - closed: 非交易时段
    """
    now = now or datetime.now()
    hm = now.hour * 100 + now.minute
    if hm < 925 or hm > 1500:
        return "closed"
    if hm <= 945:
        return "bootstrap"
    if hm <= 1000:
        return "first_wave"
    if hm <= 1430:
        return "steady"
    return "winddown"


def should_use_dynamic_range(rule: "StockRule", quote: Dict, threshold_pct: float = 8.0) -> bool:
    """
    判断是否用动态区间替代静态配置。
    条件：现价与配置的 buy_range 中点偏离超过 threshold_pct% 时，自动切换为动态区间。
    """
    price = _safe_float(quote.get("price"))
    if not price or price <= 0:
        return False
    buy_mid = (rule.buy_range[0] + rule.buy_range[1]) / 2
    if buy_mid <= 0:
        return False
    deviation = abs(price - buy_mid) / buy_mid * 100
    return deviation > threshold_pct


def format_t_signal(
    analysis: IntradayAnalysis,
    rule: "StockRule",
    quote: Dict,
    market: "MarketContext",
    is_dynamic: bool = False,
) -> str:
    """格式化独立 T 信号消息。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    price = float(quote.get("price", 0))
    change_pct = float(quote.get("change_percent", 0))

    dynamic_tag = " [动态区间]" if is_dynamic else ""

    # 分时支撑/压力展示（最近 3 个）
    support_lines = []
    for p, t in analysis.pivot_lows[-3:]:
        time_str = t.split(" ")[-1] if " " in t else t
        # 只取 HH:MM 部分
        if len(time_str) > 5:
            time_str = time_str[:5]
        support_lines.append(f"{p:.2f} ({time_str})")
    resistance_lines = []
    for p, t in analysis.pivot_highs[-3:]:
        time_str = t.split(" ")[-1] if " " in t else t
        if len(time_str) > 5:
            time_str = time_str[:5]
        resistance_lines.append(f"{p:.2f} ({time_str})")

    support_str = ", ".join(support_lines) if support_lines else "暂无"
    resistance_str = ", ".join(resistance_lines) if resistance_lines else "暂无"

    # 昨日参考
    yd_str = "暂无"
    if analysis.yesterday_high > 0:
        yd_str = f"高 {analysis.yesterday_high:.2f} / 低 {analysis.yesterday_low:.2f} / 收 {analysis.yesterday_close:.2f}"

    # 枢轴
    pivot_str = "暂无"
    if analysis.yesterday_pp > 0:
        pivot_str = f"S1={analysis.yesterday_s1:.2f} / PP={analysis.yesterday_pp:.2f} / R1={analysis.yesterday_r1:.2f}"

    # 动作建议
    action_lines = []
    if analysis.t_buy_target > 0 and price <= analysis.t_buy_target * 1.01:
        low_pivots = [p for p, _ in analysis.pivot_lows[-3:]]
        if low_pivots:
            zone_low = min(low_pivots)
            zone_high = max(low_pivots)
            action_lines.append(
                f"现价接近支撑共振区{zone_low:.1f}-{zone_high:.1f}，"
                f"分时企稳后可按{rule.per_trade_shares}股T买入，"
                f"目标VWAP附近{analysis.vwap:.1f}或压力{analysis.t_sell_target:.1f}卖出。"
            )
        else:
            action_lines.append(
                f"现价接近T买入参考{analysis.t_buy_target:.1f}，"
                f"分时企稳后可按{rule.per_trade_shares}股小仓买入。"
            )
    elif analysis.t_sell_target > 0 and price >= analysis.t_sell_target * 0.99:
        action_lines.append(
            f"现价接近T卖出参考{analysis.t_sell_target:.1f}，"
            f"可按{rule.per_trade_shares}股减仓或完成顺T，"
            f"回落到{analysis.t_buy_target:.1f}附近可再接回。"
        )
    else:
        action_lines.append(
            f"当前处于T买入{analysis.t_buy_target:.1f}和T卖出{analysis.t_sell_target:.1f}之间，"
            f"等待接近支撑或压力位再操作。"
        )

    action_str = "\n    ".join(action_lines)

    # T买入参考（趋势观察模式不显示）
    buy_line = ""
    if analysis.t_buy_target > 0:
        buy_conf = analysis.confidence
        buy_line = f"\n  T买入参考：{analysis.t_buy_target:.1f} 附近 (置信度: {buy_conf})"

    return (
        f"📊 做T信号\n"
        f"时间：{now}\n"
        f"股票：{rule.name} ({rule.code})\n"
        f"现价：{price:.2f} / 涨跌幅 {change_pct:+.2f}%\n"
        f"\n"
        f"分时分析 ({analysis.bar_count}根1分钟K线)：\n"
        f"  VWAP：{analysis.vwap:.2f}\n"
        f"  分时支撑：{support_str}\n"
        f"  分时压力：{resistance_str}\n"
        f"  昨日参考：{yd_str}\n"
        f"  枢轴：{pivot_str}\n"
        f"  MA20：{analysis.ma20:.2f} / MA60：{analysis.ma60:.2f}\n"
        f"\n"
        f"做T建议{dynamic_tag}：{buy_line}\n"
        f"  T卖出参考：{analysis.t_sell_target:.1f} 附近 (置信度: {analysis.confidence})\n"
        f"  预期价差：{analysis.t_spread_pct:.1f}%\n"
        f"  策略：{rule.strategy}\n"
        f"  动作：{action_str}\n"
        f"\n"
        f"{format_market_context(market)}\n"
        f"提示：仅为分时技术参考，请结合盘口确认。"
    )


def should_send_t_signal(state: Dict, code: str, cooldown_minutes: int, max_per_day: int) -> bool:
    """检查 T 信号是否可发送（独立冷却 + 每日上限）。"""
    intraday = state.setdefault("intraday", {})
    stock_data = intraday.get(code, {})
    today = datetime.now().strftime("%Y-%m-%d")

    # 日期不同则重置
    if stock_data.get("date") != today:
        return True

    # 每日上限
    if stock_data.get("t_signal_count", 0) >= max_per_day:
        return False

    # 冷却检查
    last_sent = stock_data.get("last_t_signal")
    if last_sent:
        try:
            last_time = datetime.fromisoformat(last_sent)
            delta_minutes = (datetime.now() - last_time).total_seconds() / 60
            if delta_minutes < cooldown_minutes:
                return False
        except ValueError:
            pass

    return True


def mark_t_signal_sent(state: Dict, code: str, analysis: IntradayAnalysis) -> None:
    """记录 T 信号发送状态。"""
    intraday = state.setdefault("intraday", {})
    today = datetime.now().strftime("%Y-%m-%d")
    stock_data = intraday.get(code, {})

    if stock_data.get("date") != today:
        stock_data = {"date": today, "t_signal_count": 0}

    stock_data["last_t_signal"] = datetime.now().isoformat(timespec="seconds")
    stock_data["t_signal_count"] = stock_data.get("t_signal_count", 0) + 1
    stock_data["t_buy_level"] = analysis.t_buy_target
    stock_data["t_sell_level"] = analysis.t_sell_target
    intraday[code] = stock_data


def is_first_crossing(code: str, rule, current_zone: str, state: Dict) -> bool:
    """
    判断当前信号是否为"首次穿越"进入该区间。

    逻辑：
    1. 查 state 中上一轮的 zone，如果和 current_zone 不同 → 一定是首次穿越
    2. 如果 zone 相同，尝试用 qmt2http 分钟线做更精确判断：
       检查最近 5 根分钟线是否有"从区间外进入区间内"的过程
    3. qmt2http 不可用时，退化到基于 state 的 zone 变化判断
    """
    zones = state.setdefault("zones", {})
    last_zone = zones.get(code)

    # zone 发生变化 → 一定是首次穿越
    if last_zone != current_zone:
        zones[code] = current_zone
        return True

    # zone 未变化 → 尝试用分钟线确认是否有中间离开再回来的情况
    minute_prices = fetch_minute_prices(code, lookback=5)
    if minute_prices and len(minute_prices) >= 2:
        # 检查分钟线中是否存在"先离开区间、再回来"的过程
        if current_zone == "buy":
            low, high = rule.buy_range
        elif current_zone == "sell":
            low, high = rule.sell_range
        else:
            # risk 区域不做分钟线精化，zone 未变就抑制
            return False

        was_outside = False
        for p in minute_prices[:-1]:  # 不看最后一根（就是当前价）
            if p < low or p > high:
                was_outside = True
                break

        if was_outside:
            # 分钟线显示有"离开再回来"的穿越，允许发信号
            return True
        # 分钟线显示一直在区间内，不是新穿越
        return False

    # qmt2http 不可用，zone 又没变 → 不发（保守抑制重复）
    return False


# ==============================================================================
# 东财妙想资讯搜索：止损/异动时自动搜索原因
# ==============================================================================

def search_stock_news(stock_name: str, reason: str = "异动") -> str:
    """
    调用东方财富妙想搜索接口，获取个股相关资讯摘要。
    返回格式化文本（2-3条摘要），失败返回空字符串。
    """
    if not MX_APIKEY:
        return ""

    query = f"{stock_name}{reason}原因分析"
    url = f"{MX_API_BASE}/api/claw/news-search"
    payload = json.dumps({"query": query}).encode()
    headers = {
        "Content-Type": "application/json",
        "apikey": MX_APIKEY,
    }
    req = Request(url, data=payload, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (HTTPError, URLError, OSError):
        return ""

    # 解析：data.data.llmSearchResponse.data
    import re
    items = None
    try:
        inner = data.get("data", {})
        if isinstance(inner, dict):
            inner2 = inner.get("data", {})
            if isinstance(inner2, dict):
                llm = inner2.get("llmSearchResponse", {})
                if isinstance(llm, dict):
                    items = llm.get("data", [])
    except Exception:
        return ""

    if not items or not isinstance(items, list):
        return ""

    lines = []
    for item in items[:3]:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        title = re.sub(r'<[^>]+>', '', title).strip()
        content = item.get("content", "")
        content = re.sub(r'<[^>]+>', '', content).strip()
        if content and len(content) > 120:
            content = content[:120] + "..."
        if title:
            lines.append(f"  · {title}")
            if content:
                lines.append(f"    {content}")
    return "\n".join(lines)


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
    enabled: bool = True


@dataclass
class MarketContext:
    regime: str
    avg_change_pct: float
    index_changes: Dict[str, float]


MARKET_INDEXES = [
    ("sh000001", "上证指数"),
    ("sz399006", "创业板指"),
]


def _safe_float(value) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def load_config(path: Path) -> Dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def merge_daily_plan(config: Dict, plan_path: Path) -> Dict:
    if not plan_path.exists():
        return config

    with plan_path.open("r", encoding="utf-8") as f:
        daily = json.load(f)

    plan_date = daily.get("date")
    today = datetime.now().strftime("%Y-%m-%d")
    if plan_date != today:
        return config

    overrides = {str(item["code"]): item for item in daily.get("stocks", [])}
    merged = json.loads(json.dumps(config, ensure_ascii=False))
    for stock in merged.get("stocks", []):
        override = overrides.get(str(stock.get("code")))
        if override:
            stock.update(override)
    return merged


def load_daily_plan(plan_path: Path) -> Dict:
    today = datetime.now().strftime("%Y-%m-%d")
    default = {"date": today, "updated_at": "", "source": "monitor", "stocks": []}
    if not plan_path.exists():
        return default
    with plan_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("date") != today:
        return default
    if "stocks" not in data or not isinstance(data["stocks"], list):
        data["stocks"] = []
    return data


def save_daily_plan(plan_path: Path, plan: Dict) -> None:
    plan["date"] = datetime.now().strftime("%Y-%m-%d")
    plan["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if "source" not in plan:
        plan["source"] = "monitor"
    with plan_path.open("w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)


def upsert_plan_override(plan: Dict, rule: StockRule) -> Dict:
    for item in plan.get("stocks", []):
        if str(item.get("code")) == rule.code:
            return item
    item = {"code": rule.code, "name": rule.name}
    plan.setdefault("stocks", []).append(item)
    return item


def ensure_state_dir() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_state() -> Dict:
    ensure_state_dir()
    if not STATE_PATH.exists():
        return {"signals": {}}
    with STATE_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict) -> None:
    ensure_state_dir()
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def in_trade_hours(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 930 <= hm <= 1130 or 1300 <= hm <= 1500


def in_preopen_hours(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 830 <= hm <= 920


def can_send_monitor_message(now: Optional[datetime] = None, allow_preopen: bool = False) -> bool:
    now = now or datetime.now()
    if in_trade_hours(now):
        return True
    if allow_preopen and in_preopen_hours(now):
        return True
    return False


def parse_rules(config: Dict) -> List[StockRule]:
    rules = []
    for item in config.get("stocks", []):
        rules.append(
            StockRule(
                code=str(item["code"]),
                name=item.get("name", str(item["code"])),
                cost_price=float(item["cost_price"]),
                base_position=int(item["base_position"]),
                per_trade_shares=int(item.get("per_trade_shares", 100)),
                buy_range=(float(item["buy_range"][0]), float(item["buy_range"][1])),
                sell_range=(float(item["sell_range"][0]), float(item["sell_range"][1])),
                stop_loss=float(item["stop_loss"]),
                strategy=item.get("strategy", "观察"),
                note=item.get("note", ""),
                watch_mode=str(item.get("watch_mode", "") or "").strip(),
                preopen_risk_mode=item.get("preopen_risk_mode", ""),
                avoid_reverse_t=bool(item.get("avoid_reverse_t", False)),
                abandon_buy_below=float(item.get("abandon_buy_below", 0) or 0),
                allow_rebound_watch_after_stop=bool(item.get("allow_rebound_watch_after_stop", False)),
                rebound_buy_above=float(item.get("rebound_buy_above", 0) or 0),
                enabled=bool(item.get("enabled", True)),
            )
        )
    enabled_rules = [r for r in rules if r.enabled]
    for rule in enabled_rules:
        apply_rule_mode_defaults(rule)
    return enabled_rules


def apply_rule_mode_defaults(rule: StockRule) -> None:
    """
    将通用观察模式映射为具体风控行为，避免每只票重复手写零散开关。
    """
    watch_mode = (rule.watch_mode or "").lower()
    if watch_mode == "light":
        rule.avoid_reverse_t = True


def get_quotes_map(codes: List[str]) -> Dict[str, Dict]:
    quotes = get_quotes(codes)
    result: Dict[str, Dict] = {}
    for quote in quotes:
        if "error" in quote:
            continue
        result[str(quote.get("code"))] = quote
    return result


def build_market_context(quotes: Dict[str, Dict]) -> MarketContext:
    index_changes: Dict[str, float] = {}
    for code, name in MARKET_INDEXES:
        quote = quotes.get(code)
        if not quote:
            continue
        try:
            index_changes[name] = float(quote.get("change_percent", 0))
        except (TypeError, ValueError):
            continue

    avg_change_pct = 0.0
    if index_changes:
        avg_change_pct = sum(index_changes.values()) / len(index_changes)

    regime = "neutral"
    if index_changes:
        min_change = min(index_changes.values())
        if avg_change_pct <= -1.2 or min_change <= -2.0:
            regime = "weak"
        elif avg_change_pct >= 1.0:
            regime = "strong"

    return MarketContext(
        regime=regime,
        avg_change_pct=avg_change_pct,
        index_changes=index_changes,
    )


def format_market_context(market: MarketContext) -> str:
    if not market.index_changes:
        return "市场状态：未知（指数行情缺失）"

    names = []
    for name, change in market.index_changes.items():
        names.append(f"{name}{change:+.2f}%")
    return (
        f"市场状态：{market.regime} "
        f"(均值 {market.avg_change_pct:+.2f}% / {'，'.join(names)})"
    )


def format_preopen_warning(
    rule: StockRule,
    market: MarketContext,
    auction: Optional[Dict],
    quote: Dict,
    reasons: List[str],
    level: str,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    quote_price = _safe_float(quote.get("price")) or 0.0
    quote_change = _safe_float(quote.get("change_percent")) or 0.0
    auction_price = _safe_float((auction or {}).get("auction_price"))
    auction_change = _safe_float((auction or {}).get("change_percent"))
    auction_volume = _safe_float((auction or {}).get("volume"))

    if level == "high":
        title = "盘前弱势预警"
        action = (
            "开盘先按防守模式处理：09:25-09:45 只观察，不开盘即逆T抄底；"
            " 若冲高到卖区，优先先卖后买。"
        )
    else:
        title = "盘前谨慎提醒"
        action = (
            "今日盘前偏弱，先缩小预期。只有竞价回暖、开盘后承接清晰时，"
            " 才考虑按原计划轻仓执行。"
        )

    auction_line = "竞价数据：暂无"
    if auction_price is not None or auction_change is not None:
        auction_line = (
            f"竞价数据：价格 {auction_price:.2f}" if auction_price is not None else "竞价数据：价格未知"
        )
        if auction_change is not None:
            auction_line += f" / 涨跌幅 {auction_change:+.2f}%"
        if auction_volume is not None:
            auction_line += f" / 量 {auction_volume:.0f}"

    return (
        f"⚠️ {title}\n"
        f"时间：{now}\n"
        f"股票：{rule.name} ({rule.code})\n"
        f"{format_market_context(market)}\n"
        f"参考现价：{quote_price:.2f} / 涨跌幅 {quote_change:+.2f}%\n"
        f"{auction_line}\n"
        f"预设区间：买 {rule.buy_range[0]:.2f}-{rule.buy_range[1]:.2f} / "
        f"卖 {rule.sell_range[0]:.2f}-{rule.sell_range[1]:.2f} / 止损 {rule.stop_loss:.2f}\n"
        f"触发原因：{'；'.join(reasons)}\n"
        f"动作建议：{action}\n"
        f"提示：盘前预警只用于降级风险，不替代开盘后盘口确认。"
    )


def apply_preopen_plan_override(
    rule: StockRule,
    plan: Dict,
    market: MarketContext,
    auction: Optional[Dict],
    warning_level: str,
) -> None:
    override = upsert_plan_override(plan, rule)
    ref_price = _safe_float((auction or {}).get("auction_price")) or 0.0

    override["preopen_risk_mode"] = "defensive" if warning_level == "high" else "cautious"
    override["avoid_reverse_t"] = True

    if warning_level == "high":
        floor = ref_price if ref_price > 0 else rule.stop_loss
        override["abandon_buy_below"] = round(min(floor, rule.stop_loss), 2)
    else:
        override["abandon_buy_below"] = round(ref_price, 2) if ref_price > 0 else round(rule.buy_range[0], 2)

    notes = list(override.get("risk_notes", [])) if isinstance(override.get("risk_notes"), list) else []
    note = (
        f"盘前{market.regime}预警："
        f"模式={override['preopen_risk_mode']} "
        f"禁止逆T={'是' if override['avoid_reverse_t'] else '否'} "
        f"放弃低吸阈值={override['abandon_buy_below']}"
    )
    if note not in notes:
        notes.append(note)
    override["risk_notes"] = notes[-3:]


def should_send_signal(state: Dict, stock_code: str, signal_type: str, cooldown_minutes: int) -> bool:
    signals = state.setdefault("signals", {})
    stock_signals = signals.setdefault(stock_code, {})
    last_sent = stock_signals.get(signal_type)
    if not last_sent:
        return True

    try:
        last_time = datetime.fromisoformat(last_sent)
    except ValueError:
        return True

    delta_minutes = (datetime.now() - last_time).total_seconds() / 60
    return delta_minutes >= cooldown_minutes


def mark_signal_sent(state: Dict, stock_code: str, signal_type: str) -> None:
    signals = state.setdefault("signals", {})
    stock_signals = signals.setdefault(stock_code, {})
    stock_signals[signal_type] = datetime.now().isoformat(timespec="seconds")


def format_signal(
    rule: StockRule,
    quote: Dict,
    signal_type: str,
    market: MarketContext,
    news_context: str = "",
) -> str:
    price = float(quote["price"])
    change_pct = float(quote.get("change_percent", 0))
    open_price = float(quote.get("open", 0))
    high = float(quote.get("high", 0))
    low = float(quote.get("low", 0))
    volume = quote.get("volume", 0)
    amount = quote.get("amount", 0)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if signal_type == "buy":
        title = "买入观察提醒"
        if market.regime == "weak":
            action = (
                f"价格进入预设买入区间 {rule.buy_range[0]:.2f}-{rule.buy_range[1]:.2f}。"
                " 但当前为弱势日，不主张直接抄底。"
                f" 只有在你已先卖出仓位、且分时承接明确时，才考虑按 {rule.per_trade_shares} 股小仓回补。"
            )
        else:
            action = (
                f"价格进入预设买入区间 {rule.buy_range[0]:.2f}-{rule.buy_range[1]:.2f}。"
                f" 若分时企稳，可先按 {rule.per_trade_shares} 股观察性接回。"
            )
    elif signal_type == "rebound_buy":
        title = "反弹观察提醒"
        action = (
            f"价格曾跌破风险线 {rule.stop_loss:.2f}，现已重新站回反弹确认位 {rule.rebound_buy_above:.2f} 上方。"
            f" 仅在你已有先卖出仓位、且分时承接明确时，才考虑按 {rule.per_trade_shares} 股小仓回补。"
        )
    elif signal_type == "sell":
        title = "卖出观察提醒"
        if market.regime == "weak":
            action = (
                f"价格进入预设卖出区间 {rule.sell_range[0]:.2f}-{rule.sell_range[1]:.2f}。"
                f" 当前为弱势日，优先考虑按 {rule.per_trade_shares} 股先卖后买，做防守型反向T。"
            )
        else:
            action = (
                f"价格进入预设卖出区间 {rule.sell_range[0]:.2f}-{rule.sell_range[1]:.2f}。"
                f" 若已有日内仓，可优先按 {rule.per_trade_shares} 股减仓或完成顺T。"
            )
    else:
        title = "风险提醒"
        if market.regime == "weak":
            action = (
                f"价格跌破风险线 {rule.stop_loss:.2f}。"
                " 当前又是弱势日，应停止逆T抄底，只保留防守和减仓思路。"
            )
        else:
            action = (
                f"价格跌破风险线 {rule.stop_loss:.2f}。"
                " 这笔交易不应再按做T思路硬扛，请重新评估。"
            )

    # 成交量信息
    vol_str = ""
    if volume:
        vol_str = f"\n成交量/额：{volume/10000:.1f}万手 / {amount:.0f}万元"

    # 资讯附加（止损/风险信号）
    news_block = ""
    if news_context:
        news_block = f"\n--- 相关资讯 ---\n{news_context}"

    return (
        f"🔔 {title}\n"
        f"时间：{now}\n"
        f"股票：{rule.name} ({rule.code})\n"
        f"现价：{price:.2f}\n"
        f"涨跌幅：{change_pct:+.2f}%\n"
        f"开盘/最高/最低：{open_price:.2f} / {high:.2f} / {low:.2f}"
        f"{vol_str}\n"
        f"底仓成本：{rule.cost_price:.2f}\n"
        f"底仓股数：{rule.base_position}\n"
        f"策略类型：{rule.strategy}\n"
        f"观察模式：{rule.watch_mode or 'normal'}\n"
        f"盘前模式：{rule.preopen_risk_mode or 'normal'}\n"
        f"{format_market_context(market)}\n"
        f"建议单次股数：{rule.per_trade_shares}\n"
        f"动作建议：{action}\n"
        f"备注：{rule.note or '无'}\n"
        f"提示：仅为条件触发提醒，请结合盘口、大盘和成交量二次确认。"
        f"{news_block}"
    )


def send_feishu(target: str, message: str) -> bool:
    result = subprocess.run(
        [
            "openclaw",
            "message",
            "send",
            "--channel",
            "feishu",
            "--target",
            target,
            "-m",
            message,
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def evaluate_rule(rule: StockRule, quote: Dict) -> Optional[str]:
    price = float(quote["price"])
    buy_low, buy_high = rule.buy_range
    sell_low, sell_high = rule.sell_range

    if price <= rule.stop_loss:
        return "risk"
    if buy_low <= price <= buy_high:
        return "buy"
    if sell_low <= price <= sell_high:
        return "sell"
    return None


def evaluate_rebound_watch(rule: StockRule, quote: Dict, state: Dict) -> Optional[str]:
    """
    对高波动票保留一类特殊观察：
    先跌破止损进入 risk，再重新站回确认位上方时，给一次反弹观察提醒。
    """
    if not rule.allow_rebound_watch_after_stop or rule.rebound_buy_above <= 0:
        return None

    price = float(quote["price"])
    if price < rule.rebound_buy_above:
        return None

    last_zone = state.setdefault("zones", {}).get(rule.code)
    if last_zone != "risk":
        return None

    return "rebound_buy"


def has_obvious_price_scale_mismatch(rule: StockRule, quote: Dict) -> bool:
    """
    检测明显的价格缩放错误，避免行情口径异常时直接触发风险/买卖信号。
    """
    price = float(quote.get("price", 0) or 0)
    ref = max(rule.buy_range[0], rule.sell_range[0], rule.stop_loss)
    if price <= 0 or ref <= 0:
        return False

    ratio = ref / price
    # 典型异常：真实应为 149.0，却被解析成 1.49，比例约 100 倍。
    return 50 <= ratio <= 200


def should_suppress_buy_signal(rule: StockRule, quote: Dict, market: MarketContext) -> bool:
    """
    弱势日默认不做抄底逆T。
    仅当该票本身就是“逆T”策略，且跌幅未显著恶化时，才保留买入观察提醒。
    """
    try:
        price = float(quote.get("price", 0))
    except (TypeError, ValueError):
        price = 0.0

    if rule.abandon_buy_below and price > 0 and price <= rule.abandon_buy_below:
        return True

    if rule.avoid_reverse_t:
        return True

    if market.regime != "weak":
        return False

    strategy = rule.strategy or ""
    if "逆T" not in strategy:
        return True

    try:
        change_pct = float(quote.get("change_percent", 0))
    except (TypeError, ValueError):
        change_pct = 0.0
    return change_pct <= -7.0


def evaluate_preopen_warning(
    rule: StockRule,
    quote: Dict,
    auction: Optional[Dict],
    market: MarketContext,
) -> Optional[Tuple[str, str]]:
    reasons: List[str] = []
    level = "medium"

    if market.regime == "weak":
        reasons.append("指数环境偏弱")
        level = "high"
    elif market.avg_change_pct <= -0.6:
        reasons.append("指数开盘前整体偏弱")

    ref_price = _safe_float((auction or {}).get("auction_price"))
    ref_change = _safe_float((auction or {}).get("change_percent"))
    if ref_price is None:
        ref_price = _safe_float(quote.get("price"))
    if ref_change is None:
        ref_change = _safe_float(quote.get("change_percent"))

    if ref_change is not None and ref_change <= -2.0:
        reasons.append(f"竞价/盘前跌幅 {ref_change:+.2f}%")
    if ref_change is not None and ref_change <= -4.0:
        level = "high"

    if ref_price is not None and ref_price <= rule.stop_loss:
        reasons.append("竞价已压到风险线下方")
        level = "high"
    elif ref_price is not None and ref_price < rule.buy_range[0]:
        reasons.append("竞价已明显低于预设买区下沿")

    if not reasons:
        return None

    message = format_preopen_warning(rule, market, auction, quote, reasons, level)
    return level, message


def check_once(config_path: Path, dry_run: bool = False) -> int:
    config = merge_daily_plan(load_config(config_path), DAILY_PLAN_PATH)
    rules = parse_rules(config)
    if not rules:
        print("未配置有效股票")
        return 1

    monitor_cfg = config.get("monitor", {})
    feishu_cfg = config.get("feishu", {})
    cooldown_minutes = int(monitor_cfg.get("cooldown_minutes", 20))
    only_trade_hours = bool(monitor_cfg.get("only_trade_hours", True))
    allow_preopen_alerts = bool(monitor_cfg.get("allow_preopen_alerts", False))

    # T 信号配置
    t_signal_enabled = bool(monitor_cfg.get("t_signal_enabled", True))
    t_signal_cooldown = int(monitor_cfg.get("t_signal_cooldown_minutes", 30))
    t_signal_max_per_day = int(monitor_cfg.get("t_signal_max_per_day", 4))
    dynamic_range_threshold = float(monitor_cfg.get("dynamic_range_threshold_pct", 8))

    now = datetime.now()
    preopen_mode = in_preopen_hours(now)

    if only_trade_hours and not (in_trade_hours(now) or preopen_mode):
        print("当前非交易时段，跳过检查")
        return 0

    quote_codes = [r.code for r in rules] + [code for code, _ in MARKET_INDEXES]
    quotes = get_quotes_map(quote_codes)
    market = build_market_context(quotes)
    print(format_market_context(market))
    state = load_state()
    send_allowed = can_send_monitor_message(now, allow_preopen=allow_preopen_alerts)

    if preopen_mode:
        sent_count = 0
        today = now.strftime("%Y-%m-%d")
        plan = load_daily_plan(DAILY_PLAN_PATH)
        plan_changed = False
        preopen_hits = 0
        for rule in rules:
            quote = quotes.get(rule.code, {})
            auction = fetch_auction_snapshot(rule.code)
            result = evaluate_preopen_warning(rule, quote, auction, market)
            if not result:
                continue

            _level, message = result
            preopen_hits += 1
            apply_preopen_plan_override(rule, plan, market, auction, _level)
            plan["source"] = "monitor-preopen"
            plan_changed = True
            signal_type = f"preopen_warning_{today}"
            if not should_send_signal(state, rule.code, signal_type, 99999):
                continue

            delivered = False
            if feishu_cfg.get("enabled", True) and not dry_run and send_allowed:
                print(message)
                print("-" * 60)
                target = feishu_cfg.get("target", "").strip()
                if not target:
                    print("飞书 target 未配置，跳过发送")
                else:
                    delivered = send_feishu(target, message)
            elif feishu_cfg.get("enabled", True) and not dry_run and not send_allowed:
                print(f"{rule.name}({rule.code}) 盘前预警已更新计划，当前不在自动发送时段")
            else:
                print(message)
                print("-" * 60)
                delivered = True

            if delivered:
                mark_signal_sent(state, rule.code, signal_type)
                if feishu_cfg.get("enabled", True) and not dry_run and send_allowed:
                    sent_count += 1
            elif feishu_cfg.get("enabled", True) and not dry_run and send_allowed:
                print(f"{rule.name}({rule.code}) 盘前预警发送失败")

        if plan_changed:
            save_daily_plan(DAILY_PLAN_PATH, plan)
        save_state(state)
        if not dry_run and not send_allowed:
            print(f"本轮盘前检查完成，命中 {preopen_hits} 只，已更新计划，未发送飞书")
        else:
            print(f"本轮盘前检查完成，发送 {sent_count} 条预警")
        return 0

    # ---- 盘中主循环 ----
    phase = get_analysis_phase(now)

    # 前日日K缓存：每日首次轮询获取，后续复用
    today_str = now.strftime("%Y-%m-%d")
    yesterday_cache = state.setdefault("yesterday_cache", {})
    if yesterday_cache.get("date") != today_str:
        yesterday_cache.clear()
        yesterday_cache["date"] = today_str

    # 日内数据每日重置
    intraday_state = state.setdefault("intraday", {})
    for code_key in list(intraday_state.keys()):
        if isinstance(intraday_state[code_key], dict) and intraday_state[code_key].get("date") != today_str:
            del intraday_state[code_key]

    sent_count = 0
    for rule in rules:
        quote = quotes.get(rule.code)
        if not quote:
            print(f"未获取到 {rule.code} 行情")
            continue

        if has_obvious_price_scale_mismatch(rule, quote):
            print(
                f"{rule.name}({rule.code}) 行情口径疑似异常："
                f"当前价 {float(quote['price']):.2f} 与策略区间不在同一数量级，跳过本轮信号判断"
            )
            continue

        # ---- 分时分析 ----
        analysis = None
        is_dynamic = False
        if t_signal_enabled and phase in ("first_wave", "steady", "winddown"):
            # 获取前日日K（缓存）
            yesterday = None
            if rule.code in yesterday_cache and isinstance(yesterday_cache[rule.code], dict):
                yesterday = yesterday_cache[rule.code]
            else:
                yesterday = fetch_yesterday_daily(rule.code)
                if yesterday:
                    yesterday_cache[rule.code] = yesterday

            # 获取完整分钟线
            bars = fetch_full_intraday_bars(rule.code)
            if bars and len(bars) >= 15:
                analysis = compute_intraday_analysis(rule.code, rule, bars, yesterday)
                if analysis:
                    analysis = adjust_for_strategy(analysis, rule)

            # 判断是否使用动态区间
            is_dynamic = analysis is not None and should_use_dynamic_range(rule, quote, dynamic_range_threshold)

        # ---- 信号判断 ----
        if is_dynamic and analysis:
            # 动态区间替代静态配置
            price = float(quote["price"])
            dyn_buy_low = analysis.t_buy_target * 0.995
            dyn_buy_high = analysis.t_buy_target * 1.005
            dyn_sell_low = analysis.t_sell_target * 0.995
            dyn_sell_high = analysis.t_sell_target * 1.005
            # 动态止损：当日最低枢轴点下方 1%
            dyn_stop = 0.0
            if analysis.pivot_lows:
                dyn_stop = min(p for p, _ in analysis.pivot_lows) * 0.99

            if dyn_stop > 0 and price <= dyn_stop:
                signal_type = "risk"
            elif dyn_buy_low <= price <= dyn_buy_high:
                signal_type = "buy"
            elif dyn_sell_low <= price <= dyn_sell_high:
                signal_type = "sell"
            else:
                signal_type = None

            if signal_type:
                print(f"{rule.name}({rule.code}) [动态区间] 触发 {signal_type}，"
                      f"动态买区 {dyn_buy_low:.2f}-{dyn_buy_high:.2f}，"
                      f"动态卖区 {dyn_sell_low:.2f}-{dyn_sell_high:.2f}")
        else:
            signal_type = evaluate_rule(rule, quote)
            if not signal_type:
                signal_type = evaluate_rebound_watch(rule, quote, state)

        if not signal_type:
            # 更新 zone 状态
            price = float(quote["price"])
            current_zone = classify_zone(price, rule)
            state.setdefault("zones", {})[rule.code] = current_zone

            # ---- 独立 T 信号：无 zone 信号但分析有高/中置信度 T 机会 ----
            if (analysis and analysis.confidence in ("高", "中")
                    and t_signal_enabled
                    and phase in ("steady", "winddown")):
                # 检查现价是否接近 T 买入或 T 卖出参考价（±1%）
                near_buy = (analysis.t_buy_target > 0
                            and price <= analysis.t_buy_target * 1.01)
                near_sell = (analysis.t_sell_target > 0
                             and price >= analysis.t_sell_target * 0.99)
                if near_buy or near_sell:
                    if should_send_t_signal(state, rule.code, t_signal_cooldown, t_signal_max_per_day):
                        t_message = format_t_signal(analysis, rule, quote, market, is_dynamic)
                        print(t_message)
                        print("-" * 60)

                        t_delivered = False
                        if feishu_cfg.get("enabled", True) and not dry_run and send_allowed:
                            target = feishu_cfg.get("target", "").strip()
                            if target:
                                t_delivered = send_feishu(target, t_message)
                        else:
                            t_delivered = True

                        if t_delivered:
                            mark_t_signal_sent(state, rule.code, analysis)
                            if feishu_cfg.get("enabled", True) and not dry_run and send_allowed:
                                sent_count += 1
                    else:
                        print(f"{rule.name}({rule.code}) T信号冷却中或已达上限")
                else:
                    print(f"{rule.name}({rule.code}) 当前 {price:.2f}，区域={current_zone}，"
                          f"T分析 买参考={analysis.t_buy_target:.2f} 卖参考={analysis.t_sell_target:.2f} "
                          f"置信={analysis.confidence}，未触发")
            else:
                print(f"{rule.name}({rule.code}) 当前 {price:.2f}，区域={current_zone}，未触发")
            continue

        if signal_type in ("buy", "rebound_buy") and should_suppress_buy_signal(rule, quote, market):
            if rule.abandon_buy_below and float(quote.get("price", 0) or 0) <= rule.abandon_buy_below:
                print(
                    f"{rule.name}({rule.code}) 当前价已低于盘前放弃低吸阈值 "
                    f"{rule.abandon_buy_below:.2f}，抑制买入提醒"
                )
            elif (rule.watch_mode or "").lower() == "light":
                print(f"{rule.name}({rule.code}) 当前为轻观察模式，抑制逆T买入提醒")
            elif rule.avoid_reverse_t:
                print(f"{rule.name}({rule.code}) 盘前已切换防守模式，禁止逆T买入提醒")
            else:
                print(f"{rule.name}({rule.code}) 当前为弱势日，抑制抄底型买入提醒")
            state.setdefault("zones", {})[rule.code] = "buy"
            continue

        # 时间冷却检查
        if not should_send_signal(state, rule.code, signal_type, cooldown_minutes):
            print(f"{rule.name}({rule.code}) {signal_type} 信号仍在冷却中")
            continue

        # 边缘检测：是否为"首次穿越"进入该区间
        current_zone = "buy" if signal_type == "rebound_buy" else signal_type
        if signal_type in ("buy", "sell") and not is_first_crossing(rule.code, rule, current_zone, state):
            print(f"{rule.name}({rule.code}) {signal_type} 非首次穿越，抑制重复信号")
            continue
        if signal_type == "rebound_buy":
            state.setdefault("zones", {})[rule.code] = "buy"

        # 止损/风险信号 → 搜索东财资讯找异动原因
        news_context = ""
        if signal_type == "risk":
            change_pct = float(quote.get("change_percent", 0))
            reason = "大跌" if change_pct < -5 else "下跌"
            print(f"  搜索 {rule.name} {reason}原因...")
            news_context = search_stock_news(rule.name, reason)
            # risk 区域也更新 zone
            state.setdefault("zones", {})[rule.code] = "risk"

        # 买入/卖出类信号且涨跌幅异常大（>5%），也附加资讯
        if signal_type in ("buy", "rebound_buy", "sell"):
            change_pct = float(quote.get("change_percent", 0))
            if abs(change_pct) > 5:
                direction = "大涨" if change_pct > 0 else "大跌"
                print(f"  涨跌幅 {change_pct:+.1f}% 异常，搜索 {rule.name} {direction}原因...")
                news_context = search_stock_news(rule.name, direction)

        message = format_signal(rule, quote, signal_type, market, news_context)

        # 如果有分时分析，在 zone 信号消息后附加 T 分析摘要
        if analysis and is_dynamic:
            message += (
                f"\n--- 分时分析参考 [动态区间] ---\n"
                f"  VWAP：{analysis.vwap:.2f} / MA20：{analysis.ma20:.2f} / MA60：{analysis.ma60:.2f}\n"
                f"  T买入参考：{analysis.t_buy_target:.1f} / T卖出参考：{analysis.t_sell_target:.1f}\n"
                f"  预期价差：{analysis.t_spread_pct:.1f}% / 置信度：{analysis.confidence}"
            )
        elif analysis:
            message += (
                f"\n--- 分时分析参考 ---\n"
                f"  VWAP：{analysis.vwap:.2f} / MA20：{analysis.ma20:.2f} / MA60：{analysis.ma60:.2f}\n"
                f"  T买入参考：{analysis.t_buy_target:.1f} / T卖出参考：{analysis.t_sell_target:.1f}\n"
                f"  预期价差：{analysis.t_spread_pct:.1f}% / 置信度：{analysis.confidence}"
            )

        print(message)
        print("-" * 60)

        delivered = False
        if feishu_cfg.get("enabled", True) and not dry_run and send_allowed:
            target = feishu_cfg.get("target", "").strip()
            if not target:
                print("飞书 target 未配置，跳过发送")
            else:
                delivered = send_feishu(target, message)
        elif feishu_cfg.get("enabled", True) and not dry_run and not send_allowed:
            print("当前非交易时段，跳过飞书发送")
        else:
            delivered = True

        if delivered:
            mark_signal_sent(state, rule.code, signal_type)
            if feishu_cfg.get("enabled", True) and not dry_run and send_allowed:
                sent_count += 1
        elif feishu_cfg.get("enabled", True) and not dry_run and send_allowed:
            print(f"{rule.name}({rule.code}) 飞书发送失败")

    save_state(state)
    print(f"本轮检查完成，发送 {sent_count} 条提醒")
    return 0


def run_daemon(config_path: Path, dry_run: bool = False) -> int:
    config = merge_daily_plan(load_config(config_path), DAILY_PLAN_PATH)
    poll_seconds = int(config.get("monitor", {}).get("poll_seconds", 60))
    print(f"开始监控，轮询间隔 {poll_seconds} 秒")
    try:
        while True:
            check_once(config_path, dry_run=dry_run)
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        print("监控已停止")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="做T半自动飞书监控")
    parser.add_argument("mode", choices=["check", "daemon"], help="check: 检查一次, daemon: 持续监控")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不发飞书")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        print(f"配置文件不存在: {config_path}")
        return 1

    if args.mode == "check":
        return check_once(config_path, dry_run=args.dry_run)
    return run_daemon(config_path, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

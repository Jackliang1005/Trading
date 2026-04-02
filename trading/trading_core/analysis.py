import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen
import requests

from .models import IntradayAnalysis, LearningProfile, MarketContext, StockRule
from .storage import _safe_float, upsert_plan_override


SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "eastmoney-quotes"
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))
from eastmoney_quotes import get_quotes  # type: ignore


QMT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "做T监控配置.json"
MX_APIKEY = os.environ.get("MX_APIKEY", "")
MX_API_BASE = "https://mkapi2.dfcfs.com/finskillshub"

MARKET_INDEXES = [("sh000001", "上证指数"), ("sz399006", "创业板指")]


def _load_qmt_runtime_config() -> Dict:
    try:
        with QMT_CONFIG_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return {}
    qmt_cfg = payload.get("qmt2http", {}) if isinstance(payload, dict) else {}
    return qmt_cfg if isinstance(qmt_cfg, dict) else {}


def _qmt_runtime_settings() -> Dict[str, object]:
    qmt_cfg = _load_qmt_runtime_config()
    timeout_value = os.environ.get("QMT2HTTP_TIMEOUT")
    base_url = (
        os.environ.get("QMT2HTTP_BASE_URL")
        or str(qmt_cfg.get("base_url") or "http://150.158.31.115:8085")
    ).rstrip("/")
    api_token = (
        os.environ.get("QMT2HTTP_API_TOKEN")
        or str(qmt_cfg.get("api_token") or "998811")
    ).strip()
    read_timeout = float(
        os.environ.get("QMT2HTTP_READ_TIMEOUT")
        or timeout_value
        or qmt_cfg.get("timeout")
        or 10
    )
    connect_timeout = float(os.environ.get("QMT2HTTP_CONNECT_TIMEOUT") or 3)
    return {
        "base_url": base_url,
        "api_token": api_token,
        "connect_timeout": connect_timeout,
        "read_timeout": read_timeout,
    }


def _qmt_code(code: str) -> str:
    text = str(code or "").strip()
    if not text:
        return text
    upper = text.upper()
    lower = text.lower()
    if "." in upper:
        return upper
    if lower.startswith("sh"):
        return f"{upper[2:]}.SH"
    if lower.startswith("sz"):
        return f"{upper[2:]}.SZ"
    if lower.startswith("bj"):
        return f"{upper[2:]}.BJ"
    if upper.startswith(("8", "4")):
        return f"{upper}.BJ"
    if upper.startswith(("6", "5", "9")):
        return f"{upper}.SH"
    return f"{upper}.SZ"


def _price_from_tick(item: Dict) -> float:
    bids = item.get("bids") or []
    if isinstance(bids, list) and bids:
        bid0 = _safe_float(bids[0])
        if bid0 and bid0 > 0:
            return bid0
    return _safe_float(item.get("last_price", item.get("price"))) or 0.0


def fetch_batch_realtime_quotes(codes: List[str]) -> Dict[str, Dict]:
    if not codes:
        return {}
    code_map = {_qmt_code(code): str(code) for code in codes}
    result = _qmt_request(
        "/rpc/data_fetcher",
        payload={
            "method": "get_batch_realtime_data",
            "params": {"codes": list(code_map.keys())},
        },
    )
    data = (result or {}).get("data")
    if not result or not result.get("success") or not isinstance(data, dict):
        return {}

    quotes: Dict[str, Dict] = {}
    for qmt_code, item in data.items():
        if not isinstance(item, dict):
            continue
        original_code = code_map.get(str(qmt_code).upper())
        if not original_code:
            continue
        price = _price_from_tick(item)
        pre_close = _safe_float(item.get("pre_close", item.get("last_close"))) or 0.0
        high = _safe_float(item.get("high")) or 0.0
        low = _safe_float(item.get("low")) or 0.0
        open_price = _safe_float(item.get("open")) or 0.0
        volume = _safe_float(item.get("volume")) or 0.0
        amount = _safe_float(item.get("amount")) or 0.0
        change_percent = _safe_float(item.get("change_pct")) or (round((price - pre_close) / pre_close * 100, 2) if pre_close and price > 0 else 0.0)
        change = price - pre_close if pre_close and price > 0 else 0.0
        if price <= 0:
            continue
        quotes[original_code] = {
            "code": original_code,
            "name": original_code,
            "price": round(price, 2),
            "change": round(change, 2),
            "change_percent": round(change_percent, 2),
            "volume": volume,
            "amount": round(amount / 10000, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "open": round(open_price, 2),
            "pre_close": round(pre_close, 2),
            "source": "qmt2http",
        }
    return quotes


def _build_url_opener():
    proxies = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        value = os.environ.get(key, "").strip()
        if not value:
            continue
        lower = key.lower()
        if "https" in lower:
            proxies["https"] = value
        elif "http" in lower:
            proxies["http"] = value
    return build_opener(ProxyHandler(proxies)) if proxies else build_opener()


def _qmt_request(path: str, method: str = "POST", payload: dict = None) -> Optional[dict]:
    settings = _qmt_runtime_settings()
    url = f"{settings['base_url']}{path}"
    headers = {"Content-Type": "application/json"}
    api_token = str(settings.get("api_token") or "").strip()
    if api_token:
        headers["X-API-Token"] = api_token
        headers["Authorization"] = f"Bearer {api_token}"
    try:
        session = requests.Session()
        session.trust_env = False
        response = session.request(
            method=method,
            url=url,
            json=payload if payload else None,
            headers=headers,
            timeout=(float(settings["connect_timeout"]), float(settings["read_timeout"])),
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError, HTTPError, URLError, OSError):
        return None


def _qmt_get(path: str, params: dict = None) -> Optional[dict]:
    settings = _qmt_runtime_settings()
    url = f"{settings['base_url']}{path}"
    headers = {}
    api_token = str(settings.get("api_token") or "").strip()
    if api_token:
        headers["X-API-Token"] = api_token
        headers["Authorization"] = f"Bearer {api_token}"
    try:
        session = requests.Session()
        session.trust_env = False
        response = session.get(
            url=url,
            params=params if params else None,
            headers=headers,
            timeout=(float(settings["connect_timeout"]), float(settings["read_timeout"])),
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError, HTTPError, URLError, OSError):
        return None


def _qmt_extract_series_payload(data, code: str = ""):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if code and isinstance(data.get(code), dict):
            return data.get(code)
        for value in data.values():
            if isinstance(value, list):
                return value
            if isinstance(value, dict) and any(isinstance(v, list) for v in value.values()):
                return value
    return None


def _extract_first_sequence(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        preferred_keys = (
            "data",
            "items",
            "bars",
            "klines",
            "kline",
            "list",
            "rows",
            "records",
            "candles",
            "values",
        )
        for key in preferred_keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
        for value in data.values():
            found = _extract_first_sequence(value)
            if isinstance(found, list) and found:
                return found
    return []


def _bar_from_sequence_item(item) -> Optional[Dict]:
    if not isinstance(item, (list, tuple)) or len(item) < 5:
        return None
    time_value = item[0]
    open_value = _safe_float(item[1]) or 0.0
    high_value = _safe_float(item[2]) or 0.0
    low_value = _safe_float(item[3]) or 0.0
    close_value = _safe_float(item[4]) or 0.0
    volume_value = _safe_float(item[5]) or 0.0 if len(item) >= 6 else 0.0
    if close_value <= 0:
        return None
    return {
        "time": time_value,
        "open": open_value,
        "high": high_value,
        "low": low_value,
        "close": close_value,
        "volume": volume_value,
    }


def _qmt_series_to_bars(series_data) -> List[Dict]:
    if isinstance(series_data, list):
        bars = []
        for item in series_data:
            if isinstance(item, dict):
                normalized = {
                    "time": item.get("time", item.get("datetime", item.get("date", ""))),
                    "open": _safe_float(item.get("open", item.get("openPrice"))) or 0.0,
                    "high": _safe_float(item.get("high", item.get("highPrice"))) or 0.0,
                    "low": _safe_float(item.get("low", item.get("lowPrice"))) or 0.0,
                    "close": _safe_float(item.get("close", item.get("lastPrice", item.get("price")))) or 0.0,
                    "volume": _safe_float(item.get("volume", item.get("vol", item.get("amount")))) or 0.0,
                }
                if normalized["close"] > 0:
                    bars.append(normalized)
            else:
                normalized = _bar_from_sequence_item(item)
                if normalized:
                    bars.append(normalized)
        return bars
    if not isinstance(series_data, dict):
        return []
    times = list(series_data.get("time", []) or [])
    opens = list(series_data.get("open", []) or [])
    highs = list(series_data.get("high", []) or [])
    lows = list(series_data.get("low", []) or [])
    closes = list(series_data.get("close", series_data.get("lastPrice", [])) or [])
    volumes = list(series_data.get("volume", series_data.get("vol", [])) or [])
    size = min(len(times), len(opens), len(highs), len(lows), len(closes))
    if size <= 0:
        return []
    bars = []
    for idx in range(size):
        close_value = _safe_float(closes[idx]) or 0.0
        if close_value <= 0:
            continue
        bars.append({
            "time": times[idx],
            "open": _safe_float(opens[idx]) or 0.0,
            "high": _safe_float(highs[idx]) or 0.0,
            "low": _safe_float(lows[idx]) or 0.0,
            "close": close_value,
            "volume": _safe_float(volumes[idx]) or 0.0,
        })
    return bars


def _warmup_qmt_history(code: str, period: str) -> None:
    _qmt_get("/api/stock/data", params={"stock_list": _qmt_code(code), "period": period})


def _fetch_intraday_minute_bars(code: str, start_hm: str = "09:30") -> Optional[List[Dict]]:
    qmt_code = _qmt_code(code)
    now = datetime.now()
    attempts = [
        {
            "method": "get_intraday_minute_data",
            "params": {"code": qmt_code, "date_str": now.strftime("%Y%m%d"), "start_hm": start_hm},
        },
        {
            "method": "get_intraday_minute_data",
            "params": {"code": qmt_code, "date_str": now.strftime("%Y%m%d"), "start_hm": start_hm, "end_hm": now.strftime("%H:%M")},
        },
    ]
    for attempt in attempts:
        result = _qmt_request("/rpc/data_fetcher", payload=attempt)
        data = (result or {}).get("data")
        if result and result.get("success") and data:
            bars = _qmt_series_to_bars(data) or _qmt_series_to_bars(_extract_first_sequence(data))
            if bars:
                return bars

    _warmup_qmt_history(code, "1m")
    history_attempts = [
        {"code": qmt_code, "period": "1m", "count": 240},
        {"code": [qmt_code], "period": "1m", "count": 240},
    ]
    for params in history_attempts:
        fallback = _qmt_request("/rpc/data_fetcher", payload={"method": "get_history_data", "params": params})
        fallback_data = (fallback or {}).get("data")
        if not fallback or not fallback.get("success") or not fallback_data:
            continue
        series_payload = _qmt_extract_series_payload(fallback_data, qmt_code)
        bars = _qmt_series_to_bars(series_payload)
        if not bars:
            bars = _qmt_series_to_bars(_extract_first_sequence(fallback_data))
        if bars:
            return bars
    return None


def fetch_minute_prices(code: str, lookback: int = 5) -> Optional[List[float]]:
    now = datetime.now()
    start_h = now.hour
    start_m = max(now.minute - lookback, 0)
    if now.minute < lookback:
        start_h = max(start_h - 1, 9)
        start_m = 60 - (lookback - now.minute)
    bars = _fetch_intraday_minute_bars(code, start_hm=f"{start_h:02d}:{start_m:02d}")
    if not bars:
        return None
    prices = [float(bar.get("close", 0) or 0) for bar in bars if float(bar.get("close", 0) or 0) > 0]
    return prices[-max(lookback, 1):] or None


def fetch_full_intraday_bars(code: str) -> Optional[List[Dict]]:
    return _fetch_intraday_minute_bars(code, start_hm="09:30")


def fetch_yesterday_daily(code: str) -> Optional[Dict]:
    result = _qmt_request("/rpc/data_fetcher", payload={
        "method": "get_history_data",
        "params": {"code": _qmt_code(code), "period": "1d", "count": 2},
    })
    data = (result or {}).get("data")
    if not result or not result.get("success") or not data:
        return None
    series_payload = _qmt_extract_series_payload(data, _qmt_code(code))
    series_bars = _qmt_series_to_bars(series_payload)
    if series_bars:
        bar = series_bars[-2] if len(series_bars) >= 2 else series_bars[-1]
        return {"high": bar["high"], "low": bar["low"], "close": bar["close"]}
    raw_list = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [])
    if not raw_list:
        return None
    bar = raw_list[-2] if len(raw_list) >= 2 else raw_list[-1]
    high = _safe_float(bar.get("high", bar.get("highPrice")))
    low = _safe_float(bar.get("low", bar.get("lowPrice")))
    close = _safe_float(bar.get("close", bar.get("lastPrice")))
    if high is None or low is None or close is None:
        return None
    return {"high": high, "low": low, "close": close}


def fetch_auction_snapshot(code: str) -> Optional[Dict]:
    result = _qmt_request("/rpc/data_fetcher", payload={
        "method": "get_auction_data",
        "params": {"codes": [_qmt_code(code)], "date": datetime.now().strftime("%Y%m%d")},
    })
    data = (result or {}).get("data")
    if not result or not result.get("success") or not data:
        return None
    item = None
    if isinstance(data, list) and data:
        item = data[0]
    elif isinstance(data, dict):
        item = data.get(_qmt_code(code)) or data.get(code) or next(iter(data.values()), None)
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
    volume = _safe_float(item.get("volume")) or _safe_float(item.get("auction_volume")) or _safe_float(item.get("matched_volume"))
    if auction_price is None and change_percent is None:
        return None
    return {"auction_price": auction_price, "pre_close": pre_close, "change_percent": change_percent, "volume": volume, "raw": item}


def classify_zone(price: float, rule: StockRule) -> str:
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


def _find_pivots(bars: List[Dict], window: int = 3) -> Tuple[List[Tuple[float, str]], List[Tuple[float, str]]]:
    highs = []
    lows = []
    for i in range(window, len(bars) - window):
        is_high = all(bars[i]["high"] > bars[i - j]["high"] and bars[i]["high"] > bars[i + j]["high"] for j in range(1, window + 1))
        is_low = all(bars[i]["low"] < bars[i - j]["low"] and bars[i]["low"] < bars[i + j]["low"] for j in range(1, window + 1))
        if is_high:
            highs.append((bars[i]["high"], str(bars[i].get("time", ""))))
        if is_low:
            lows.append((bars[i]["low"], str(bars[i].get("time", ""))))
    return highs, lows


def _compute_ma(bars: List[Dict], period: int) -> float:
    subset = bars[-period:] if len(bars) >= period else bars
    return sum(b["close"] for b in subset) / len(subset) if subset else 0.0


def _weighted_avg(values_weights: List[Tuple[float, float]]) -> float:
    total_v = 0.0
    total_w = 0.0
    for value, weight in values_weights:
        if value > 0:
            total_v += value * weight
            total_w += weight
    return total_v / total_w if total_w else 0.0


def _avg_true_range(bars: List[Dict], period: int = 20) -> float:
    if not bars:
        return 0.0
    subset = bars[-period:] if len(bars) >= period else bars
    prev_close = subset[0]["open"] or subset[0]["close"]
    ranges = []
    for bar in subset:
        high = float(bar.get("high", 0) or 0)
        low = float(bar.get("low", 0) or 0)
        close = float(bar.get("close", 0) or 0)
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        if tr > 0:
            ranges.append(tr)
        prev_close = close or prev_close
    return sum(ranges) / len(ranges) if ranges else 0.0


def _classify_intraday_structure(
    bars: List[Dict],
    vwap: float,
    ma20: float,
    ma60: float,
    risk_unit: float,
    quote_close: float,
    yd_close: float,
) -> str:
    if not bars or quote_close <= 0:
        return "neutral"
    session_high = max(float(bar.get("high", 0) or 0) for bar in bars)
    session_low = min(float(bar.get("low", 0) or 0) for bar in bars)
    day_range = max(session_high - session_low, 0.0)
    rebound_from_low = quote_close - session_low
    pullback_from_high = session_high - quote_close
    risk = risk_unit or max(quote_close * 0.003, 0.01)
    trend_bias = quote_close - vwap
    ma_bias = ma20 - ma60 if ma20 > 0 and ma60 > 0 else 0.0
    if (
        yd_close > 0
        and quote_close < yd_close
        and rebound_from_low >= risk * 1.2
        and (yd_close - session_low) >= risk * 1.8
    ):
        return "panic_rebound"
    if trend_bias >= risk and ma_bias >= 0 and pullback_from_high <= risk * 0.8:
        return "trend_up"
    if trend_bias <= -risk and ma_bias <= 0 and rebound_from_low <= risk * 0.8:
        return "trend_down"
    if day_range >= risk * 2.2 and abs(trend_bias) <= risk * 0.8:
        return "range"
    return "neutral"


def _assess_confidence(values: List[float], threshold_pct: float = 0.5) -> str:
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
    if cluster_count >= 1:
        return "中"
    return "低"


def compute_intraday_analysis(code: str, rule: StockRule, bars: List[Dict], yesterday: Optional[Dict]) -> Optional[IntradayAnalysis]:
    if not bars or len(bars) < 15:
        return None
    total_tp_vol = 0.0
    total_vol = 0.0
    for bar in bars:
        tp = (bar["high"] + bar["low"] + bar["close"]) / 3
        total_tp_vol += tp * bar["volume"]
        total_vol += bar["volume"]
    vwap = total_tp_vol / total_vol if total_vol > 0 else bars[-1]["close"]
    pivot_highs, pivot_lows = _find_pivots(bars, window=3)
    yd_high = yesterday["high"] if yesterday else 0.0
    yd_low = yesterday["low"] if yesterday else 0.0
    yd_close = yesterday["close"] if yesterday else 0.0
    if yd_high > 0 and yd_low > 0 and yd_close > 0:
        pp = (yd_high + yd_low + yd_close) / 3
        s1 = 2 * pp - yd_high
        r1 = 2 * pp - yd_low
    else:
        pp = s1 = r1 = 0.0
    ma20 = _compute_ma(bars, 20)
    ma60 = _compute_ma(bars, 60)
    risk_unit = _avg_true_range(bars, 20)
    if risk_unit <= 0:
        ref_close = bars[-1]["close"]
        risk_unit = max(ref_close * 0.0035, 0.01)
    recent_support = pivot_lows[-1][0] if pivot_lows else 0.0
    recent_resistance = pivot_highs[-1][0] if pivot_highs else 0.0
    vwap_lower = vwap - risk_unit * 0.35 if vwap > 0 else 0.0
    vwap_upper = vwap + risk_unit * 0.35 if vwap > 0 else 0.0
    ma_support = min(ma20, ma60) if ma20 > 0 and ma60 > 0 else (ma20 or ma60)
    ma_resistance = max(ma20, ma60) if ma20 > 0 and ma60 > 0 else (ma20 or ma60)
    structure = _classify_intraday_structure(
        bars=bars,
        vwap=vwap,
        ma20=ma20,
        ma60=ma60,
        risk_unit=risk_unit,
        quote_close=bars[-1]["close"],
        yd_close=yd_close,
    )
    if structure == "trend_up":
        t_buy_target = _weighted_avg([(vwap_lower, 0.45), (ma_support, 0.25), (recent_support, 0.20), (pp, 0.10)])
        t_sell_target = _weighted_avg([(recent_resistance, 0.35), (vwap_upper + risk_unit * 0.6, 0.35), (r1, 0.30)])
    elif structure == "trend_down":
        t_buy_target = _weighted_avg([(recent_support, 0.30), (vwap_lower - risk_unit * 0.4, 0.35), (s1, 0.35)])
        t_sell_target = _weighted_avg([(vwap_upper, 0.35), (recent_resistance, 0.25), (ma_resistance, 0.20), (pp, 0.20)])
    elif structure == "panic_rebound":
        panic_floor = min(filter(lambda x: x > 0, [recent_support, s1, bars[-1]["low"]]), default=0.0)
        t_buy_target = _weighted_avg([(panic_floor, 0.40), (bars[-1]["close"] - risk_unit * 0.6, 0.35), (vwap_lower, 0.25)])
        t_sell_target = _weighted_avg([(vwap_upper, 0.35), (pp, 0.30), (recent_resistance, 0.20), (bars[-1]["close"] + risk_unit * 0.8, 0.15)])
    else:
        t_buy_target = _weighted_avg([(recent_support, 0.30), (vwap_lower, 0.25), (s1, 0.25), (ma_support, 0.20)])
        t_sell_target = _weighted_avg([(recent_resistance, 0.30), (vwap_upper, 0.25), (r1, 0.25), (ma_resistance, 0.20)])
    t_buy_target = max(t_buy_target, 0.0)
    t_sell_target = max(t_sell_target, 0.0)
    min_gap = risk_unit * (1.1 if structure in ("trend_up", "range") else 0.9)
    if t_buy_target > 0 and t_sell_target > 0 and t_sell_target - t_buy_target < min_gap:
        mid = (t_buy_target + t_sell_target) / 2
        t_buy_target = max(mid - min_gap / 2, 0.0)
        t_sell_target = mid + min_gap / 2
    t_spread_pct = (t_sell_target - t_buy_target) / t_buy_target * 100 if t_buy_target > 0 and t_sell_target > t_buy_target else 0.0
    buy_conf = _assess_confidence([v for v in [recent_support, vwap_lower, s1, ma_support] if v > 0])
    sell_conf = _assess_confidence([v for v in [recent_resistance, vwap_upper, r1, ma_resistance] if v > 0])
    conf_rank = {"高": 2, "中": 1, "低": 0}
    overall_conf = buy_conf if conf_rank.get(buy_conf, 0) <= conf_rank.get(sell_conf, 0) else sell_conf
    intraday_trend_pct = (bars[-1]["close"] - bars[0]["open"]) / bars[0]["open"] * 100 if bars[0]["open"] > 0 else 0.0
    day_range_pct = (max(bar["high"] for bar in bars) - min(bar["low"] for bar in bars)) / bars[0]["open"] * 100 if bars[0]["open"] > 0 else 0.0
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
        structure=structure,
        risk_unit=round(risk_unit, 3),
        intraday_trend_pct=round(intraday_trend_pct, 2),
        day_range_pct=round(day_range_pct, 2),
    )


def adjust_for_strategy(analysis: IntradayAnalysis, rule: StockRule) -> IntradayAnalysis:
    conf_rank = {"高": 2, "中": 1, "低": 0}
    rank_conf = {2: "高", 1: "中", 0: "低"}
    strategy = rule.strategy or ""
    if (rule.watch_mode or "").lower() == "light":
        analysis.t_buy_target = 0.0
        analysis.confidence = "低"
    elif "顺T" in strategy:
        if analysis.structure == "trend_up" and analysis.t_buy_target > 0:
            analysis.t_buy_target = round(min(analysis.t_buy_target, analysis.vwap - analysis.risk_unit * 0.2), 2)
        elif analysis.structure not in ("trend_up", "range"):
            analysis.confidence = rank_conf.get(max(conf_rank.get(analysis.confidence, 0) - 1, 0), "低")
    elif "逆T" in strategy:
        analysis.confidence = rank_conf.get(max(conf_rank.get(analysis.confidence, 0) - 1, 0), "低")
        if analysis.structure != "panic_rebound":
            analysis.t_buy_target = round(max(analysis.t_buy_target - analysis.risk_unit * 0.3, 0.0), 2)
    elif "箱体" in strategy and analysis.t_spread_pct < 1.5:
        analysis.confidence = "低"
    if "箱体" in strategy and analysis.structure != "range":
        analysis.confidence = rank_conf.get(max(conf_rank.get(analysis.confidence, 0) - 1, 0), "低")
    return analysis


def get_analysis_phase(now: Optional[datetime] = None) -> str:
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


def should_use_dynamic_range(rule: StockRule, quote: Dict, threshold_pct: float = 8.0) -> bool:
    price = _safe_float(quote.get("price"))
    if not price or price <= 0:
        return False
    buy_mid = (rule.buy_range[0] + rule.buy_range[1]) / 2
    return buy_mid > 0 and abs(price - buy_mid) / buy_mid * 100 > threshold_pct


def get_quotes_map(codes: List[str]) -> Dict[str, Dict]:
    qmt_quotes = fetch_batch_realtime_quotes(codes)
    missing = [code for code in codes if str(code) not in qmt_quotes]
    merged = dict(qmt_quotes)
    for requested_code, quote in zip(missing, get_quotes(missing)):
        if "error" not in quote:
            merged[str(requested_code)] = quote
    return merged


def build_market_context(quotes: Dict[str, Dict]) -> MarketContext:
    index_changes = {}
    for code, name in MARKET_INDEXES:
        quote = quotes.get(code)
        if not quote:
            continue
        try:
            index_changes[name] = float(quote.get("change_percent", 0))
        except (TypeError, ValueError):
            continue
    avg = sum(index_changes.values()) / len(index_changes) if index_changes else 0.0
    regime = "neutral"
    if index_changes:
        if avg <= -1.2 or min(index_changes.values()) <= -2.0:
            regime = "weak"
        elif avg >= 1.0:
            regime = "strong"
    return MarketContext(regime=regime, avg_change_pct=avg, index_changes=index_changes)


def format_market_context(market: MarketContext) -> str:
    if not market.index_changes:
        return "市场状态：未知（指数行情缺失）"
    names = [f"{name}{change:+.2f}%" for name, change in market.index_changes.items()]
    return f"市场状态：{market.regime} (均值 {market.avg_change_pct:+.2f}% / {'，'.join(names)})"


def detect_panic_rebound(rule: StockRule, quote: Dict, market: MarketContext) -> bool:
    if not rule.allow_market_panic_reverse_t or market.regime != "weak":
        return False
    price = _safe_float(quote.get("price")) or 0.0
    low = _safe_float(quote.get("low")) or 0.0
    change_pct = _safe_float(quote.get("change_percent")) or 0.0
    if price <= 0 or low <= 0:
        return False
    rebound_pct = (price - low) / low * 100
    return change_pct <= -5.0 and rebound_pct >= rule.panic_rebound_pct


def derive_signal_action(analysis: IntradayAnalysis, rule: StockRule, quote: Dict, market: MarketContext) -> str:
    price = float(quote.get("price", 0) or 0)
    risk_band = max(analysis.risk_unit * 0.35, price * 0.002 if price > 0 else 0.0)
    near_buy = analysis.t_buy_target > 0 and price <= analysis.t_buy_target + risk_band
    near_sell = analysis.t_sell_target > 0 and price >= analysis.t_sell_target - risk_band
    if analysis.structure == "trend_down" and near_buy:
        return "wait"
    if near_buy and not near_sell:
        return "buy"
    if near_sell and not near_buy:
        return "sell"
    if detect_panic_rebound(rule, quote, market) and analysis.t_buy_target > 0:
        return "buy"
    return "wait"


def opportunity_score(
    analysis: IntradayAnalysis,
    rule: StockRule,
    quote: Dict,
    market: MarketContext,
    learning: LearningProfile,
) -> Tuple[int, str, str, str]:
    price = float(quote.get("price", 0) or 0)
    score = 0
    reasons = []
    action = derive_signal_action(analysis, rule, quote, market)
    if analysis.confidence == "高":
        score += 35
        reasons.append("分时共振强")
    elif analysis.confidence == "中":
        score += 20
        reasons.append("分时共振一般")
    if analysis.t_spread_pct >= 2.0:
        score += 25
        reasons.append(f"预期价差{analysis.t_spread_pct:.1f}%")
    elif analysis.t_spread_pct >= 1.2:
        score += 12
        reasons.append(f"预期价差{analysis.t_spread_pct:.1f}%")
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
        score -= 12
        reasons.append("分时下压结构")
    if action == "buy":
        score += 18
        reasons.append("贴近T买点")
        if detect_panic_rebound(rule, quote, market):
            score += 12
            reasons.append("弱市恐慌反抽")
    elif action == "sell":
        score += 18
        reasons.append("贴近T卖点")
    if market.regime == "weak" and action == "buy":
        score -= 12
        reasons.append("大盘偏弱")
    if market.regime == "strong" and action == "sell":
        score -= 5
        reasons.append("大盘偏强")
    if learning.bias == "aggressive":
        score += 8
        reasons.append(f"近端实盘正反馈({learning.sample_count}笔)")
    elif learning.bias == "defensive":
        score -= 12
        reasons.append(f"近端实盘偏弱({learning.sample_count}笔)")
    if learning.preferred_structure and learning.preferred_structure == analysis.structure:
        score += 6
        reasons.append(f"结构复盘偏好({analysis.structure})")
    forecast_weight = 10 if analysis.forecast_confidence == "高" else 6 if analysis.forecast_confidence == "中" else 3
    if analysis.forecast_bias == "bullish":
        if action == "buy":
            score += forecast_weight
            reasons.append(f"TimeFM预测偏强({analysis.forecast_return_pct:+.2f}%)")
        elif action == "sell":
            score -= forecast_weight
            reasons.append("TimeFM预测不支持追卖")
    elif analysis.forecast_bias == "bearish":
        if action == "sell":
            score += forecast_weight
            reasons.append(f"TimeFM预测偏弱({analysis.forecast_return_pct:+.2f}%)")
        elif action == "buy":
            score -= forecast_weight
            reasons.append("TimeFM预测不支持抄底")
    score = max(0, min(score, 100))
    level = "S" if score >= 75 else ("A" if score >= 60 else ("B" if score >= 40 else "C"))
    return score, level, " / ".join(reasons) or "等待更优位置", action


def should_auto_trade_signal(
    analysis: IntradayAnalysis,
    rule: StockRule,
    quote: Dict,
    market: MarketContext,
    learning: LearningProfile,
) -> bool:
    score, level, _, action = opportunity_score(analysis, rule, quote, market, learning)
    if action not in ("buy", "sell"):
        return False
    if level not in ("S", "A"):
        return False
    if analysis.confidence not in ("高", "中"):
        return False
    if analysis.t_spread_pct < 1.2 and action == "buy":
        return False
    if learning.bias == "defensive" and action == "buy":
        return False
    return score >= 60


def format_t_signal(
    analysis: IntradayAnalysis,
    rule: StockRule,
    quote: Dict,
    market: MarketContext,
    learning: LearningProfile,
    is_dynamic: bool = False,
    auto_trade_enabled: bool = False,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    price = float(quote.get("price", 0))
    change_pct = float(quote.get("change_percent", 0))
    score, level, reason, action_code = opportunity_score(analysis, rule, quote, market, learning)
    dynamic_tag = " [动态区间]" if is_dynamic else ""
    if action_code == "buy":
        action = f"T买 {rule.per_trade_shares} 股"
        trigger_line = f"触发参考：<= {analysis.t_buy_target:.2f}"
        target_line = f"回补/反抽目标：{analysis.t_sell_target:.2f}"
        abort_line = f"放弃条件：跌回日内低点附近，或无法站稳 {analysis.t_buy_target:.2f}"
    elif action_code == "sell":
        action = f"T卖 {rule.per_trade_shares} 股"
        trigger_line = f"触发参考：>= {analysis.t_sell_target:.2f}"
        target_line = f"回落接回参考：{analysis.t_buy_target:.2f}" if analysis.t_buy_target > 0 else "回落接回参考：等待下一支撑"
        abort_line = f"放弃条件：放量突破后继续走强，或无法回落到 {analysis.t_buy_target:.2f}"
    else:
        action = "观望"
        trigger_line = "触发参考：等待更贴近支撑/压力"
        target_line = f"关注区间：T买 {analysis.t_buy_target:.2f} / T卖 {analysis.t_sell_target:.2f}"
        abort_line = "放弃条件：价差继续收窄或结构失真"
    auto_line = "是" if auto_trade_enabled and should_auto_trade_signal(analysis, rule, quote, market, learning) else "否"
    forecast_line = analysis.forecast_summary if analysis.forecast_summary else "TimeFM：未启用"
    return (
        f"📊 做T大脑信号{dynamic_tag}\n"
        f"时间：{now}\n"
        f"股票：{rule.name} ({rule.code})\n"
        f"现价：{price:.2f} / 涨跌幅 {change_pct:+.2f}%\n"
        f"机会评分：{score}/100 ({level})\n"
        f"核心依据：{reason}\n"
        f"VWAP：{analysis.vwap:.2f} / MA20：{analysis.ma20:.2f} / MA60：{analysis.ma60:.2f}\n"
        f"结构：{analysis.structure} / 波动单位：{analysis.risk_unit:.3f}\n"
        f"{forecast_line}\n"
        f"建议动作：{action}\n"
        f"{trigger_line}\n"
        f"{target_line}\n"
        f"预期价差：{analysis.t_spread_pct:.1f}% / 置信度：{analysis.confidence}\n"
        f"当前下单参考：{price:.2f}\n"
        f"自动交易：{auto_line}\n"
        f"{abort_line}\n"
        f"学习状态：样本{learning.sample_count} 胜率{learning.win_rate:.0%} 单笔均值{learning.avg_profit:.2f}"
        f" 风险系数{learning.risk_factor:.2f}"
        f"{(' 偏好结构' + learning.preferred_structure) if learning.preferred_structure else ''}\n"
        f"{format_market_context(market)}\n"
        f"提示：消息已给出明确买卖点，自动交易仅在高分信号下触发。"
    )


def should_send_t_signal(state: Dict, code: str, cooldown_minutes: int, max_per_day: int) -> bool:
    intraday = state.setdefault("intraday", {})
    stock_data = intraday.get(code, {})
    today = datetime.now().strftime("%Y-%m-%d")
    if stock_data.get("date") != today:
        return True
    if stock_data.get("t_signal_count", 0) >= max_per_day:
        return False
    last_sent = stock_data.get("last_t_signal")
    if not last_sent:
        return True
    try:
        delta = (datetime.now() - datetime.fromisoformat(last_sent)).total_seconds() / 60
        return delta >= cooldown_minutes
    except ValueError:
        return True


def mark_t_signal_sent(state: Dict, code: str, analysis: IntradayAnalysis) -> None:
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


def is_first_crossing(code: str, rule: StockRule, current_zone: str, state: Dict) -> bool:
    zones = state.setdefault("zones", {})
    last_zone = zones.get(code)
    if last_zone != current_zone:
        zones[code] = current_zone
        return True
    minute_prices = fetch_minute_prices(code, lookback=5)
    if minute_prices and len(minute_prices) >= 2 and current_zone in ("buy", "sell"):
        low, high = rule.buy_range if current_zone == "buy" else rule.sell_range
        return any(p < low or p > high for p in minute_prices[:-1])
    return False


def search_stock_news(stock_name: str, reason: str = "异动") -> str:
    if not MX_APIKEY:
        return ""
    query = f"{stock_name}{reason}原因分析"
    req = Request(
        f"{MX_API_BASE}/api/claw/news-search",
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json", "apikey": MX_APIKEY},
        method="POST",
    )
    try:
        with _build_url_opener().open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (HTTPError, URLError, OSError):
        return ""
    items = ((((data.get("data") or {}).get("data") or {}).get("llmSearchResponse") or {}).get("data")) or []
    lines = []
    for item in items[:3]:
        title = str(item.get("title", "")).strip()
        content = str(item.get("content", "")).strip()
        if content and len(content) > 120:
            content = content[:120] + "..."
        if title:
            lines.append(f"  · {title}")
            if content:
                lines.append(f"    {content}")
    return "\n".join(lines)


def format_preopen_warning(rule: StockRule, market: MarketContext, auction: Optional[Dict], quote: Dict, reasons: List[str], level: str) -> str:
    quote_price = _safe_float(quote.get("price")) or 0.0
    quote_change = _safe_float(quote.get("change_percent")) or 0.0
    auction_price = _safe_float((auction or {}).get("auction_price"))
    auction_change = _safe_float((auction or {}).get("change_percent"))
    action = "开盘先观察，不开盘即逆T抄底；先卖后买优先。" if level == "high" else "缩小预期，承接确认后再轻仓执行。"
    auction_line = "竞价数据：暂无"
    if auction_price is not None or auction_change is not None:
        auction_line = f"竞价数据：价格 {auction_price:.2f}" if auction_price is not None else "竞价数据：价格未知"
        if auction_change is not None:
            auction_line += f" / 涨跌幅 {auction_change:+.2f}%"
    return (
        f"⚠️ {'盘前弱势预警' if level == 'high' else '盘前谨慎提醒'}\n"
        f"股票：{rule.name} ({rule.code})\n"
        f"{format_market_context(market)}\n"
        f"参考现价：{quote_price:.2f} / 涨跌幅 {quote_change:+.2f}%\n"
        f"{auction_line}\n"
        f"触发原因：{'；'.join(reasons)}\n"
        f"动作建议：{action}"
    )


def evaluate_preopen_warning(rule: StockRule, quote: Dict, auction: Optional[Dict], market: MarketContext) -> Optional[Tuple[str, str]]:
    reasons = []
    level = "medium"
    if market.regime == "weak":
        reasons.append("指数环境偏弱")
        level = "high"
    ref_price = _safe_float((auction or {}).get("auction_price")) or _safe_float(quote.get("price"))
    ref_change = _safe_float((auction or {}).get("change_percent")) or _safe_float(quote.get("change_percent"))
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
    return level, format_preopen_warning(rule, market, auction, quote, reasons, level)


def apply_preopen_plan_override(rule: StockRule, plan: Dict, market: MarketContext, auction: Optional[Dict], warning_level: str) -> None:
    override = upsert_plan_override(plan, rule)
    ref_price = _safe_float((auction or {}).get("auction_price")) or 0.0
    override["preopen_risk_mode"] = "defensive" if warning_level == "high" else "cautious"
    override["avoid_reverse_t"] = True
    override["abandon_buy_below"] = round(min(ref_price, rule.stop_loss), 2) if warning_level == "high" and ref_price > 0 else round(ref_price or rule.buy_range[0], 2)


def evaluate_rule(rule: StockRule, quote: Dict) -> Optional[str]:
    price = float(quote["price"])
    if price <= rule.stop_loss:
        return "risk"
    if rule.buy_range[0] <= price <= rule.buy_range[1]:
        return "buy"
    if rule.sell_range[0] <= price <= rule.sell_range[1]:
        return "sell"
    return None


def evaluate_rebound_watch(rule: StockRule, quote: Dict, state: Dict) -> Optional[str]:
    if not rule.allow_rebound_watch_after_stop or rule.rebound_buy_above <= 0:
        return None
    if float(quote["price"]) < rule.rebound_buy_above:
        return None
    if state.setdefault("zones", {}).get(rule.code) != "risk":
        return None
    return "rebound_buy"


def has_obvious_price_scale_mismatch(rule: StockRule, quote: Dict) -> bool:
    price = float(quote.get("price", 0) or 0)
    ref = max(rule.buy_range[0], rule.sell_range[0], rule.stop_loss)
    return price > 0 and ref > 0 and 50 <= ref / price <= 200


def should_suppress_buy_signal(rule: StockRule, quote: Dict, market: MarketContext) -> bool:
    price = float(quote.get("price", 0) or 0)
    if rule.buy_blocked:
        return True
    if rule.abandon_buy_below and price > 0 and price <= rule.abandon_buy_below:
        return True
    if rule.avoid_reverse_t:
        return True
    if market.regime != "weak":
        return False
    if detect_panic_rebound(rule, quote, market):
        return False
    return "逆T" not in (rule.strategy or "") or float(quote.get("change_percent", 0) or 0) <= -7.0


def format_signal(rule: StockRule, quote: Dict, signal_type: str, market: MarketContext, news_context: str = "") -> str:
    price = float(quote["price"])
    change_pct = float(quote.get("change_percent", 0))
    action = {
        "buy": f"T买 {rule.per_trade_shares} 股",
        "rebound_buy": f"T买 {rule.per_trade_shares} 股",
        "sell": f"T卖 {rule.per_trade_shares} 股",
        "risk": "停止逆T，先防守",
    }[signal_type]
    trigger = {
        "buy": f"买区 {rule.buy_range[0]:.2f}-{rule.buy_range[1]:.2f}",
        "rebound_buy": f"反弹确认位 >= {rule.rebound_buy_above:.2f}",
        "sell": f"卖区 {rule.sell_range[0]:.2f}-{rule.sell_range[1]:.2f}",
        "risk": f"风险线 <= {rule.stop_loss:.2f}",
    }[signal_type]
    follow = {
        "buy": f"放弃条件：跌破 {rule.stop_loss:.2f} 或承接消失",
        "rebound_buy": f"放弃条件：重新跌回 {rule.rebound_buy_above:.2f} 下方",
        "sell": f"回补观察：回落到买区 {rule.buy_range[0]:.2f}-{rule.buy_range[1]:.2f}",
        "risk": "等待重新站稳后再评估",
    }[signal_type]
    extra_lines = []
    if rule.buy_blocked and rule.buy_block_reason and signal_type in ("buy", "rebound_buy"):
        extra_lines.append(f"禁买原因：{rule.buy_block_reason}")
    if news_context:
        extra_lines.append(f"--- 相关资讯 ---\n{news_context}")
    extra = f"\n{chr(10).join(extra_lines)}" if extra_lines else ""
    return (
        f"🔔 {'风险提醒' if signal_type == 'risk' else '交易观察提醒'}\n"
        f"股票：{rule.name} ({rule.code})\n"
        f"现价：{price:.2f} / 涨跌幅 {change_pct:+.2f}%\n"
        f"策略：{rule.strategy} / 单次数量：{rule.per_trade_shares}\n"
        f"{format_market_context(market)}\n"
        f"建议动作：{action}\n"
        f"触发参考：{trigger}\n"
        f"当前下单参考：{price:.2f}\n"
        f"{follow}{extra}"
    )

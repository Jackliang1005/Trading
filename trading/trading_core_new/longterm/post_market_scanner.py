from __future__ import annotations

import json
import math
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .llm_runtime import resolve_llm_runtime
from .models import LongTermSettings, PortfolioState, StockCandidate


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _mean(values: List[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / max(1, len(values))


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _is_default_sync_candidate(candidate: Optional[StockCandidate]) -> bool:
    if candidate is None:
        return False
    return (
        str(candidate.thesis or "").strip() == "from_portfolio_sync"
        and abs(float(candidate.value_score) - 55.0) < 1e-6
        and abs(float(candidate.quality_score) - 55.0) < 1e-6
        and abs(float(candidate.growth_score) - 50.0) < 1e-6
        and abs(float(candidate.risk_score) - 65.0) < 1e-6
    )


def _derive_position_scores(
    position,
    *,
    nav: float,
    tags: List[str],
) -> Dict[str, float]:
    cost_price = max(0.0, _float(getattr(position, "cost_price", 0.0), 0.0))
    last_price = _float(getattr(position, "last_price", 0.0), 0.0)
    if last_price <= 0:
        last_price = cost_price
    pnl_ratio = ((last_price - cost_price) / cost_price) if cost_price > 0 else 0.0
    market_value = max(0.0, _float(getattr(position, "market_value", 0.0), 0.0))
    weight = (market_value / nav) if nav > 0 else 0.0
    tag_bonus = min(8.0, float(len([x for x in tags if str(x).strip()])) * 0.6)
    board_bonus = 5.0 if str(getattr(position, "code", "")).upper().startswith(("688", "300")) else 0.0
    value_score = _clamp(52.0 + max(0.0, -pnl_ratio) * 40.0 - min(0.18, weight) * 20.0, 20.0, 85.0)
    quality_score = _clamp(48.0 + max(0.0, min(0.15, pnl_ratio)) * 60.0 + tag_bonus * 0.5, 20.0, 85.0)
    growth_score = _clamp(50.0 + pnl_ratio * 55.0 + tag_bonus, 20.0, 90.0)
    risk_score = _clamp(45.0 + weight * 120.0 + max(0.0, -pnl_ratio) * 35.0 + board_bonus, 15.0, 95.0)
    return {
        "value_score": round(value_score, 2),
        "quality_score": round(quality_score, 2),
        "growth_score": round(growth_score, 2),
        "risk_score": round(risk_score, 2),
    }


def _param(settings: Optional[LongTermSettings], name: str, default: float) -> float:
    if settings is None:
        return float(default)
    try:
        return float(getattr(settings, name, default))
    except Exception:
        return float(default)


def _calculate_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(0.0, delta))
        losses.append(max(0.0, -delta))
    avg_gain = _mean(gains[-period:])
    avg_loss = _mean(losses[-period:])
    if avg_loss <= 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _calculate_atr_pct(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    tr_list: List[float] = []
    for i in range(1, n):
        h = highs[i]
        l = lows[i]
        prev_close = closes[i - 1]
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        if math.isfinite(tr):
            tr_list.append(max(0.0, tr))
    if len(tr_list) < period:
        return None
    atr = _mean(tr_list[-period:])
    last_close = closes[-1]
    if last_close <= 0:
        return None
    return atr / last_close


def _build_reference_style_row(
    candidate: StockCandidate,
    quote: Dict,
    history: Dict[str, List[float]],
    heat_info: Optional[Dict] = None,
    settings: Optional[LongTermSettings] = None,
) -> Optional[Dict]:
    closes = [x for x in history.get("close", []) if _float(x) > 0]
    highs = [x for x in history.get("high", []) if _float(x) > 0]
    lows = [x for x in history.get("low", []) if _float(x) > 0]
    volumes = [max(0.0, _float(x)) for x in history.get("volume", [])]
    amounts = [max(0.0, _float(x)) for x in history.get("amount", [])]
    if min(len(closes), len(highs), len(lows), len(volumes)) < 25:
        return None

    close = float(closes[-1])
    ma5 = _mean(closes[-5:])
    ma10 = _mean(closes[-10:])
    ma20 = _mean(closes[-20:])
    vol_ma5 = _mean(volumes[-5:])
    latest_volume = float(volumes[-1])
    latest_amount = float(amounts[-1]) if amounts else latest_volume * close
    bias_ma5 = (close - ma5) / ma5 if ma5 > 0 else 0.0

    rsi14 = _calculate_rsi(closes, period=14)
    atr_pct = _calculate_atr_pct(highs, lows, closes, period=14)
    if rsi14 is None or atr_pct is None:
        return None

    turnover_rate = _float(quote.get("turnover_rate"), 0.0)
    bias_ma5_abs_max = _param(settings, "scan_bias_ma5_abs_max", 0.05)
    atr_min = _param(settings, "scan_atr_min", 0.04)
    turnover_min = _param(settings, "scan_turnover_min", 5.0)
    turnover_max = _param(settings, "scan_turnover_max", 20.0)
    volume_mul = _param(settings, "scan_volume_ma5_multiplier", 1.2)
    rsi_min = _param(settings, "scan_rsi_min", 55.0)
    rsi_max = _param(settings, "scan_rsi_max", 70.0)
    trend_ok = close > ma5 > ma10 > ma20
    not_too_far = abs(bias_ma5) <= bias_ma5_abs_max
    atr_ok = atr_pct > atr_min
    turnover_ok = True if turnover_rate <= 0 else (turnover_min <= turnover_rate <= turnover_max)
    volume_ok = (vol_ma5 > 0 and latest_volume > vol_ma5 * volume_mul)
    rsi_ok = rsi_min <= rsi14 <= rsi_max
    if not (trend_ok and not_too_far and atr_ok and turnover_ok and volume_ok and rsi_ok):
        return None

    atr_score = atr_pct * 100.0
    bias_score = max(0.0, (0.05 - abs(bias_ma5)) * 100.0)
    vol_score = (latest_volume / vol_ma5) if vol_ma5 > 0 else 0.0
    heat_score = _float((heat_info or {}).get("heat_score"), 0.0)
    heat_bonus_cap = _param(settings, "scan_heat_bonus_cap", 8.0)
    heat_bonus_scale = _param(settings, "scan_heat_bonus_scale", 0.08)
    heat_bonus = min(heat_bonus_cap, max(0.0, heat_score) * heat_bonus_scale)
    total_score = atr_score * 0.5 + bias_score * 0.3 + vol_score * 0.2 + heat_bonus

    return {
        "code": str(candidate.code).upper(),
        "name": candidate.name,
        "status": candidate.status,
        "industry": candidate.industry,
        "score": round(max(0.0, min(100.0, total_score)), 3),
        "close": round(close, 4),
        "ma5": round(ma5, 4),
        "ma10": round(ma10, 4),
        "ma20": round(ma20, 4),
        "bias_ma5_pct": round(bias_ma5 * 100.0, 3),
        "atr_pct": round(atr_pct * 100.0, 3),
        "turnover_rate": round(turnover_rate, 3),
        "volume": round(latest_volume, 2),
        "volume_ma5": round(vol_ma5, 2),
        "amount": round(latest_amount, 2),
        "rsi14": round(rsi14, 3),
        "change_percent": _float(quote.get("change_percent"), 0.0),
        "ths_heat_score": round(heat_score, 3),
        "ths_hot_concepts": list((heat_info or {}).get("hot_concepts") or [])[:6],
        "score_breakdown": {
            "atr_score": round(atr_score, 3),
            "bias_score": round(bias_score, 3),
            "volume_score": round(vol_score, 3),
            "heat_bonus": round(heat_bonus, 3),
        },
        "llm": {},
        "scan_mode": "reference_filter",
    }


def _fallback_row(
    candidate: StockCandidate,
    quote: Dict,
    heat_info: Optional[Dict] = None,
    settings: Optional[LongTermSettings] = None,
) -> Dict:
    change_pct = _float(quote.get("change_percent"), 0.0)
    amount = _float(quote.get("amount"), 0.0)
    volume = _float(quote.get("volume"), 0.0)
    change_floor = _param(settings, "scan_fallback_change_bonus_floor", -3.0)
    change_cap = _param(settings, "scan_fallback_change_bonus_cap", 5.0)
    change_divisor = max(_param(settings, "scan_fallback_change_divisor", 2.0), 1e-9)
    amount_divisor = max(_param(settings, "scan_fallback_amount_divisor", 200_000_000.0), 1e-9)
    amount_cap = _param(settings, "scan_fallback_amount_bonus_cap", 6.0)
    volume_divisor = max(_param(settings, "scan_fallback_volume_divisor", 10_000_000.0), 1e-9)
    volume_cap = _param(settings, "scan_fallback_volume_bonus_cap", 3.0)
    change_bonus = max(change_floor, min(change_cap, change_pct / change_divisor))
    score = candidate.composite_score + change_bonus
    if amount > 0:
        liquidity_bonus = min(amount_cap, max(0.0, amount / amount_divisor))
    elif volume > 0:
        liquidity_bonus = min(volume_cap, max(0.0, volume / volume_divisor))
    else:
        liquidity_bonus = 0.0
    score += liquidity_bonus
    heat_score = _float((heat_info or {}).get("heat_score"), 0.0)
    heat_bonus_cap = _param(settings, "scan_heat_bonus_cap", 8.0)
    heat_bonus_scale = _param(settings, "scan_heat_bonus_scale", 0.08)
    heat_bonus = min(heat_bonus_cap, max(0.0, heat_score) * heat_bonus_scale)
    score += heat_bonus
    return {
        "code": str(candidate.code).upper(),
        "name": candidate.name,
        "status": candidate.status,
        "industry": candidate.industry,
        "score": round(max(0.0, min(100.0, score)), 3),
        "price": _float(quote.get("price"), 0.0),
        "change_percent": change_pct,
        "amount": amount,
        "volume": volume,
        "ths_heat_score": round(heat_score, 3),
        "ths_hot_concepts": list((heat_info or {}).get("hot_concepts") or [])[:6],
        "score_breakdown": {
            "base_composite": round(candidate.composite_score, 3),
            "change_bonus": round(change_bonus, 3),
            "liquidity_bonus": round(liquidity_bonus, 3),
            "heat_bonus": round(heat_bonus, 3),
        },
        "llm": {},
        "scan_mode": "fallback_heuristic",
    }


# ---------------------------------------------------------------------------
# Heat Rotation helpers
# ---------------------------------------------------------------------------

_CONCEPT_DB_PATH = Path("/root/qmttrader/concept_db/concepts.db")


def _load_concept_db_date(cursor, trade_date: str) -> str:
    """Return the latest concept DB date <= trade_date."""
    cursor.execute("SELECT MAX(date) FROM hot_concepts WHERE date <= ?", (trade_date,))
    row = cursor.fetchone()
    return str((row or [""])[0] or "").strip()


def _normalize_stock_code(code: str) -> str:
    text = str(code or "").strip().upper()
    if "." in text:
        return text.split(".")[0]
    if text.startswith(("SH", "SZ", "BJ")) and len(text) > 2:
        return text[2:]
    return text


def _raw_strength_for_codes(
    codes: List[str],
    trade_date: str,
    *,
    limit_up_weight: float = 2.0,
    change_weight: float = 18.0,
) -> Dict[str, float]:
    """Compute raw_strength per code on the given trade_date from concept DB."""
    normalized = [_normalize_stock_code(x) for x in codes if str(x or "").strip()]
    out: Dict[str, float] = {code: 0.0 for code in normalized}
    if not normalized or not _CONCEPT_DB_PATH.exists():
        return out
    try:
        with sqlite3.connect(str(_CONCEPT_DB_PATH)) as conn:
            cursor = conn.cursor()
            db_date = _load_concept_db_date(cursor, trade_date)
            if not db_date:
                return out
            placeholders = ",".join(["?"] * len(normalized))
            cursor.execute(
                f"""
                SELECT cs.stock_code, hc.limit_up_num, hc.change
                FROM concept_stocks cs
                JOIN hot_concepts hc
                  ON cs.concept_code = hc.concept_code
                 AND cs.date = hc.date
                WHERE cs.date = ?
                  AND cs.stock_code IN ({placeholders})
                """,
                [db_date, *normalized],
            )
            rows = cursor.fetchall()
        code_sum: Dict[str, float] = {code: 0.0 for code in normalized}
        for row in rows:
            if not row:
                continue
            code, limit_up_val, change_val = row
            code = _normalize_stock_code(str(code or ""))
            if code not in code_sum:
                continue
            strength = (max(0.0, float(limit_up_val or 0)) * float(limit_up_weight)
                        + max(0.0, float(change_val or 0)) * float(change_weight))
            code_sum[code] += strength
        out = {code: round(v, 4) for code, v in code_sum.items()}
    except Exception:
        pass
    return out


def compute_heat_acceleration(
    codes: List[str],
    trade_date: str,
    *,
    window: int = 5,
    limit_up_weight: float = 2.0,
    change_weight: float = 18.0,
) -> Dict[str, float]:
    """Return heat acceleration for each code = (raw_today - raw_n_days_ago) / max(raw_n_days_ago, 1e-6)."""
    if window <= 0:
        return {_normalize_stock_code(c): 0.0 for c in codes}
    raw_today = _raw_strength_for_codes(codes, trade_date,
                                        limit_up_weight=limit_up_weight,
                                        change_weight=change_weight)
    # Calculate the N-days-ago date (approximate — use trade_date - window calendar days)
    from datetime import timedelta
    try:
        dt = datetime.strptime(trade_date, "%Y-%m-%d")
        past_dt = dt - timedelta(days=max(1, int(window)))
        past_date = past_dt.strftime("%Y-%m-%d")
    except Exception:
        past_date = trade_date
    raw_past = _raw_strength_for_codes(codes, past_date,
                                       limit_up_weight=limit_up_weight,
                                       change_weight=change_weight)
    accel: Dict[str, float] = {}
    for code in codes:
        c = _normalize_stock_code(code)
        today = raw_today.get(c, 0.0)
        past = raw_past.get(c, 0.0)
        if past > 1e-6:
            accel[c] = round((today - past) / past, 4)
        else:
            accel[c] = 0.0
    return accel


def _get_ths_sector_cache() -> Dict[str, List[str]]:
    """Read THS sector cache file."""
    cache_file = Path(__file__).resolve().parents[2] / "trading_data" / "longterm" / "ths_sector_cache.json"
    if not cache_file.exists():
        return {}
    try:
        return json.loads(cache_file.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _fetch_hot_concepts(date_key: str) -> List[Dict]:
    """Fetch top hot concepts for a given date from concept DB."""
    if not _CONCEPT_DB_PATH.exists():
        return []
    try:
        with sqlite3.connect(str(_CONCEPT_DB_PATH)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(date) FROM hot_concepts WHERE date <= ?", (date_key,))
            row = cursor.fetchone()
            if not row or not row[0]:
                return []
            db_date = str(row[0])
            cursor.execute(
                """
                SELECT concept_name, limit_up_num, change
                FROM hot_concepts
                WHERE date = ?
                ORDER BY (limit_up_num * 2.0 + change) DESC
                """,
                (db_date,),
            )
            rows = cursor.fetchall()
        return [
            {
                "concept_name": str(r[0] or "").strip(),
                "limit_up_num": float(r[1] or 0),
                "change": float(r[2] or 0),
            }
            for r in rows if r
        ]
    except Exception:
        return []


def compute_sector_momentum(
    codes: List[str],
    trade_date: str,
    *,
    top_n: int = 20,
) -> Dict[str, float]:
    """Compute sector momentum for each code.

    Sector momentum = how hot the code's associated concepts are.
    Normalized to 0-100 based on concept ranking.
    """
    concepts = _fetch_hot_concepts(trade_date)
    if not concepts:
        return {_normalize_stock_code(c): 0.0 for c in codes}

    # Build concept -> momentum score (0-100)
    # Use log1p for limit_up_num to give diminishing returns to very large concepts
    import math
    max_strength = max(
        math.log1p(float(c["limit_up_num"])) * 3.0 + float(c["change"]) for c in concepts
    ) or 1.0
    concept_score: Dict[str, float] = {}
    for c in concepts[:top_n]:
        strength = math.log1p(float(c["limit_up_num"])) * 3.0 + float(c["change"])
        concept_score[c["concept_name"]] = round((strength / max_strength) * 100.0, 3)

    # Map codes to concepts via THS sector cache
    sector_cache = _get_ths_sector_cache()
    out: Dict[str, float] = {}
    for code in codes:
        c = _normalize_stock_code(code)
        tags = [str(x).strip() for x in (sector_cache.get(c) or []) if str(x).strip()]
        if not tags:
            out[c] = 0.0
            continue
        matched_scores = []
        for tag in tags:
            if tag in concept_score:
                matched_scores.append(concept_score[tag])
            else:
                # Fuzzy match
                for cname, cscore in concept_score.items():
                    if (tag in cname) or (cname in tag):
                        matched_scores.append(cscore)
                        break
        if matched_scores:
            out[c] = round(sum(matched_scores) / len(matched_scores), 3)
        else:
            out[c] = 0.0
    return out


def _compute_price_trend_score(
    closes: List[float],
) -> float:
    """Compute price trend score 0-100.

    Uses: MA alignment (MA5/MA10/MA20), RSI health, MA5 deviation.
    """
    if len(closes) < 25:
        return 50.0
    ma5 = _mean(closes[-5:])
    ma10 = _mean(closes[-10:])
    ma20 = _mean(closes[-20:])
    close = closes[-1]

    # MA alignment score (0-40)
    ma_score = 0.0
    if close > ma5 > ma10 > ma20:
        ma_score = 40.0
    elif close > ma5 > ma10:
        ma_score = 30.0
    elif close > ma5:
        ma_score = 20.0
    elif close > ma10:
        ma_score = 10.0

    # RSI health score (0-30)
    rsi = _calculate_rsi(closes, period=14) or 50.0
    if 55 <= rsi <= 70:
        rsi_score = 30.0
    elif 50 <= rsi < 55:
        rsi_score = 20.0
    elif 45 <= rsi < 50:
        rsi_score = 10.0
    elif rsi > 70:
        rsi_score = 20.0  # Still strong but overbought discount
    else:
        rsi_score = 5.0

    # MA5 deviation score (0-30)
    if ma5 > 0:
        bias = abs((close - ma5) / ma5)
        if bias <= 0.02:
            bias_score = 30.0
        elif bias <= 0.05:
            bias_score = 20.0
        elif bias <= 0.08:
            bias_score = 10.0
        else:
            bias_score = 0.0
    else:
        bias_score = 0.0

    return ma_score + rsi_score + bias_score


def _compute_liquidity_score(
    quote: Dict,
    history: Dict[str, List[float]],
) -> float:
    """Compute liquidity quality score 0-100.

    Uses: amount percentile, volume ratio, turnover rate moderation.
    """
    amounts = [max(0.0, _float(x)) for x in history.get("amount", [])]
    volumes = [max(0.0, _float(x)) for x in history.get("volume", [])]

    latest_amount = _float(quote.get("amount"), 0.0)
    latest_volume = _float(quote.get("volume"), 0.0)
    turnover = _float(quote.get("turnover_rate"), 0.0)

    if latest_amount <= 0 and latest_volume <= 0:
        return 0.0

    # Amount percentile score (0-40)
    if amounts and latest_amount > 0 and len(amounts) >= 5:
        sorted_amounts = sorted(amounts)
        rank = sum(1 for a in sorted_amounts if a < latest_amount)
        pct = rank / len(sorted_amounts)
        if pct >= 0.7:
            amt_score = 40.0
        elif pct >= 0.5:
            amt_score = 30.0
        elif pct >= 0.3:
            amt_score = 20.0
        else:
            amt_score = 10.0
    else:
        amt_score = 10.0

    # Volume ratio score (0-30) — latest vs MA5
    if volumes and latest_volume > 0 and len(volumes) >= 5:
        vol_ma5 = _mean(volumes[-5:])
        if vol_ma5 > 0:
            ratio = latest_volume / vol_ma5
            if 1.2 <= ratio <= 3.0:
                vol_score = 30.0
            elif 1.0 <= ratio < 1.2:
                vol_score = 20.0
            elif 0.8 <= ratio < 1.0:
                vol_score = 10.0
            elif ratio > 3.0:
                vol_score = 15.0  # Too high volume = suspicious
            else:
                vol_score = 5.0
        else:
            vol_score = 5.0
    else:
        vol_score = 5.0

    # Turnover moderation score (0-30)
    if turnover > 0:
        if 3.0 <= turnover <= 20.0:
            tov_score = 30.0
        elif 1.0 <= turnover < 3.0:
            tov_score = 20.0
        elif 20.0 < turnover <= 30.0:
            tov_score = 15.0
        else:
            tov_score = 5.0
    else:
        tov_score = 10.0

    return amt_score + vol_score + tov_score


def _llm_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _llm_request(payload: Dict) -> Optional[Dict]:
    runtime = resolve_llm_runtime(allow_openclaw_key_fallback=True)
    if not runtime:
        return None
    api_key = runtime["api_key"]
    base_url = runtime["base_url"]
    model = runtime["model"]
    req_payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是A股长线组合盘后选股审查员。"
                    "请输出严格JSON，字段: verdict(approve/watch/reject), risk(0-100), summary, thesis, key_risks(list[str])."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(req_payload, ensure_ascii=False).encode("utf-8"),
        headers=_llm_headers(api_key),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw)
    except Exception:
        return None

    try:
        content = (
            ((parsed.get("choices") or [])[0] or {})
            .get("message", {})
            .get("content", "")
        )
        return json.loads(content) if content else None
    except Exception:
        return None


def run_post_market_scan(
    *,
    trade_date: str,
    universe: List[StockCandidate],
    portfolio: PortfolioState,
    quotes: Dict[str, Dict],
    history_by_code: Optional[Dict[str, Dict[str, List[float]]]] = None,
    heat_by_code: Optional[Dict[str, Dict]] = None,
    settings: Optional[LongTermSettings] = None,
    top_k: int = 15,
    use_llm: bool = True,
) -> Dict:
    holdings = {item.code for item in portfolio.positions}
    rows: List[Dict] = []
    history_by_code = history_by_code or {}
    heat_by_code = heat_by_code or {}
    fallback_count = 0

    # Pre-compute rotation fields
    all_codes = [str(item.code).upper() for item in universe]
    heat_accel_window = int(getattr(settings, "heat_accel_window", 5) or 5)
    heat_accel_map = compute_heat_acceleration(all_codes, trade_date, window=heat_accel_window)
    max_concepts = int(getattr(settings, "max_concepts", 20) or 20)
    sector_mom_map = compute_sector_momentum(all_codes, trade_date, top_n=max_concepts)

    for candidate in universe:
        code = str(candidate.code).upper()
        quote = quotes.get(code, {}) or {}
        history = history_by_code.get(code, {}) or {}
        heat_info = heat_by_code.get(code, {}) or {}
        row = _build_reference_style_row(candidate, quote, history, heat_info=heat_info, settings=settings)
        if row is None:
            fallback_count += 1
            row = _fallback_row(candidate, quote, heat_info=heat_info, settings=settings)

        # Inject rotation fields
        closes = [x for x in history.get("close", []) if _float(x) > 0]
        row["heat_accel"] = heat_accel_map.get(code, 0.0)
        row["sector_momentum"] = sector_mom_map.get(code, 0.0)
        row["price_trend"] = round(_compute_price_trend_score(closes), 3)
        row["liquidity_score"] = round(_compute_liquidity_score(quote, history), 3)
        row["in_portfolio"] = code in holdings
        rows.append(row)

    ranked = sorted(rows, key=lambda item: item["score"], reverse=True)
    top = ranked[: max(1, int(top_k or 15))]
    llm_enabled = bool(use_llm)
    if llm_enabled:
        for item in top:
            payload = {
                "trade_date": trade_date,
                "candidate": item,
                "portfolio_state": {
                    "nav": portfolio.nav,
                    "cash": portfolio.cash,
                    "available_cash": portfolio.available_cash,
                    "frozen_cash": portfolio.frozen_cash,
                    "holdings_count": len(portfolio.positions),
                },
            }
            llm_result = _llm_request(payload) or {}
            if llm_result:
                item["llm"] = llm_result

    if llm_enabled:
        approved = []
        for item in top:
            llm = item.get("llm", {}) or {}
            verdict = str(llm.get("verdict", "")).lower().strip()
            # LLM unavailable/failed: fallback to heuristic candidate.
            if not verdict:
                approved.append(item)
                continue
            if verdict in {"approve", "watch"}:
                approved.append(item)
    else:
        approved = list(top)

    return {
        "trade_date": trade_date,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": "post_market_alpha_scanner_migrated",
        "universe_count": len(universe),
        "top_k": int(top_k),
        "llm_enabled": llm_enabled,
        "fallback_count": fallback_count,
        "heat_coverage_count": len([x for x in heat_by_code.values() if _float((x or {}).get("heat_score"), 0.0) > 0]),
        "portfolio_snapshot": {
            "nav": round(portfolio.nav, 2),
            "cash": round(portfolio.cash, 2),
            "available_cash": round(portfolio.available_cash, 2),
            "frozen_cash": round(portfolio.frozen_cash, 2),
            "holdings_count": len(portfolio.positions),
        },
        "ranked": ranked,
        "top_candidates": top,
        "suggested_watchlist": approved[:10],
    }


def build_candidates_from_portfolio(
    portfolio: PortfolioState,
    *,
    updated_at: str,
    industry_map: Optional[Dict[str, str]] = None,
    sector_map: Optional[Dict[str, List[str]]] = None,
    existing_candidates: Optional[List[StockCandidate]] = None,
) -> List[StockCandidate]:
    candidates: List[StockCandidate] = []
    nav = max(float(portfolio.nav), 1.0)
    industry_map = {str(k).upper(): str(v) for k, v in (industry_map or {}).items() if str(k).strip()}
    sector_map = {str(k).upper(): list(v or []) for k, v in (sector_map or {}).items() if str(k).strip()}
    existing_map = {str(item.code).upper(): item for item in (existing_candidates or []) if str(item.code).strip()}
    for pos in portfolio.positions:
        code = str(pos.code).upper()
        previous = existing_map.get(code)
        industry = str(industry_map.get(code, "UNKNOWN") or "UNKNOWN").strip() or "UNKNOWN"
        if industry == "UNKNOWN" and previous and str(previous.industry or "").strip():
            industry = str(previous.industry or "").strip()
        tags: List[str] = []
        for item in list(sector_map.get(code) or []) + list((previous.tags if previous else []) or []):
            text = str(item).strip()
            if text and text not in tags:
                tags.append(text)
        if "synced" not in tags:
            tags.append("synced")
        if previous and not _is_default_sync_candidate(previous):
            score_payload = {
                "value_score": round(_clamp(previous.value_score), 2),
                "quality_score": round(_clamp(previous.quality_score), 2),
                "growth_score": round(_clamp(previous.growth_score), 2),
                "risk_score": round(_clamp(previous.risk_score), 2),
            }
            thesis = str(previous.thesis or "").strip() or "from_portfolio_sync"
        else:
            score_payload = _derive_position_scores(pos, nav=nav, tags=tags)
            thesis = "from_portfolio_sync"
        candidates.append(
            StockCandidate(
                code=code,
                name=pos.name or (previous.name if previous else code),
                status="active",
                value_score=score_payload["value_score"],
                quality_score=score_payload["quality_score"],
                growth_score=score_payload["growth_score"],
                risk_score=score_payload["risk_score"],
                industry=industry,
                thesis=thesis,
                tags=tags[:12],
                updated_at=updated_at,
            )
        )
    return candidates

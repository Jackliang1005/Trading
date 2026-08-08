#!/usr/bin/env python3
"""JSON API used by OpenClaw A-share Skill wrappers."""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

SKILL_CACHE_DIR = Path(__file__).resolve().parent / "data" / "skill_cache"


def _load_skill_cache(name: str) -> dict[str, Any]:
    path = SKILL_CACHE_DIR / f"{name}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_skill_cache(name: str, payload: dict[str, Any]) -> None:
    try:
        SKILL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = SKILL_CACHE_DIR / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass


def _latest_a_share_market_date(now: datetime | None = None) -> tuple[str, str]:
    """Estimate the market date when an upstream snapshot omits its own timestamp."""
    current = now or datetime.now()
    candidate = current.date()
    if current.hour * 60 + current.minute < 9 * 60 + 30:
        candidate -= timedelta(days=1)
    try:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XSHG")
        for _ in range(30):
            value = candidate.isoformat()
            if bool(calendar.is_session(value)):
                return value, "exchange_calendars:XSHG"
            candidate -= timedelta(days=1)
    except Exception:
        pass
    for _ in range(30):
        if candidate.weekday() < 5:
            return candidate.isoformat(), "weekday_fallback"
        candidate -= timedelta(days=1)
    return current.date().isoformat(), "date_fallback"


def _symbol(code: str) -> str:
    raw = str(code or "").strip().upper()
    digits = "".join(char for char in raw if char.isdigit())[-6:]
    return ("sh" if raw.endswith((".SH", ".XSHG")) or digits.startswith(("5", "6", "9")) else "sz") + digits


def _resolve_symbol(query: str) -> dict[str, Any]:
    """Resolve an A-share code or Chinese name with a daily cached code list."""
    raw = str(query or "").strip()
    code_match = re.fullmatch(r"([036159]\d{5})(?:\.(SH|SZ))?", raw.upper())
    if code_match:
        digits = code_match.group(1)
        return {"ok": True, "query": raw, "matches": [{"code": digits, "name": "", "exchange": "SH" if digits.startswith(("5", "6", "9")) else "SZ"}], "source": "input_code"}

    cache_path = Path(__file__).resolve().parent / "data" / "a_share_symbol_cache.json"
    items: list[list[str]] = []
    source = "akshare.stock_info_a_code_name"
    try:
        if cache_path.exists() and time.time() - cache_path.stat().st_mtime < 86400:
            items = json.loads(cache_path.read_text(encoding="utf-8"))
            source = "a_share_symbol_cache"
    except Exception:
        items = []
    if not items:
        script = (
            "import akshare as ak,json; df=ak.stock_info_a_code_name(); "
            "items=[[str(row[0]),str(row[1])] for row in df.iloc[:,:2].itertuples(index=False,name=None)]; "
            "print(json.dumps({'items':items},ensure_ascii=False))"
        )
        try:
            items = _akshare_subprocess(script, timeout=25).get("items") or []
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            return {"ok": False, "query": raw, "error": "symbol_source_unavailable", "message": str(exc)}

    exact = [item for item in items if len(item) >= 2 and str(item[1]) == raw]
    partial = [item for item in items if len(item) >= 2 and raw in str(item[1])]
    embedded = [item for item in items if len(item) >= 2 and str(item[1]) and str(item[1]) in raw]
    selected = exact or partial or embedded
    if not selected:
        # Natural-language queries often contain a short company name inside a sentence.
        candidate_tokens = {
            raw[start:end]
            for start in range(len(raw))
            for end in range(start + 2, min(len(raw), start + 8) + 1)
        }
        matches_by_length = []
        for token in candidate_tokens:
            rows = [item for item in items if len(item) >= 2 and token in str(item[1])]
            if rows:
                matches_by_length.append((len(token), rows))
        if matches_by_length:
            best_length = max(length for length, _ in matches_by_length)
            selected = next(rows for length, rows in matches_by_length if length == best_length)
    selected = selected[:10]
    matches = [
        {"code": str(item[0]).zfill(6), "name": str(item[1]), "exchange": "SH" if str(item[0]).startswith(("5", "6", "9")) else "SZ"}
        for item in selected
    ]
    return {"ok": bool(matches), "query": raw, "matches": matches, "source": source, "error": "symbol_not_found" if not matches else ""}


def _quote(codes: list[str]) -> dict[str, Any]:
    symbols = [_symbol(code) for code in codes if str(code).strip()]
    req = urllib.request.Request("https://qt.gtimg.cn/q=" + ",".join(symbols), headers={"User-Agent": "Mozilla/5.0"})
    text = urllib.request.urlopen(req, timeout=8).read().decode("gb18030", errors="replace")
    rows = []
    for statement in text.split(";"):
        if "=" not in statement:
            continue
        key, raw = statement.split("=", 1)
        fields = raw.strip().strip('"').split("~")
        if len(fields) < 38:
            continue
        bids = [{"level": level, "volume": float(fields[9 + (level - 1) * 2] or 0), "price": float(fields[10 + (level - 1) * 2] or 0)} for level in range(1, 6)]
        asks = [{"level": level, "volume": float(fields[19 + (level - 1) * 2] or 0), "price": float(fields[20 + (level - 1) * 2] or 0)} for level in range(1, 6)]
        rows.append({
            "symbol": key.strip().replace("v_", ""), "code": fields[2], "name": fields[1],
            "price": float(fields[3] or 0), "pre_close": float(fields[4] or 0),
            "change_pct": float(fields[32] or 0), "high": float(fields[33] or 0), "low": float(fields[34] or 0),
            "turnover_yi": round(float(fields[37] or 0) / 10000, 2), "turnover_rate_pct": float(fields[38] or 0),
            "pe_ttm": float(fields[39] or 0), "pb": float(fields[46] or 0),
            "market_cap_yi": float(fields[45] or 0),
            "order_book": {"bids": bids, "asks": asks},
            "as_of": fields[30], "source": "tencent_quote",
        })
    return {"ok": bool(rows), "quotes": rows}


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(value * alpha + result[-1] * (1 - alpha))
    return result


def _kline(code: str, period: str = "day", limit: int = 240) -> dict[str, Any]:
    normalized_period = str(period or "day").lower()
    if normalized_period not in {"day", "week", "month", "m1", "m5", "m15", "m30", "m60"}:
        return {"ok": False, "error": "unsupported_kline_period", "supported": ["day", "week", "month", "m1", "m5", "m15", "m30", "m60"]}
    symbol = _symbol(code)
    bars = max(20, min(int(limit or 240), 800))
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},{normalized_period},,,{bars},"
    payload = json.loads(urllib.request.urlopen(url, timeout=10).read().decode("utf-8", errors="replace"))
    rows = ((payload.get("data") or {}).get(symbol) or {}).get(normalized_period) or []
    return {"ok": bool(rows), "code": code, "period": normalized_period, "bars": rows, "as_of": rows[-1][0] if rows else "", "source": "tencent_kline", "error": "insufficient_kline" if not rows else ""}


def _technical(code: str, period: str = "day") -> dict[str, Any]:
    symbol = _symbol(code)
    normalized_period = str(period or "day").lower()
    if normalized_period not in {"day", "week", "month", "m1", "m5", "m15", "m30", "m60"}:
        return {"ok": False, "error": "unsupported_kline_period"}
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},{normalized_period},,,80,"
    data = json.loads(urllib.request.urlopen(url, timeout=8).read().decode("utf-8", errors="replace"))
    rows = ((data.get("data") or {}).get(symbol) or {}).get(normalized_period) or []
    valid_rows = [row for row in rows if isinstance(row, list) and len(row) >= 5]
    closes = [float(row[2]) for row in valid_rows]
    if len(closes) < 20:
        return {"ok": False, "error": "insufficient_kline"}
    def ma(period: int) -> float:
        return round(sum(closes[-period:]) / period, 3) if len(closes) >= period else 0.0
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    gains = [max(value, 0) for value in changes[-14:]]
    losses = [abs(min(value, 0)) for value in changes[-14:]]
    rs = (sum(gains) / 14) / (sum(losses) / 14) if sum(losses) else math.inf
    rsi = 100 - 100 / (1 + rs)
    fast, slow = _ema(closes, 12), _ema(closes, 26)
    macd = fast[-1] - slow[-1]
    signal = _ema([a - b for a, b in zip(fast, slow)], 9)[-1]
    previous_macd = fast[-2] - slow[-2]
    previous_signal = _ema([a - b for a, b in zip(fast, slow)], 9)[-2]
    ma_cross = "golden_cross" if ma(5) > ma(20) and sum(closes[-6:-1]) / 5 <= sum(closes[-21:-1]) / 20 else "death_cross" if ma(5) < ma(20) and sum(closes[-6:-1]) / 5 >= sum(closes[-21:-1]) / 20 else "none"
    k, d = 50.0, 50.0
    for index in range(len(valid_rows)):
        window = valid_rows[max(0, index - 8):index + 1]
        highest = max(float(row[3]) for row in window)
        lowest = min(float(row[4]) for row in window)
        rsv = 50.0 if highest == lowest else (float(valid_rows[index][2]) - lowest) / (highest - lowest) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
    boll_mid = ma(20)
    boll_std = math.sqrt(sum((value - boll_mid) ** 2 for value in closes[-20:]) / 20)
    stance = "bullish" if closes[-1] > ma(20) and macd > signal else "bearish" if closes[-1] < ma(20) and macd < signal else "neutral"
    return {"ok": True, "code": code, "period": normalized_period, "as_of": valid_rows[-1][0], "close": closes[-1], "ma5": ma(5), "ma10": ma(10), "ma20": ma(20), "ma60": ma(60), "ma_cross": ma_cross, "rsi14": round(rsi, 2), "macd": round(macd, 4), "macd_signal": round(signal, 4), "macd_cross": "golden_cross" if macd > signal and previous_macd <= previous_signal else "death_cross" if macd < signal and previous_macd >= previous_signal else "none", "kdj_k": round(k, 2), "kdj_d": round(d, 2), "kdj_j": round(3 * k - 2 * d, 2), "boll_mid": round(boll_mid, 3), "boll_upper": round(boll_mid + 2 * boll_std, 3), "boll_lower": round(boll_mid - 2 * boll_std, 3), "stance": stance, "support": min(closes[-20:]), "resistance": max(closes[-20:]), "source": "tencent_kline"}


def _backtest(code: str, days: int = 250) -> dict[str, Any]:
    """Long-only MA5/MA20 cross backtest with next-day execution assumption."""
    symbol = _symbol(code)
    lookback = max(80, min(int(days), 800))
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{lookback},"
    payload = json.loads(urllib.request.urlopen(url, timeout=10).read().decode("utf-8", errors="replace"))
    rows = ((payload.get("data") or {}).get(symbol) or {}).get("day") or []
    closes = [float(row[2]) for row in rows if isinstance(row, list) and len(row) >= 3]
    if len(closes) < 60:
        return {"ok": False, "error": "insufficient_kline"}
    signals = []
    for index in range(len(closes)):
        ma5 = sum(closes[max(0, index - 4):index + 1]) / min(index + 1, 5)
        ma20 = sum(closes[max(0, index - 19):index + 1]) / min(index + 1, 20)
        signals.append(1.0 if index >= 19 and ma5 > ma20 else 0.0)
    returns = [(closes[index] / closes[index - 1] - 1) * signals[index - 1] for index in range(1, len(closes))]
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    annualized = equity ** (252 / len(returns)) - 1 if equity > 0 else -1.0
    sharpe = mean / math.sqrt(variance) * math.sqrt(252) if variance > 0 else 0.0
    trades = sum(1 for index in range(1, len(signals)) if signals[index] != signals[index - 1])
    active_returns = [value for value in returns if value != 0]
    win_rate = sum(1 for value in active_returns if value > 0) / len(active_returns) if active_returns else 0.0
    return {"ok": True, "code": code, "strategy": "MA5_above_MA20_long_only", "start": rows[0][0], "end": rows[-1][0], "bars": len(closes), "total_return_pct": round((equity - 1) * 100, 2), "annualized_return_pct": round(annualized * 100, 2), "max_drawdown_pct": round(max_drawdown * 100, 2), "sharpe": round(sharpe, 2), "win_rate_pct": round(win_rate * 100, 2), "signal_changes": trades, "source": "tencent_kline", "assumption": "signal at close, position applied next day; excludes fees/slippage"}


def _akshare_subprocess(source: str, timeout: int = 18) -> dict[str, Any]:
    """Keep potentially slow third-party calls out of the Skill process."""
    result = subprocess.run(["python3", "-c", source], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "akshare_failed")[:300])
    value = json.loads(result.stdout or "{}")
    return value if isinstance(value, dict) else {}


def _financial(code: str) -> dict[str, Any]:
    digits = "".join(char for char in str(code) if char.isdigit())[-6:]
    source = (
        "import akshare as ak,json; "
        f"df=ak.stock_financial_abstract(symbol='{digits}'); "
        "dates=[c for c in df.columns if str(c).startswith('20')]; latest=dates[0] if dates else ''; "
        "rows={str(r.get('指标')):r.get(latest) for r in df.to_dict('records') if r.get('指标')}; "
        "print(json.dumps({'period':latest,'rows':rows},ensure_ascii=False,default=str))"
    )
    try:
        row = _akshare_subprocess(source, timeout=30)
    except Exception as exc:
        return {"ok": False, "code": code, "error": "financial_source_unavailable", "message": str(exc)}
    if not row or not row.get("rows"):
        return {"ok": False, "code": code, "error": "financial_data_empty", "message": "AKShare returned no financial indicator row"}
    values = row.get("rows") or {}
    aliases = {
        "revenue": ("营业总收入",),
        "net_profit": ("归母净利润",),
        "operating_cashflow": ("经营现金流量净额",),
        "roe": ("净资产收益率(ROE)", "净资产收益率_平均"),
        "gross_margin": ("毛利率",),
        "debt_ratio": ("资产负债率",),
        "revenue_growth": ("营业总收入增长率",),
        "net_profit_growth": ("归属母公司净利润增长率",),
    }
    metrics = {key: next((values.get(name) for name in names if name in values), None) for key, names in aliases.items()}
    return {"ok": True, "code": code, "period": row.get("period", ""), "metrics": metrics, "raw_fields": len(values), "source": "akshare.stock_financial_abstract"}


def _number(value: Any) -> float | None:
    try:
        text = str(value).strip().replace(",", "").replace("%", "")
        return float(text) if text and text.lower() not in {"none", "nan", "null"} else None
    except Exception:
        return None


def _cninfo_industry_profile(code: str) -> dict[str, str] | None:
    """Resolve one stock's current public CNInfo industry label as a fallback."""
    source = (
        "import akshare as ak,json; "
        f"df=ak.stock_industry_change_cninfo(symbol='{code[:6]}',start_date='19900101',end_date='20991231'); "
        "df=df[df.iloc[:,8].astype(str)=='008002'].sort_values(df.columns[-1]); row=df.iloc[-1].tolist() if len(df) else []; "
        "label=next((value for value in (row[1],row[2],row[4]) if str(value).strip().lower() not in {'','nan','none','nat'}),'') if row else ''; "
        "print(json.dumps({'industry':str(label),"
        "'industry_code':str(row[6]) if row else '', 'classification':str(row[7]) if row else ''},ensure_ascii=False))"
    )
    try:
        data = _akshare_subprocess(source, timeout=12)
    except Exception:
        return None
    industry = str(data.get("industry") or "").strip()
    return {"industry": industry, "industry_code": str(data.get("industry_code") or ""), "classification": str(data.get("classification") or "")} if industry else None


def _industry_peer_summary(code: str) -> dict[str, Any]:
    """Return cache-backed industry context, never a fabricated peer set."""
    cache_path = Path(__file__).resolve().parent / "data" / "mootdx_finance_cache.json"
    if not cache_path.exists():
        return {"available": False, "reason": "finance_cache_missing"}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        rows = cache.get("rows") or []
        item = next((row for row in rows if str(row.get("code") or "")[:6] == code[:6]), None)
    except Exception:
        return {"available": False, "reason": "finance_cache_unreadable"}
    if not item or not str(item.get("industry") or "").strip():
        profile = _cninfo_industry_profile(code)
        return {
            "available": False,
            "reason": "industry_mapping_unavailable",
            "coverage": {"mapped": int(cache.get("industry_coverage") or 0), "total": int(cache.get("universe_size") or len(rows))},
            "industry_profile": profile,
        }
    industry = str(item["industry"]).strip()
    peers = [row for row in rows if str(row.get("industry") or "").strip() == industry]
    if len(peers) < 5:
        return {"available": False, "reason": "insufficient_industry_peers", "industry": industry, "peer_count": len(peers)}

    def percentile(metric: str, lower_is_better: bool = False) -> float | None:
        value = _number(item.get(metric))
        values = [_number(row.get(metric)) for row in peers]
        values = [number for number in values if number is not None and (number > 0 if lower_is_better else True)]
        if value is None or (lower_is_better and value <= 0) or not values:
            return None
        below = sum(number <= value for number in values)
        result = below / len(values) * 100
        return round(100 - result if lower_is_better else result, 1)

    return {
        "available": True,
        "industry": industry,
        "peer_count": len(peers),
        "as_of": cache.get("quote_refreshed_at") or cache.get("growth_refreshed_at") or "",
        "percentiles": {
            "pe_ttm_lower_better": percentile("pe_ttm", lower_is_better=True),
            "pb_lower_better": percentile("pb", lower_is_better=True),
            "roe_higher_better": percentile("roe"),
        },
        "source": cache.get("industry_source") or "cached_industry_mapping",
    }


def _fundamental(code: str) -> dict[str, Any]:
    financial = _financial(code)
    quote_response = _quote([code])
    quote = (quote_response.get("quotes") or [{}])[0]
    if not financial.get("ok"):
        return {"ok": False, "code": code, "error": financial.get("error", "financial_source_unavailable"), "message": financial.get("message", "")}
    metrics = financial.get("metrics") or {}
    roe = _number(metrics.get("roe"))
    debt_ratio = _number(metrics.get("debt_ratio"))
    cashflow = _number(metrics.get("operating_cashflow"))
    net_profit = _number(metrics.get("net_profit"))
    revenue = _number(metrics.get("revenue"))
    net_profit_growth = _number(metrics.get("net_profit_growth"))
    market_cap_yi = _number(quote.get("market_cap_yi"))
    coverage = round(cashflow / net_profit, 3) if cashflow is not None and net_profit not in (None, 0) else None
    ps = round(market_cap_yi * 100000000 / revenue, 3) if market_cap_yi is not None and revenue not in (None, 0) else None
    pe_ttm = _number(quote.get("pe_ttm"))
    peg = round(pe_ttm / net_profit_growth, 3) if pe_ttm is not None and net_profit_growth not in (None, 0) and net_profit_growth > 0 else None
    observations = []
    if roe is not None:
        observations.append("roe_high" if roe >= 15 else "roe_low" if roe < 8 else "roe_mid")
    if debt_ratio is not None:
        observations.append("debt_high" if debt_ratio >= 60 else "debt_controlled")
    if coverage is not None:
        observations.append("cashflow_covers_profit" if coverage >= 1 else "cashflow_below_profit")
    return {
        "ok": True,
        "code": code,
        "financial_period": financial.get("period"),
        "valuation_as_of": quote.get("as_of", ""),
        "valuation": {"pe_ttm": pe_ttm, "pb": quote.get("pb"), "ps": ps, "peg": peg, "market_cap_yi": market_cap_yi, "price": quote.get("price"), "turnover_rate_pct": quote.get("turnover_rate_pct")},
        "financial_health": {"roe": roe, "debt_ratio": debt_ratio, "operating_cashflow": cashflow, "net_profit": net_profit, "operating_cashflow_to_net_profit": coverage},
        "growth": {"revenue_growth": _number(metrics.get("revenue_growth")), "net_profit_growth": net_profit_growth, "gross_margin": _number(metrics.get("gross_margin"))},
        "observations": observations,
        "industry_comparison": _industry_peer_summary(code),
        "sources": [financial.get("source"), quote.get("source", "quote_unavailable")],
        "limitation": "Industry percentiles are shown only when the classified full-universe cache has a sufficient peer set.",
    }


def _macro() -> dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    source_functions = {
        "cpi": "macro_china_cpi_yearly",
        "ppi": "macro_china_ppi_yearly",
        "gdp": "macro_china_gdp_yearly",
        "money_supply": "macro_china_money_supply",
        "lpr": "macro_china_lpr",
        "retail_sales": "macro_china_consumer_goods_retail",
    }

    def load_one(function_name: str) -> dict[str, Any]:
        source = (
            "import akshare as ak,json; "
            f"frame=ak.{function_name}(); "
            "row=frame.head(1).to_dict('records')[0] if len(frame) else {}; "
            "print(json.dumps(row,ensure_ascii=False,default=str))"
        )
        return _akshare_subprocess(source, timeout=10)

    data: dict[str, Any] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(source_functions)) as executor:
        futures = {executor.submit(load_one, function_name): name for name, function_name in source_functions.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                data[name] = future.result()
            except Exception as exc:
                errors[name] = str(exc)[:160]

    fresh, stale = {}, {}
    for name, row in data.items():
        as_of = _macro_as_of(row if isinstance(row, dict) else {})
        if as_of and (date.today() - as_of).days > 120:
            stale[name] = as_of.isoformat()
        else:
            fresh[name] = row
    return {
        "ok": bool(fresh),
        "data": fresh,
        "available_sources": sorted(fresh),
        "stale_sources": stale,
        "source_errors": errors,
        "source": "akshare.macro_china",
        "limitation": "Each series has its own publication lag; inspect the returned period fields before making a time-sensitive conclusion.",
    }


def _macro_as_of(row: dict[str, Any]) -> date | None:
    for value in row.values():
        match = re.search(r"(19|20)\d{2}\D+(\d{1,2})(?:\D+(\d{1,2}))?", str(value or ""))
        if not match:
            continue
        try:
            return date(int(match.group(0)[:4]), int(match.group(2)), int(match.group(3) or 1))
        except ValueError:
            continue
    return None


def _announcements(code: str, date_text: str = "") -> dict[str, Any]:
    digits = "".join(char for char in str(code) if char.isdigit())[-6:]
    target = date.fromisoformat(date_text) if date_text else date.today()
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    compact = target.strftime("%Y%m%d")
    source = (
        "import akshare as ak,json; "
        f"df=ak.stock_notice_report(symbol='全部',date='{compact}'); "
        f"df=df[df['代码'].astype(str).str.zfill(6)=='{digits}']; "
        "print(json.dumps({'items':df.head(12).to_dict('records')},ensure_ascii=False,default=str))"
    )
    cache_name = f"announcements_{digits}_{compact}"
    cached = _load_skill_cache(cache_name)
    cache_recent = False
    cache_covers_target = False
    try:
        fetched_at = datetime.fromisoformat(str(cached.get("fetched_at") or ""))
        cache_recent = (datetime.now() - fetched_at).total_seconds() < 300
        # A cache written before the requested announcement date cannot prove
        # that the date is complete.  Retry the source and use it only as an
        # explicit fallback if the refresh fails.
        cache_covers_target = fetched_at.date() >= target
    except Exception:
        pass
    if cached.get("ok") and (cache_recent or (target < date.today() and cache_covers_target)):
        return {**cached, "cache_hit": True}
    try:
        data = _akshare_subprocess(source, timeout=12)
        result = {"ok": True, "code": digits, "date": target.isoformat(), "items": data.get("items") or [], "source": "akshare.stock_notice_report", "fetched_at": datetime.now().isoformat(timespec="seconds")}
        _save_skill_cache(cache_name, result)
        return result
    except Exception as exc:
        if cached.get("ok"):
            return {**cached, "cache_fallback": True, "source_error": str(exc)[:160]}
        return {"ok": False, "code": digits, "error": "announcement_source_unavailable", "message": str(exc)}


def _news_interpretation(code: str, date_text: str = "") -> dict[str, Any]:
    """Produce source-grounded announcement facts and transparent title-level signals."""
    data = _announcements(code, date_text)
    if not data.get("ok"):
        return data
    positive_terms = ("\u4e1a\u7ee9\u9884\u589e", "\u56de\u8d2d", "\u4e2d\u6807", "\u589e\u6301", "\u5206\u7ea2", "\u7b7e\u7f72", "\u9884\u559c")
    negative_terms = ("\u51cf\u6301", "\u4e1a\u7ee9\u9884\u51cf", "\u4e8f\u635f", "\u8bc9\u8bbc", "\u7acb\u6848", "\u5904\u7f5a", "\u7ec8\u6b62", "\u4e0b\u8c03", "\u98ce\u9669")
    facts = []
    positive_count = 0
    negative_count = 0
    for item in data.get("items") or []:
        title = str(item.get("\u516c\u544a\u6807\u9898") or item.get("title") or "")
        hits_positive = [term for term in positive_terms if term in title]
        hits_negative = [term for term in negative_terms if term in title]
        positive_count += bool(hits_positive)
        negative_count += bool(hits_negative)
        facts.append({
            "title": title,
            "type": item.get("\u516c\u544a\u7c7b\u578b") or item.get("type") or "",
            "url": item.get("\u7f51\u5740") or item.get("url") or "",
            "title_signals": {"positive": hits_positive, "negative": hits_negative},
        })
    sentiment = "mixed" if positive_count and negative_count else "positive" if positive_count else "negative" if negative_count else "neutral"
    return {
        "ok": True,
        "code": data.get("code"),
        "date": data.get("date"),
        "facts": facts,
        "title_level_sentiment": sentiment,
        "counts": {"positive_titles": positive_count, "negative_titles": negative_count, "total": len(facts)},
        "source": data.get("source"),
        "fetched_at": data.get("fetched_at"),
        "cache_fallback": bool(data.get("cache_fallback")),
        "interpretation_boundary": "Signals are title-level triage only. The Agent must distinguish sourced facts from its own analysis and inspect the linked original notice before a material conclusion.",
    }


def _capital_flow(date_text: str = "", timeout: int = 30) -> dict[str, Any]:
    target = date.fromisoformat(date_text) if date_text else date.today()
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    compact = target.strftime("%Y%m%d")
    source = (
        "import akshare as ak,json; "
        f"lhb=ak.stock_lhb_detail_em(start_date='{compact}',end_date='{compact}'); north=ak.stock_hsgt_fund_flow_summary_em(); "
        "lhb_rows=[{'code':str(row[1]),'name':str(row[2]),'interpretation':str(row[4]),'change_pct':row[6],'net_buy':row[7],'reason':str(row[16])} for row in lhb.itertuples(index=False,name=None)][:20]; "
        "north_rows=[{'kind':str(row[1]),'board':str(row[2]),'direction':str(row[3]),'net_buy':row[5],'net_inflow':row[6],'up_count':row[8],'down_count':row[10],'index_change_pct':row[12]} for row in north.itertuples(index=False,name=None)]; "
        "print(json.dumps({'lhb':lhb_rows,'northbound':north_rows},ensure_ascii=False,default=str))"
    )
    try:
        data = _akshare_subprocess(source, timeout=max(1, int(timeout)))
        return {"ok": True, "date": target.isoformat(), "lhb": data.get("lhb") or [], "northbound": data.get("northbound") or [], "source": "akshare.stock_lhb_detail_em+stock_hsgt_fund_flow_summary_em"}
    except Exception as exc:
        return {"ok": False, "date": target.isoformat(), "error": "capital_flow_source_unavailable", "message": str(exc)}


def _sector_flow(limit: int = 10) -> dict[str, Any]:
    capped = max(1, min(int(limit), 50))
    source = (
        "import akshare as ak,json; "
        "df=ak.stock_board_industry_summary_ths(); "
        f"rows=[{{'name':str(row[1]),'change_pct':row[2],'main_net_inflow_yi':row[5],'up_count':row[6],'down_count':row[7],'leader':str(row[9]),'leader_change_pct':row[11]}} for row in df.itertuples(index=False,name=None)][:{capped}]; "
        "print(json.dumps({'sectors':rows},ensure_ascii=False,default=str))"
    )
    try:
        data = _akshare_subprocess(source, timeout=12)
        sectors = data.get("sectors") or []
        market_date, market_date_source = _latest_a_share_market_date()
        result = {
            "ok": bool(sectors),
            "sectors": sectors,
            "source": "akshare.stock_board_industry_summary_ths",
            "market_date": market_date,
            "market_date_source": market_date_source,
            "market_date_estimated": True,
            "retrieved_at": datetime.now().isoformat(timespec="seconds"),
        }
        if result["ok"]:
            _save_skill_cache("sector_flow", result)
        return result
    except Exception as exc:
        cached = _load_skill_cache("sector_flow")
        if cached.get("ok"):
            if not cached.get("market_date"):
                market_date, market_date_source = _latest_a_share_market_date()
                cached = {
                    **cached,
                    "market_date": market_date,
                    "market_date_source": market_date_source,
                    "market_date_estimated": True,
                    "retrieved_at": cached.get("retrieved_at") or cached.get("as_of"),
                }
            return {**cached, "cache_fallback": True, "source_error": str(exc)[:160]}
        return {"ok": False, "error": "sector_flow_source_unavailable", "message": str(exc)}


def _screener(request: dict[str, Any]) -> dict[str, Any]:
    codes = [str(code) for code in (request.get("codes") or []) if str(code).strip()][:10]
    if not codes:
        if str(request.get("universe") or "") != "mootdx":
            return {"ok": False, "error": "universe_required", "message": "Provide up to 10 codes, or request the available mootdx full-universe basic-financial cache."}
        cache_path = Path(__file__).resolve().parent / "data" / "mootdx_finance_cache.json"
        if not cache_path.exists():
            return {"ok": False, "error": "mootdx_cache_missing", "message": "Build the daily mootdx finance cache before full-universe screening."}
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        conditions = request.get("conditions") or {}
        supported = {"roe", "debt_ratio", "revenue", "net_profit", "operating_cashflow"}
        valuation_metrics = {"pe_ttm", "pb", "ps"}
        growth_metrics = {"revenue_growth", "net_profit_growth", "gross_margin", "peg"}
        momentum_metrics = {"momentum_20d", "momentum_60d"}
        requested = set((conditions.get("min") or {})) | set((conditions.get("max") or {}))
        quote_coverage = int(cache.get("quote_coverage") or 0)
        universe_size = int(cache.get("universe_size") or len(cache.get("rows") or []))
        if universe_size and quote_coverage / universe_size >= 0.95:
            supported |= valuation_metrics
        metric_coverage = cache.get("metric_coverage") or {}
        growth_coverage = min(
            int(metric_coverage.get("revenue_growth") or 0),
            int(metric_coverage.get("net_profit_growth") or 0),
        )
        if universe_size and growth_coverage / universe_size >= 0.95:
            supported |= growth_metrics
        momentum_coverage = int(cache.get("momentum_coverage") or 0)
        if universe_size and momentum_coverage / universe_size >= 0.90:
            supported |= momentum_metrics
        unsupported = sorted(requested - supported)
        if unsupported:
            return {"ok": False, "error": "full_universe_metric_unavailable", "unsupported_metrics": unsupported, "quote_coverage": quote_coverage, "growth_coverage": growth_coverage, "universe_size": universe_size, "message": "Valuation and latest-reported growth filters require at least 95% data coverage."}
        exclude_st = bool(request.get("exclude_st", True))
        rank_by = str(request.get("rank_by") or "").strip().lower()
        if rank_by not in {"", "value", "growth", "momentum"}:
            return {"ok": False, "error": "unsupported_rank_by", "message": "Supported full-universe ranks are value, growth, and momentum."}
        if rank_by == "momentum" and (not universe_size or momentum_coverage / universe_size < 0.90):
            return {"ok": False, "error": "momentum_coverage_insufficient", "momentum_coverage": momentum_coverage, "universe_size": universe_size}
        matches = []
        for item in cache.get("rows") or []:
            if exclude_st and "ST" in str(item.get("name") or "").upper():
                continue
            passed = True
            for key, minimum in (conditions.get("min") or {}).items():
                try:
                    value = float(item[key])
                except (KeyError, TypeError, ValueError):
                    passed = False
                    break
                if key in {"pe_ttm", "pb", "ps", "peg"} and value <= 0:
                    passed = False
                    break
                passed = passed and value >= float(minimum)
            for key, maximum in (conditions.get("max") or {}).items():
                try:
                    value = float(item[key])
                except (KeyError, TypeError, ValueError):
                    passed = False
                    break
                if key in {"pe_ttm", "pb", "ps", "peg"} and value <= 0:
                    passed = False
                    break
                passed = passed and value <= float(maximum)
            if passed:
                matches.append(item)
        if rank_by == "value":
            ranked = []
            for item in matches:
                try:
                    pe_ttm, pb, roe = float(item["pe_ttm"]), float(item["pb"]), float(item["roe"])
                except (KeyError, TypeError, ValueError):
                    continue
                if pe_ttm <= 0 or pb <= 0 or roe <= 0:
                    continue
                ranked_item = dict(item)
                ranked_item["_factor_roe"] = roe
                ranked_item["_factor_pe"] = pe_ttm
                ranked_item["_factor_pb"] = pb
                ranked.append(ranked_item)
            from bisect import bisect_right
            roe_values = sorted(item["_factor_roe"] for item in ranked)
            pe_values = sorted(item["_factor_pe"] for item in ranked)
            pb_values = sorted(item["_factor_pb"] for item in ranked)
            for item in ranked:
                score = (
                    bisect_right(roe_values, item["_factor_roe"]) / len(ranked)
                    + 1 - bisect_right(pe_values, item["_factor_pe"]) / len(ranked)
                    + 1 - bisect_right(pb_values, item["_factor_pb"]) / len(ranked)
                )
                item["factor_score"] = round(score * 100, 2)
                for key in ("_factor_roe", "_factor_pe", "_factor_pb"):
                    item.pop(key, None)
            matches = sorted(ranked, key=lambda item: float(item["factor_score"]), reverse=True)
        elif rank_by == "growth":
            ranked = []
            for item in matches:
                try:
                    revenue_growth = float(item["revenue_growth"])
                    net_profit_growth = float(item["net_profit_growth"])
                except (KeyError, TypeError, ValueError):
                    continue
                if revenue_growth <= 0 or net_profit_growth <= 0:
                    continue
                ranked_item = dict(item)
                ranked_item["_factor_revenue_growth"] = revenue_growth
                ranked_item["_factor_net_profit_growth"] = net_profit_growth
                ranked.append(ranked_item)
            from bisect import bisect_right
            revenue_values = sorted(item["_factor_revenue_growth"] for item in ranked)
            net_profit_values = sorted(item["_factor_net_profit_growth"] for item in ranked)
            for item in ranked:
                score = (
                    bisect_right(revenue_values, item["_factor_revenue_growth"]) / len(ranked)
                    + bisect_right(net_profit_values, item["_factor_net_profit_growth"]) / len(ranked)
                )
                item["factor_score"] = round(score * 100, 2)
                item.pop("_factor_revenue_growth", None)
                item.pop("_factor_net_profit_growth", None)
            matches = sorted(ranked, key=lambda item: float(item["factor_score"]), reverse=True)
        elif rank_by == "momentum":
            ranked = []
            for item in matches:
                try:
                    momentum_20d = float(item["momentum_20d"])
                    momentum_60d = float(item["momentum_60d"])
                except (KeyError, TypeError, ValueError):
                    continue
                ranked_item = dict(item)
                ranked_item["_factor_momentum_20d"] = momentum_20d
                ranked_item["_factor_momentum_60d"] = momentum_60d
                ranked.append(ranked_item)
            from bisect import bisect_right
            momentum_20d_values = sorted(item["_factor_momentum_20d"] for item in ranked)
            momentum_60d_values = sorted(item["_factor_momentum_60d"] for item in ranked)
            for item in ranked:
                score = (
                    bisect_right(momentum_20d_values, item["_factor_momentum_20d"]) / len(ranked)
                    + bisect_right(momentum_60d_values, item["_factor_momentum_60d"]) / len(ranked)
                )
                item["factor_score"] = round(score * 100, 2)
                item.pop("_factor_momentum_20d", None)
                item.pop("_factor_momentum_60d", None)
            matches = sorted(ranked, key=lambda item: float(item["factor_score"]), reverse=True)
        minimum_keys = list((conditions.get("min") or {}).keys())
        maximum_keys = list((conditions.get("max") or {}).keys())
        if rank_by:
            pass
        elif minimum_keys:
            key = minimum_keys[0]
            def sort_minimum(item: dict) -> float:
                try:
                    return float(item.get(key))
                except (TypeError, ValueError):
                    return float("-inf")
            matches.sort(key=sort_minimum, reverse=True)
        elif maximum_keys:
            key = maximum_keys[0]
            def sort_maximum(item: dict) -> float:
                try:
                    return float(item.get(key))
                except (TypeError, ValueError):
                    return float("inf")
            matches.sort(key=sort_maximum)
        else:
            matches.sort(key=lambda item: str(item.get("code") or ""))
        return {"ok": True, "scope": "mootdx_full_universe", "as_of": cache.get("generated_at"), "conditions": conditions, "rank_by": rank_by or None, "matches": matches[:200], "match_count": len(matches), "source": cache.get("source"), "universe_size": universe_size, "metric_coverage": metric_coverage, "growth_report_period": cache.get("growth_report_period"), "quote_coverage": quote_coverage, "valuation_coverage": cache.get("valuation_coverage"), "momentum_coverage": momentum_coverage, "momentum_as_of": cache.get("momentum_refreshed_at"), "screening_defaults": {"exclude_st": exclude_st}, "limitations": cache.get("limitations")}
    conditions = request.get("conditions") or {}
    matches = []
    rejected = []
    quote_map = {str(item.get("code") or "").zfill(6): item for item in (_quote(codes).get("quotes") or [])}
    for code in codes:
        data = _financial(code)
        if not data.get("ok"):
            rejected.append({"code": code, "reason": data.get("error"), "evaluated": False})
            continue
        metrics = dict(data.get("metrics") or {})
        quote = quote_map.get("".join(char for char in code if char.isdigit())[-6:].zfill(6), {})
        metrics.update({key: quote.get(key) for key in ("pe_ttm", "pb", "turnover_rate_pct")})
        passed = True
        for key, minimum in (conditions.get("min") or {}).items():
            try:
                value = _number(metrics.get(key))
                passed = passed and value is not None and value >= float(minimum)
            except Exception:
                passed = False
        for key, maximum in (conditions.get("max") or {}).items():
            try:
                value = _number(metrics.get(key))
                passed = passed and value is not None and value <= float(maximum)
            except Exception:
                passed = False
        display_name = str(quote.get("name") or "").strip()
        (matches if passed else rejected).append(
            {"code": code, "name": display_name, "metrics": metrics}
            if passed
            else {"code": code, "name": display_name, "reason": "conditions_not_met", "metrics": metrics}
        )
    return {"ok": True, "scope": "provided_codes", "conditions": conditions, "matches": matches, "rejected": rejected, "source": "akshare_financial"}


def _dispatch(request: dict[str, Any]) -> dict[str, Any]:
    action = str(request.get("action") or "").strip().lower()
    if action == "resolve":
        return _resolve_symbol(str(request.get("query") or request.get("code") or ""))
    if action == "quote":
        return _quote(request.get("codes") or [])
    if action == "kline":
        return _kline(str(request.get("code") or ""), str(request.get("period") or "day"), int(request.get("limit") or 240))
    if action == "technical":
        return _technical(str(request.get("code") or ""), str(request.get("period") or "day"))
    if action == "backtest":
        return _backtest(str(request.get("code") or ""), int(request.get("days") or 250))
    if action == "financial":
        return _financial(str(request.get("code") or ""))
    if action == "fundamental":
        return _fundamental(str(request.get("code") or ""))
    if action == "macro":
        return _macro()
    if action == "announcements":
        return _announcements(str(request.get("code") or ""), str(request.get("date") or ""))
    if action == "news_interpretation":
        return _news_interpretation(str(request.get("code") or ""), str(request.get("date") or ""))
    if action == "screener":
        return _screener(request)
    if action == "market_review":
        from domain.services.market_review_service import build_market_review
        return build_market_review(str(request.get("date") or ""))
    if action == "portfolio":
        from domain.services.decision_monitor_service import build_decision_monitor
        monitor = build_decision_monitor(slot="skill")
        # Keep every Skill action on the same success/failure envelope.
        monitor.setdefault("ok", True)
        return monitor
    if action == "risk_alert":
        from domain.services.decision_monitor_service import build_decision_monitor
        monitor = build_decision_monitor(slot="skill-risk-alert")
        if not monitor.get("trading_session"):
            return {"ok": True, "trading_session": False, "alerts": [], "source": "decision_monitor"}
        alerts = [
            {"code": item.get("code"), "name": item.get("name"), "state": item.get("decision_state"), "suggestion": item.get("suggestion"), "execution_hint": item.get("execution_hint"), "triggers": ["decision_reduce_priority"]}
            for item in (monitor.get("tracked_positions") or [])
            if item.get("decision_state") == "reduce_priority"
        ]
        for alert in alerts:
            technical = _technical(str(alert.get("code") or ""))
            if not technical.get("ok"):
                continue
            if technical.get("macd_cross") == "death_cross":
                alert["triggers"].append("macd_death_cross")
            if technical.get("close", 0) >= technical.get("boll_upper", float("inf")):
                alert["triggers"].append("bollinger_upper_touch")
            alert["technical_as_of"] = technical.get("as_of")
            notice = _news_interpretation(str(alert.get("code") or ""))
            if notice.get("ok") and int((notice.get("counts") or {}).get("negative_titles") or 0) > 0:
                alert["triggers"].append("negative_announcement_title")
                alert["announcement_date"] = notice.get("date")
                alert["negative_announcement_count"] = (notice.get("counts") or {}).get("negative_titles")
        return {"ok": True, "trading_session": True, "alerts": alerts, "source": "decision_monitor+tencent_kline"}
    if action == "sentiment":
        from domain.services.market_review_service import build_market_review
        requested_date = str(request.get("date") or "")
        # Market breadth is the core result. Capital-flow enrichment must never
        # serially add a 30-second AkShare wait to an interactive Feishu query.
        with ThreadPoolExecutor(max_workers=2) as pool:
            review_future = pool.submit(build_market_review, requested_date)
            flow_future = pool.submit(_capital_flow, requested_date, 8)
            review = review_future.result()
            flow = flow_future.result()
        result = {key: review.get(key) for key in ("as_of", "limit_up", "limit_down", "max_height", "ladders", "sentiment", "data_ok")}
        result["capital_flow"] = flow
        return result
    if action == "capital_flow":
        return _capital_flow(str(request.get("date") or ""))
    if action == "sector_flow":
        return _sector_flow(int(request.get("limit") or 10))
    if action == "news":
        from domain.services.global_impact_service import build_global_impact_brief
        return build_global_impact_brief(limit=80, min_score=45, top_n=5, use_cache=True)
    return {"ok": False, "error": "unsupported_action", "supported": ["resolve", "quote", "kline", "technical", "backtest", "financial", "fundamental", "macro", "announcements", "news_interpretation", "screener", "market_review", "capital_flow", "sector_flow", "portfolio", "risk_alert", "sentiment", "news"]}


def main() -> None:
    try:
        request = json.loads(sys.stdin.read() or "{}")
        print(json.dumps(_dispatch(request if isinstance(request, dict) else {}), ensure_ascii=False, default=str))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Structured A-share post-market review from public market-data endpoints."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Dict, List


INDEXES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
}
THS_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.10jqka.com.cn/"}


def _get_json(url: str, headers: Dict[str, str] | None = None, timeout: int = 8) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8", errors="replace"))
    return value if isinstance(value, dict) else {}


def _latest_trading_day(value: str) -> str:
    current = date.fromisoformat(value)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current.isoformat()


def _fetch_index_history(symbol: str, name: str, as_of: str = "") -> Dict[str, Any]:
    end = date.fromisoformat(as_of) if as_of else date.today()
    start = end - timedelta(days=14)
    param = f"{symbol},day,{start.isoformat()},{end.isoformat()},10,none"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + urllib.parse.quote(param, safe=",")
    try:
        payload = _get_json(url)
        rows = ((payload.get("data") or {}).get(symbol) or {}).get("day") or []
    except Exception as exc:
        return {"symbol": symbol, "name": name, "rows": [], "error": type(exc).__name__}
    normalized = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        open_price, close, high, low, volume = (float(row[i] or 0) for i in range(1, 6))
        normalized.append({"date": row[0], "open": open_price, "close": close, "high": high, "low": low, "volume": volume})
    normalized.sort(key=lambda item: item["date"])
    for index, row in enumerate(normalized):
        previous = normalized[index - 1]["close"] if index else 0
        row["change_pct"] = round((row["close"] - previous) / previous * 100, 2) if previous else None
    return {"symbol": symbol, "name": name, "rows": normalized[-5:], "latest_date": normalized[-1]["date"] if normalized else ""}


def _fetch_index_turnover(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        text = urllib.request.urlopen(req, timeout=8).read().decode("gb18030", errors="replace")
    except Exception:
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for statement in text.split(";"):
        if "=" not in statement:
            continue
        key, raw = statement.split("=", 1)
        fields = raw.strip().strip('"').split("~")
        if len(fields) < 38:
            continue
        symbol = key.strip().replace("v_", "")
        result[symbol] = {
            "as_of": fields[30] if len(fields) > 30 else "",
            "change_pct": float(fields[32] or 0),
            # Tencent field 37 is turnover in 10k CNY.
            "turnover_yi": round(float(fields[37] or 0) / 10000, 2),
        }
    return result


def _ths(path: str, target: str) -> Dict[str, Any]:
    url = "https://data.10jqka.com.cn/dataapi/limit_up/" + path + "?" + urllib.parse.urlencode({"filter": "HS,GEM2STAR", "date": target.replace("-", "")})
    try:
        return _get_json(url, THS_HEADERS)
    except Exception as exc:
        return {"status_code": -1, "error": type(exc).__name__}


def build_market_review(target_date: str = "") -> Dict[str, Any]:
    requested = target_date or date.today().isoformat()
    as_of = _latest_trading_day(requested)
    # These seven public endpoints are independent and each has its own timeout.
    # Parallel fetching keeps one slow source from serially blocking Feishu replies.
    with ThreadPoolExecutor(max_workers=7) as executor:
        history_futures = {
            symbol: executor.submit(_fetch_index_history, symbol, name, as_of)
            for symbol, name in INDEXES.items()
        }
        turnover_future = executor.submit(_fetch_index_turnover, list(INDEXES))
        pool_future = executor.submit(_ths, "limit_up_pool", as_of)
        ladder_future = executor.submit(_ths, "continuous_limit_up", as_of)
        down_future = executor.submit(_ths, "limit_down_pool", as_of)
        histories = [history_futures[symbol].result() for symbol in INDEXES]
        turnover = turnover_future.result()
        pool = pool_future.result()
        ladder = ladder_future.result()
        down = down_future.result()
    pool_data = pool.get("data") or {}
    pool_ok = pool.get("status_code") == 0 and isinstance(pool.get("data"), dict)
    ladder_ok = ladder.get("status_code") == 0 and isinstance(ladder.get("data"), list)
    down_ok = down.get("status_code") == 0 and isinstance(down.get("data"), dict)
    limit_up = int(((pool_data.get("page") or {}).get("total") or 0)) if pool_ok else None
    limit_down = int((((down.get("data") or {}).get("page") or {}).get("total") or 0)) if down_ok else None
    ladders = []
    for item in (ladder.get("data") or []):
        if not isinstance(item, dict):
            continue
        codes = item.get("code_list") or []
        ladders.append({"height": int(item.get("height") or 0), "stocks": [{"code": str(x.get("code") or ""), "name": str(x.get("name") or "")} for x in codes[:12] if isinstance(x, dict)]})
    max_height = max((row["height"] for row in ladders), default=0) if ladder_ok else None
    latest_changes = [float((turnover.get(symbol) or {}).get("change_pct") or 0) for symbol in INDEXES]
    if limit_up is not None and max_height is not None and limit_up >= 70 and max_height >= 3 and sum(latest_changes) > 0:
        sentiment = "偏强"
    elif (limit_down is not None and limit_up is not None and limit_down > limit_up) or sum(latest_changes) < -3:
        sentiment = "偏弱"
    else:
        sentiment = "分歧/中性"
    for item in histories:
        quote = turnover.get(item["symbol"]) or {}
        item["quote_as_of"] = quote.get("as_of", "")
        item["turnover_yi"] = quote.get("turnover_yi") if str(quote.get("as_of") or "")[:8] == as_of.replace("-", "") else None
    quality_issues = []
    for item in histories:
        if item.get("latest_date") != as_of:
            quality_issues.append(f"{item.get('symbol')}_history_date_mismatch")
        if item.get("turnover_yi") is None:
            quality_issues.append(f"{item.get('symbol')}_turnover_date_mismatch_or_missing")
    if not pool_ok:
        quality_issues.append("limit_up_unavailable")
    if not down_ok:
        quality_issues.append("limit_down_unavailable")
    if not ladder_ok:
        quality_issues.append("limit_ladder_unavailable")
    return {
        "requested_date": requested,
        "as_of": as_of,
        "indices": histories,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "ladders": sorted(ladders, key=lambda x: -x["height"]),
        "max_height": max_height,
        "sentiment": sentiment,
        "data_ok": bool(any(x.get("rows") for x in histories)),
        "quality_ok": not quality_issues,
        "quality_issues": quality_issues,
        "breadth_sources": {"limit_up": pool_ok, "limit_down": down_ok, "ladder": ladder_ok},
    }


def format_market_review(review: Dict[str, Any]) -> List[str]:
    lines = [f"市场复盘（数据日 {review.get('as_of')}）", "5日指数:"]
    for item in review.get("indices") or []:
        rows = item.get("rows") or []
        if not rows:
            lines.append(f"- {item.get('name')}: 数据获取失败")
            continue
        compact = " | ".join(f"{r['date'][5:]} {r['close']:.2f} " + (f"{r['change_pct']:+.2f}%" if r['change_pct'] is not None else "变动率缺失") for r in rows)
        turnover = item.get("turnover_yi")
        lines.append(f"- {item.get('name')}: {compact}; 当日成交额={turnover:.0f}亿" if turnover is not None else f"- {item.get('name')}: {compact}; 当日成交额=未取得")
    index_map = {item.get("symbol"): item for item in review.get("indices") or []}
    sh_turnover = index_map.get("sh000001", {}).get("turnover_yi")
    sz_turnover = index_map.get("sz399001", {}).get("turnover_yi")
    if sh_turnover is not None and sz_turnover is not None:
        lines.append(f"两市成交额: {float(sh_turnover) + float(sz_turnover):.0f}亿（沪市 {float(sh_turnover):.0f}亿 + 深市 {float(sz_turnover):.0f}亿）")
    limit_up = review.get("limit_up")
    limit_down = review.get("limit_down")
    max_height = review.get("max_height")
    lines.append(f"涨停={limit_up if limit_up is not None else '未取得'} | 跌停={limit_down if limit_down is not None else '未取得'} | 最高连板={(str(max_height) + '板') if max_height is not None else '未取得'} | 情绪={review.get('sentiment')}")
    ladders = review.get("ladders") or []
    if ladders:
        lines.append("连板梯队:")
        for row in ladders[:4]:
            names = "、".join(f"{x.get('name')}({x.get('code')})" for x in row.get("stocks") or []) or "无"
            lines.append(f"- {row.get('height')}板: {names}")
    else:
        lines.append("连板梯队: 未取得")
    if review.get("quality_issues"):
        lines.append("数据质量告警: " + ", ".join(str(x) for x in review.get("quality_issues") or []))
    return lines

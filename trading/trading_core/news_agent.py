import json
import os
from datetime import datetime, timedelta
from typing import Dict, List
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen
import requests
try:
    import akshare as ak
except Exception:  # pragma: no cover
    ak = None

from .analysis import MX_APIKEY, MX_API_BASE
from .models import StockNewsSnapshot, StockRule
from .paths import LOCAL_SECRETS_PATH, NEWS_SNAPSHOTS_CACHE_PATH, TICKDB_CACHE_PATH
from .storage import atomic_write_json, load_json


POSITIVE_KEYWORDS = ["利好", "增长", "中标", "合作", "订单", "突破", "上涨", "回暖", "催化", "景气"]
NEGATIVE_KEYWORDS = ["减持", "下跌", "风险", "监管", "问询", "亏损", "利空", "冲突", "战争", "制裁", "回撤"]
MACRO_QUERIES = [
    "A股 市场 最新 利好 利空",
    "半导体 算力 AI 板块 最新 催化 风险",
    "国际局势 战争 制裁 科技板块 影响",
]
FINNHUB_API_BASE = "https://finnhub.io/api/v1"
TICKDB_API_BASE = "https://api.tickdb.ai/v1"
FINNHUB_MACRO_KEYWORDS = {
    "market_policy": ["china", "beijing", "policy", "stimulus", "easing", "regulation", "tariff"],
    "war_risk": ["war", "conflict", "iran", "israel", "sanction", "middle east"],
    "ai_compute": ["ai", "artificial intelligence", "gpu", "server", "cloud", "datacenter"],
    "semiconductor": ["semiconductor", "chip", "memory", "foundry", "wafer"],
    "lithium_energy": ["lithium", "battery", "ev", "electric vehicle", "solar"],
}
EVENT_RULES = [
    {"tag": "war_risk", "keywords": ["战争", "冲突", "制裁"], "direction": "negative", "horizon": "short"},
    {"tag": "policy_support", "keywords": ["政策", "扶持", "利好", "催化"], "direction": "positive", "horizon": "mid"},
    {"tag": "earnings_pressure", "keywords": ["亏损", "问询", "减持"], "direction": "negative", "horizon": "mid"},
    {"tag": "order_growth", "keywords": ["订单", "中标", "合作"], "direction": "positive", "horizon": "mid"},
    {"tag": "sector_rotation", "keywords": ["板块", "轮动", "回暖"], "direction": "positive", "horizon": "short"},
    {"tag": "war_risk", "keywords": ["war", "conflict", "iran", "israel", "sanction"], "direction": "negative", "horizon": "short"},
    {"tag": "policy_support", "keywords": ["policy", "stimulus", "subsidy", "easing"], "direction": "positive", "horizon": "mid"},
    {"tag": "earnings_pressure", "keywords": ["loss", "warning", "probe", "investigation"], "direction": "negative", "horizon": "mid"},
    {"tag": "order_growth", "keywords": ["order", "contract", "partnership", "backlog"], "direction": "positive", "horizon": "mid"},
    {"tag": "sector_rotation", "keywords": ["rotation", "rebound", "recovery", "rally"], "direction": "positive", "horizon": "short"},
]
EVENT_DECAY_RULES = {
    "war_risk": {"fresh_hours": 12, "active_hours": 36, "cooling_hours": 72},
    "policy_support": {"fresh_hours": 24, "active_hours": 96, "cooling_hours": 168},
    "earnings_pressure": {"fresh_hours": 24, "active_hours": 120, "cooling_hours": 240},
    "order_growth": {"fresh_hours": 24, "active_hours": 120, "cooling_hours": 240},
    "sector_rotation": {"fresh_hours": 12, "active_hours": 48, "cooling_hours": 96},
}
OVERSEAS_PEER_MAPPING = {
    "存储芯片": [
        {"name": "美光科技", "tickdb_symbols": ["MU.US"], "symbols": ["MU"]},
        {"name": "西部数据", "tickdb_symbols": ["WDC.US"], "symbols": ["WDC"]},
        {"name": "SK海力士", "tickdb_symbols": ["000660.KS"], "symbols": ["000660.KS", "000660"]},
        {"name": "三星电子", "tickdb_symbols": ["005930.KS"], "symbols": ["005930.KS", "005930"]},
    ],
    "半导体": [
        {"name": "美光科技", "tickdb_symbols": ["MU.US"], "symbols": ["MU"]},
        {"name": "台积电", "tickdb_symbols": ["TSM.US"], "symbols": ["TSM"]},
        {"name": "阿斯麦", "tickdb_symbols": ["ASML.US"], "symbols": ["ASML"]},
    ],
    "算力": [
        {"name": "英伟达", "tickdb_symbols": ["NVDA.US"], "symbols": ["NVDA"]},
        {"name": "超威半导体", "tickdb_symbols": ["AMD.US"], "symbols": ["AMD"]},
        {"name": "博通", "tickdb_symbols": ["AVGO.US"], "symbols": ["AVGO"]},
    ],
}
SECTOR_TAG_ALIASES = {
    "存储": "存储芯片",
    "存储器": "存储芯片",
    "dram": "存储芯片",
    "nand": "存储芯片",
    "hbm": "存储芯片",
    "企业级存储": "存储芯片",
    "芯片": "半导体",
    "晶圆": "半导体",
    "晶圆代工": "半导体",
    "半导体设备": "半导体",
    "半导体材料": "半导体",
    "ai算力": "算力",
    "人工智能": "算力",
    "云计算": "算力",
    "东数西算": "算力",
    "数据中心": "算力",
}
OVERSEAS_PEER_SEVERE_DROP_PCT = -4.0
OVERSEAS_PEER_WEAK_DROP_PCT = -2.5
OVERSEAS_STORAGE_HARD_BLOCK_PCT = -6.0
TICKDB_MAX_REQUESTS_PER_MINUTE = 30
TICKDB_CACHE_SECONDS = 120
AKSHARE_CACHE_SECONDS = 600
NEWS_PLATFORM_HINTS = [
    {"name": "财联社", "id": "cls"},
    {"name": "华尔街见闻", "id": "wallstreetcn"},
    {"name": "第一财经", "id": "yicai"},
    {"name": "东方财富", "id": "eastmoney"},
    {"name": "新浪财经", "id": "sina"},
    {"name": "同花顺", "id": "ths_10jqka"},
    {"name": "富途牛牛", "id": "futunn"},
    {"name": "21财经", "id": "21jingji"},
    {"name": "和讯网", "id": "hexun"},
    {"name": "金融界", "id": "jrj"},
]
MACRO_NEWS_PLATFORM_IDS = ["cls", "wallstreetcn", "yicai", "eastmoney"]
SECTOR_NEWS_PLATFORM_IDS = ["cls", "eastmoney", "ths_10jqka", "sina"]
STOCK_NEWS_PLATFORM_IDS = ["cls", "eastmoney", "sina", "ths_10jqka", "futunn"]


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


def _refresh_slot(now: datetime | None = None) -> str:
    now = now or datetime.now()
    hm = now.hour * 100 + now.minute
    date_str = now.strftime("%Y-%m-%d")
    if 835 <= hm <= 920:
        return f"{date_str}:preopen"
    if 930 <= hm < 1000:
        return f"{date_str}:open"
    if 1000 <= hm < 1100:
        return f"{date_str}:mid_am"
    if 1100 <= hm <= 1130:
        return f"{date_str}:late_am"
    if 1300 <= hm < 1400:
        return f"{date_str}:early_pm"
    if 1400 <= hm <= 1500:
        return f"{date_str}:late_pm"
    return f"{date_str}:offhours"


def _load_news_cache() -> Dict:
    return load_json(NEWS_SNAPSHOTS_CACHE_PATH, {"updated_at": "", "refresh_slot": "", "codes": [], "snapshots": {}})


def _load_tickdb_cache() -> Dict:
    return load_json(
        TICKDB_CACHE_PATH,
        {"updated_at": "", "minute_slot": "", "request_count": 0, "quotes": {}, "last_errors": {}},
    )


def _load_akshare_cache() -> Dict:
    cache = _load_tickdb_cache()
    akshare_quotes = cache.get("akshare_quotes")
    if not isinstance(akshare_quotes, dict):
        cache["akshare_quotes"] = {}
    return cache


def _save_tickdb_cache(cache: Dict) -> None:
    cache["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    atomic_write_json(TICKDB_CACHE_PATH, cache)


def _save_news_cache(refresh_slot: str, codes: List[str], snapshots: Dict[str, StockNewsSnapshot]) -> None:
    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "refresh_slot": refresh_slot,
        "codes": sorted(codes),
        "snapshots": {code: snapshot.__dict__ for code, snapshot in snapshots.items()},
    }
    atomic_write_json(NEWS_SNAPSHOTS_CACHE_PATH, payload)


def _cache_to_snapshots(data: Dict) -> Dict[str, StockNewsSnapshot]:
    snapshots = {}
    for code, item in (data.get("snapshots") or {}).items():
        if not isinstance(item, dict):
            continue
        snapshots[str(code)] = StockNewsSnapshot(
            code=str(item.get("code", code)),
            stock_score=int(item.get("stock_score", 0) or 0),
            stock_sentiment=str(item.get("stock_sentiment", "neutral")),
            stock_items=list(item.get("stock_items", [])),
            sector_score=int(item.get("sector_score", 0) or 0),
            sector_sentiment=str(item.get("sector_sentiment", "neutral")),
            sector_items=list(item.get("sector_items", [])),
            macro_score=int(item.get("macro_score", 0) or 0),
            macro_sentiment=str(item.get("macro_sentiment", "neutral")),
            macro_items=list(item.get("macro_items", [])),
            event_tags=list(item.get("event_tags", [])),
            overseas_peer_score=int(item.get("overseas_peer_score", 0) or 0),
            overseas_peer_sentiment=str(item.get("overseas_peer_sentiment", "neutral")),
            overseas_peer_items=list(item.get("overseas_peer_items", [])),
            overseas_peer_block_buy=bool(item.get("overseas_peer_block_buy", False)),
            overseas_peer_block_reason=str(item.get("overseas_peer_block_reason", "") or ""),
        )
    return snapshots


def _parse_published_at(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _event_recency(tag_name: str, published_at: str, now: datetime | None = None) -> Dict:
    now = now or datetime.now()
    rules = EVENT_DECAY_RULES.get(tag_name, {"fresh_hours": 24, "active_hours": 72, "cooling_hours": 168})
    event_dt = _parse_published_at(published_at)
    if event_dt is None:
        return {"stage": "fresh", "weight": 1.0, "age_hours": 0}
    age_hours = max(0.0, (now - event_dt).total_seconds() / 3600)
    if age_hours <= rules["fresh_hours"]:
        return {"stage": "fresh", "weight": 1.0, "age_hours": round(age_hours, 1)}
    if age_hours <= rules["active_hours"]:
        return {"stage": "active", "weight": 0.7, "age_hours": round(age_hours, 1)}
    if age_hours <= rules["cooling_hours"]:
        return {"stage": "cooling", "weight": 0.35, "age_hours": round(age_hours, 1)}
    return {"stage": "stale", "weight": 0.0, "age_hours": round(age_hours, 1)}


def _load_local_secret(name: str) -> str:
    data = load_json(LOCAL_SECRETS_PATH, {})
    value = str(data.get(name, "")).strip() if isinstance(data, dict) else ""
    return value


def _finnhub_api_key() -> str:
    return os.environ.get("FINNHUB_API_KEY", "").strip() or _load_local_secret("FINNHUB_API_KEY")


def _tickdb_api_key() -> str:
    return os.environ.get("TICKDB_API_KEY", "").strip() or _load_local_secret("TICKDB_API_KEY")


def _tickdb_cache_key(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _tickdb_parse_change(item: Dict) -> float | None:
    if not isinstance(item, dict):
        return None
    for key in (
        "change_percent",
        "changePct",
        "chg_pct",
        "percent_change",
        "pct",
        "pc",
        "price_change_percent_24h",
    ):
        try:
            value = item.get(key)
            if value not in (None, ""):
                parsed = float(value)
                if abs(parsed) > 30:
                    parsed /= 100.0
                return round(parsed, 2)
        except (TypeError, ValueError):
            continue
    last_price = item.get("last") or item.get("close") or item.get("price")
    prev_close = item.get("prev_close") or item.get("previous_close") or item.get("pre_close")
    try:
        last_val = float(last_price)
        prev_val = float(prev_close)
    except (TypeError, ValueError):
        return None
    if last_val <= 0 or prev_val <= 0:
        return None
    return round((last_val - prev_val) / prev_val * 100, 2)


def _akshare_us_daily_change(symbol: str) -> float | None:
    if ak is None:
        return None
    base = str(symbol or "").strip().upper().replace(".US", "")
    if not base:
        return None
    frame = ak.stock_us_daily(symbol=base)
    if frame is None or getattr(frame, "empty", True):
        return None
    recent = frame.tail(2)
    if len(recent.index) < 2:
        return None
    prev_close = float(recent.iloc[-2]["close"])
    last_close = float(recent.iloc[-1]["close"])
    if prev_close <= 0:
        return None
    return round((last_close - prev_close) / prev_close * 100, 2)


def _akshare_hk_daily_change(symbol: str) -> float | None:
    if ak is None:
        return None
    base = str(symbol or "").strip().upper().replace(".HK", "")
    if not base:
        return None
    normalized = base.zfill(5)
    frame = ak.stock_hk_daily(symbol=normalized)
    if frame is None or getattr(frame, "empty", True):
        return None
    recent = frame.tail(2)
    if len(recent.index) < 2:
        return None
    prev_close = float(recent.iloc[-2]["close"])
    last_close = float(recent.iloc[-1]["close"])
    if prev_close <= 0:
        return None
    return round((last_close - prev_close) / prev_close * 100, 2)


def _akshare_symbol_change(symbol: str) -> float | None:
    normalized = _tickdb_cache_key(symbol)
    if not normalized:
        return None

    cache = _load_akshare_cache()
    quotes = cache.get("akshare_quotes", {})
    now = datetime.now()
    now_ts = now.timestamp()
    cached = quotes.get(normalized)
    if isinstance(cached, dict):
        fetched_at = _parse_published_at(str(cached.get("fetched_at", "") or ""))
        if fetched_at and now_ts - fetched_at.timestamp() <= AKSHARE_CACHE_SECONDS:
            try:
                value = cached.get("change_percent")
                return None if value is None else round(float(value), 2)
            except (TypeError, ValueError):
                pass

    proxy_keys = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
    saved_env = {key: os.environ.get(key) for key in proxy_keys}
    try:
        for key in proxy_keys:
            os.environ.pop(key, None)
        if normalized.endswith(".US"):
            change = _akshare_us_daily_change(normalized)
        elif normalized.endswith(".HK"):
            change = _akshare_hk_daily_change(normalized)
        else:
            change = None
    except Exception as exc:
        cache.setdefault("last_errors", {})[f"akshare:{normalized}"] = str(exc)[:300]
        _save_tickdb_cache(cache)
        return None
    finally:
        for key, value in saved_env.items():
            if value:
                os.environ[key] = value

    quotes[normalized] = {
        "change_percent": change,
        "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    cache["akshare_quotes"] = quotes
    _save_tickdb_cache(cache)
    return change


def _tickdb_batch_quote_changes(symbols: List[str]) -> Dict[str, float | None]:
    normalized = [_tickdb_cache_key(symbol) for symbol in symbols if str(symbol or "").strip()]
    if not normalized:
        return {}
    api_key = _tickdb_api_key()
    if not api_key:
        return {symbol: None for symbol in normalized}

    now = datetime.now()
    now_ts = now.timestamp()
    minute_slot = now.strftime("%Y-%m-%d %H:%M")
    cache = _load_tickdb_cache()
    if cache.get("minute_slot") != minute_slot:
        cache["minute_slot"] = minute_slot
        cache["request_count"] = 0

    quotes = cache.get("quotes", {}) if isinstance(cache.get("quotes"), dict) else {}
    results: Dict[str, float | None] = {}
    pending: List[str] = []
    for symbol in normalized:
        cached = quotes.get(symbol)
        if isinstance(cached, dict):
            fetched_at = str(cached.get("fetched_at", "") or "")
            change_pct = cached.get("change_percent")
            if fetched_at:
                fetched_dt = _parse_published_at(fetched_at)
                if fetched_dt and now_ts - fetched_dt.timestamp() <= TICKDB_CACHE_SECONDS:
                    try:
                        results[symbol] = None if change_pct is None else round(float(change_pct), 2)
                        continue
                    except (TypeError, ValueError):
                        pass
        pending.append(symbol)

    if not pending:
        return results
    if int(cache.get("request_count", 0) or 0) >= TICKDB_MAX_REQUESTS_PER_MINUTE:
        for symbol in pending:
            results[symbol] = None
        return results

    try:
        response = requests.get(
            f"{TICKDB_API_BASE}/market/ticker",
            headers={"X-API-Key": api_key},
            params={"symbols": ",".join(pending)},
            timeout=15,
            proxies={
                "http": os.environ.get("HTTP_PROXY", ""),
                "https": os.environ.get("HTTPS_PROXY", ""),
            },
        )
        data = response.json()
    except Exception as exc:
        cache.setdefault("last_errors", {})[minute_slot] = str(exc)
        _save_tickdb_cache(cache)
        for symbol in pending:
            results[symbol] = None
        return results

    cache["request_count"] = int(cache.get("request_count", 0) or 0) + 1
    payload = data.get("data") if isinstance(data, dict) else None
    if isinstance(payload, list):
        payload = {
            _tickdb_cache_key(item.get("symbol", "")): item
            for item in payload
            if isinstance(item, dict) and str(item.get("symbol", "")).strip()
        }
    if response.status_code >= 400 or not isinstance(payload, dict):
        if isinstance(data, dict) and int(data.get("code", 0) or 0) == 3001:
            cache["request_count"] = TICKDB_MAX_REQUESTS_PER_MINUTE
        cache.setdefault("last_errors", {})[minute_slot] = str(data)[:500]
        _save_tickdb_cache(cache)
        for symbol in pending:
            results[symbol] = None
        return results

    fetched_at = now.strftime("%Y-%m-%d %H:%M:%S")
    for symbol in pending:
        item = payload.get(symbol) or payload.get(symbol.upper()) or payload.get(symbol.lower()) or {}
        change_pct = _tickdb_parse_change(item)
        quotes[symbol] = {"change_percent": change_pct, "fetched_at": fetched_at, "raw": item}
        results[symbol] = change_pct
    cache["quotes"] = quotes
    _save_tickdb_cache(cache)
    return results


def _platform_lookup(platform_id: str) -> Dict:
    for item in NEWS_PLATFORM_HINTS:
        if item["id"] == platform_id:
            return item
    return {"name": platform_id, "id": platform_id}


def _normalize_news_item(item: Dict, fallback_source: str) -> Dict:
    title = str(item.get("title", "")).strip()
    content = str(item.get("content", "")).strip()
    if not title:
        return {}
    published_at = str(item.get("publishTime") or item.get("published_at") or item.get("time") or "").strip()
    url = str(item.get("url") or item.get("link") or "").strip()
    source_name = str(item.get("source") or item.get("media") or item.get("site") or "").strip()
    source = fallback_source if not source_name else f"{fallback_source}|{source_name}"
    return {
        "title": title,
        "content": content[:120],
        "source": source,
        "published_at": published_at,
        "url": url,
    }


def _search_news_items(query: str, limit: int = 3, platform_id: str = "") -> List[Dict]:
    if not MX_APIKEY:
        return []
    platform = _platform_lookup(platform_id) if platform_id else {}
    search_query = query if not platform else f"{platform.get('name', '')} {query}"
    req = Request(
        f"{MX_API_BASE}/api/claw/news-search",
        data=json.dumps({"query": search_query}).encode(),
        headers={"Content-Type": "application/json", "apikey": MX_APIKEY},
        method="POST",
    )
    try:
        with _build_url_opener().open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return []
    items = ((((data.get("data") or {}).get("data") or {}).get("llmSearchResponse") or {}).get("data")) or []
    results = []
    fallback_source = "openclaw:news-search"
    if platform:
        fallback_source = f"openclaw:news-search:{platform.get('id', '')}"
    for item in items[:limit]:
        normalized = _normalize_news_item(item, fallback_source=fallback_source)
        if normalized:
            results.append(normalized)
    return results


def _search_news_items_multi(query: str, limit: int = 4, platform_ids: List[str] | None = None, base_limit: int = 2) -> List[Dict]:
    platform_ids = list(platform_ids or [])
    items: List[Dict] = []
    if base_limit > 0:
        items.extend(_search_news_items(query, limit=base_limit))
    per_platform_limit = 1 if limit <= 4 else 2
    for platform_id in platform_ids:
        items.extend(_search_news_items(query, limit=per_platform_limit, platform_id=platform_id))
        if len(_dedupe_items(items, limit=limit)) >= limit:
            break
    return _dedupe_items(items, limit=limit)


def _source_resonance_adjustment(items: List[Dict]) -> int:
    positive_sources = set()
    negative_sources = set()
    for item in items:
        text = f"{item.get('title', '')} {item.get('content', '')}"
        pos_hits = sum(1 for keyword in POSITIVE_KEYWORDS if keyword in text)
        neg_hits = sum(1 for keyword in NEGATIVE_KEYWORDS if keyword in text)
        source = str(item.get("source", "")).strip() or "unknown"
        if pos_hits > neg_hits and pos_hits > 0:
            positive_sources.add(source)
        elif neg_hits > pos_hits and neg_hits > 0:
            negative_sources.add(source)
    adjustment = 0
    if len(positive_sources) >= 2:
        adjustment += 2 if len(positive_sources) == 2 else 4
    if len(negative_sources) >= 2:
        adjustment -= 2 if len(negative_sources) == 2 else 4
    return adjustment


def _finnhub_get_json(path: str, params: Dict[str, str]) -> List[Dict]:
    api_key = _finnhub_api_key()
    if not api_key:
        return []
    query = dict(params)
    query["token"] = api_key
    req = Request(
        f"{FINNHUB_API_BASE}{path}?{urlencode(query)}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with _build_url_opener().open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _finnhub_get_object(path: str, params: Dict[str, str]) -> Dict:
    api_key = _finnhub_api_key()
    if not api_key:
        return {}
    query = dict(params)
    query["token"] = api_key
    req = Request(
        f"{FINNHUB_API_BASE}{path}?{urlencode(query)}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with _build_url_opener().open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _normalize_finnhub_items(items: List[Dict], limit: int = 6) -> List[Dict]:
    normalized = []
    for item in items[:limit]:
        title = str(item.get("headline") or item.get("title") or "").strip()
        summary = str(item.get("summary") or item.get("content") or "").strip()
        if not title:
            continue
        published_at = ""
        timestamp = item.get("datetime")
        if isinstance(timestamp, (int, float)) and timestamp > 0:
            published_at = datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M")
        normalized.append({
            "title": title,
            "content": summary[:160],
            "source": f"finnhub:{item.get('source', 'news')}",
            "published_at": published_at,
            "url": str(item.get("url", "")).strip(),
        })
    return normalized


def _contains_keywords(text: str, keywords: List[str]) -> bool:
    text_lower = text.lower()
    return any(keyword.lower() in text_lower for keyword in keywords)


def _finnhub_general_news_by_keywords(keywords: List[str], limit: int = 4) -> List[Dict]:
    raw_items = _finnhub_get_json("/news", {"category": "general"})
    if not raw_items:
        return []
    matched = []
    for item in raw_items:
        text = f"{item.get('headline', '')} {item.get('summary', '')}"
        if _contains_keywords(text, keywords):
            matched.append(item)
        if len(matched) >= limit:
            break
    return _normalize_finnhub_items(matched, limit=limit)


def _finnhub_company_news(symbol: str, limit: int = 3) -> List[Dict]:
    if not symbol:
        return []
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=7)
    raw_items = _finnhub_get_json("/company-news", {
        "symbol": symbol,
        "from": start_date.isoformat(),
        "to": end_date.isoformat(),
    })
    return _normalize_finnhub_items(raw_items, limit=limit)


def _finnhub_quote_change(symbol: str) -> float | None:
    data = _finnhub_get_object("/quote", {"symbol": symbol})
    current = data.get("c")
    prev_close = data.get("pc")
    try:
        current_val = float(current)
        prev_close_val = float(prev_close)
    except (TypeError, ValueError):
        return None
    if current_val <= 0 or prev_close_val <= 0:
        return None
    return round((current_val - prev_close_val) / prev_close_val * 100, 2)


def _tickdb_quote_change(symbol: str) -> float | None:
    return _tickdb_batch_quote_changes([symbol]).get(_tickdb_cache_key(symbol))


def _peer_negative_news_item(name: str) -> Dict:
    items = _search_news_items_multi(
        f"{name} 股价 大跌 存储 芯片",
        limit=2,
        platform_ids=["cls", "wallstreetcn", "eastmoney", "sina"],
        base_limit=1,
    )
    if not items:
        items = _search_news_items_multi(
            f"{name} 下跌 原因",
            limit=2,
            platform_ids=["cls", "wallstreetcn", "eastmoney", "sina"],
            base_limit=1,
        )
    if not items:
        return {}
    text = f"{items[0].get('title', '')} {items[0].get('content', '')}"
    if any(keyword in text for keyword in ("大跌", "暴跌", "下跌", "回撤", "承压")):
        return items[0]
    return {}


def _score_items(items: List[Dict]) -> int:
    score = 0
    for item in items:
        text = f"{item.get('title', '')} {item.get('content', '')}"
        score += sum(2 for keyword in POSITIVE_KEYWORDS if keyword in text)
        score -= sum(2 for keyword in NEGATIVE_KEYWORDS if keyword in text)
    return score


def _sentiment_from_score(score: int) -> str:
    if score >= 3:
        return "positive"
    if score <= -3:
        return "negative"
    return "neutral"


def _extract_event_tags(items: List[Dict], now: datetime | None = None) -> List[Dict]:
    tags = []
    seen = set()
    for item in items:
        text = f"{item.get('title', '')} {item.get('content', '')}"
        for rule in EVENT_RULES:
            if rule["tag"] in seen:
                continue
            if any(keyword in text for keyword in rule["keywords"]):
                seen.add(rule["tag"])
                recency = _event_recency(rule["tag"], str(item.get("published_at", "")).strip(), now=now)
                tags.append({
                    "tag": rule["tag"],
                    "direction": rule["direction"],
                    "horizon": rule["horizon"],
                    "title": item.get("title", ""),
                    "source": item.get("source", ""),
                    "published_at": item.get("published_at", ""),
                    "stage": recency["stage"],
                    "weight": recency["weight"],
                    "age_hours": recency["age_hours"],
                })
    return tags


def _dedupe_items(items: List[Dict], limit: int = 6) -> List[Dict]:
    results = []
    seen = set()
    for item in items:
        title = str(item.get("title", "")).strip()
        source = str(item.get("source", "")).strip()
        if not title:
            continue
        key = (title, source)
        if key in seen:
            continue
        seen.add(key)
        results.append(item)
        if len(results) >= limit:
            break
    return results


def _finnhub_sector_items(rule: StockRule, limit: int = 4) -> List[Dict]:
    items: List[Dict] = []
    for tag in rule.sector_tags or []:
        keywords = [tag]
        if "半导体" in tag or "芯片" in tag:
            keywords = FINNHUB_MACRO_KEYWORDS["semiconductor"]
        elif "AI" in tag or "人工智能" in tag or "算力" in tag or "云计算" in tag:
            keywords = FINNHUB_MACRO_KEYWORDS["ai_compute"]
        elif "锂" in tag or "新能源" in tag:
            keywords = FINNHUB_MACRO_KEYWORDS["lithium_energy"]
        items.extend(_finnhub_general_news_by_keywords(keywords, limit=2))
    return _dedupe_items(items, limit=limit)


def _finnhub_macro_items(limit: int = 6) -> List[Dict]:
    items: List[Dict] = []
    for keywords in FINNHUB_MACRO_KEYWORDS.values():
        items.extend(_finnhub_general_news_by_keywords(keywords, limit=2))
    return _dedupe_items(items, limit=limit)


def _mapped_overseas_peers(rule: StockRule) -> List[Dict]:
    normalized_tags = _normalized_sector_tags(rule)
    mapped: List[Dict] = []
    seen = set()
    for tag in normalized_tags:
        for peer in OVERSEAS_PEER_MAPPING.get(tag, []):
            name = str(peer.get("name", "")).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            mapped.append(peer)
    return mapped


def _normalized_sector_tags(rule: StockRule) -> List[str]:
    tags: List[str] = []
    seen = set()
    for raw in rule.sector_tags or []:
        text = str(raw or "").strip()
        if not text:
            continue
        candidates = [text]
        lowered = text.lower()
        alias = SECTOR_TAG_ALIASES.get(lowered) or SECTOR_TAG_ALIASES.get(text)
        if alias:
            candidates.append(alias)
        if "存储" in text and "存储芯片" not in candidates:
            candidates.append("存储芯片")
        if ("芯片" in text or "半导体" in text or "晶圆" in text) and "半导体" not in candidates:
            candidates.append("半导体")
        if ("算力" in text or "gpu" in lowered or "云" in text or "数据中心" in text) and "算力" not in candidates:
            candidates.append("算力")
        for item in candidates:
            if item not in seen:
                seen.add(item)
                tags.append(item)
    return tags


def _build_overseas_peer_snapshot(rule: StockRule) -> Dict:
    peers = _mapped_overseas_peers(rule)
    if not peers:
        return {"score": 0, "sentiment": "neutral", "items": [], "block_buy": False, "block_reason": ""}

    normalized_tags = _normalized_sector_tags(rule)
    items: List[Dict] = []
    severe = []
    weak = []
    scored_changes: List[float] = []
    tickdb_symbol_map: Dict[str, str] = {}
    tickdb_symbols: List[str] = []
    for peer in peers:
        for symbol in peer.get("tickdb_symbols", []) or []:
            normalized = _tickdb_cache_key(symbol)
            if normalized and normalized not in tickdb_symbol_map:
                tickdb_symbol_map[normalized] = str(peer.get("name", "")).strip()
                tickdb_symbols.append(normalized)
    tickdb_changes = _tickdb_batch_quote_changes(tickdb_symbols)
    for peer in peers:
        name = str(peer.get("name", "")).strip()
        change_pct = None
        used_symbol = ""
        used_source = ""
        for symbol in peer.get("tickdb_symbols", []) or []:
            change_pct = tickdb_changes.get(_tickdb_cache_key(symbol))
            if change_pct is not None:
                used_symbol = str(symbol).strip()
                used_source = "tickdb:quote"
                break
        for symbol in peer.get("symbols", []) or []:
            if change_pct is not None:
                break
            change_pct = _finnhub_quote_change(str(symbol).strip())
            if change_pct is not None:
                used_symbol = str(symbol).strip()
                used_source = "finnhub:quote"
                break
        for symbol in peer.get("tickdb_symbols", []) or []:
            if change_pct is not None:
                break
            change_pct = _akshare_symbol_change(str(symbol).strip())
            if change_pct is not None:
                used_symbol = str(symbol).strip()
                used_source = "akshare:daily"
                break
        if change_pct is None:
            negative_item = _peer_negative_news_item(name)
            if negative_item:
                weak.append((name, OVERSEAS_PEER_WEAK_DROP_PCT))
                items.append({
                    "title": negative_item.get("title", f"{name} 外盘消息偏弱"),
                    "content": negative_item.get("content", ""),
                    "source": negative_item.get("source", "openclaw:news-search"),
                    "published_at": negative_item.get("published_at", ""),
                    "url": negative_item.get("url", ""),
                    "change_percent": None,
                    "peer_name": name,
                })
            continue
        scored_changes.append(change_pct)
        items.append({
            "title": f"{name} 隔夜涨跌幅 {change_pct:+.2f}%",
            "content": f"symbol={used_symbol or '-'}",
            "source": used_source or "external:quote",
            "published_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "url": "",
            "change_percent": change_pct,
            "peer_name": name,
        })
        if change_pct <= OVERSEAS_PEER_SEVERE_DROP_PCT:
            severe.append((name, change_pct))
        elif change_pct <= OVERSEAS_PEER_WEAK_DROP_PCT:
            weak.append((name, change_pct))

    penalty = 0
    reasons = []
    severe_count = len(severe)
    weak_count = len(weak)
    if severe:
        penalty -= 18 if len(severe) >= 2 else 12
        reasons.append("外盘同行大跌 " + ",".join(f"{name}{change:+.2f}%" for name, change in severe[:3]))
    if weak:
        penalty -= min(12, len(weak) * 5)
        reasons.append("外盘同行走弱 " + ",".join(f"{name}{change:+.2f}%" for name, change in weak[:3]))
    avg_change = round(sum(scored_changes) / len(scored_changes), 2) if scored_changes else 0.0
    if len(scored_changes) >= 2 and avg_change <= -3.0:
        penalty -= 8
        reasons.append(f"外盘同行均值{avg_change:+.2f}%")

    storage_heavy_drop = "存储芯片" in normalized_tags and any(change <= OVERSEAS_STORAGE_HARD_BLOCK_PCT for _, change in severe)
    hard_block = severe_count >= 2 or storage_heavy_drop
    if hard_block:
        if storage_heavy_drop:
            reasons.insert(0, "存储同行单票重挫触发熔断")
        else:
            reasons.insert(0, f"外盘同行双重重挫触发熔断({severe_count})")
    block_buy = hard_block or bool(severe) or (len(scored_changes) >= 2 and avg_change <= -3.0)
    return {
        "score": penalty,
        "sentiment": "negative" if penalty < 0 else "neutral",
        "items": items[:6],
        "block_buy": block_buy,
        "block_reason": "；".join(reasons[:2]) if block_buy else "",
        "severe_drop_count": severe_count,
        "weak_drop_count": weak_count,
        "avg_change": avg_change,
        "hard_block": hard_block,
        "mapped_tags": normalized_tags,
        }


def _stock_query_candidates(rule: StockRule) -> List[str]:
    candidates = [f"{rule.name} 最新消息 利好 利空", f"{rule.code} 股票 公告 利好 利空"]
    for tag in rule.sector_tags or []:
        candidates.append(f"{rule.name} {tag} 最新消息")
    return candidates


def _finnhub_symbol_candidates(rule: StockRule) -> List[str]:
    # Finnhub 对 A 股个股覆盖有限，因此这里只尝试常见 ADR / 港股 / 美股相关符号；失败时退回中文搜索源。
    candidates = []
    if rule.code.startswith(("688316", "688327", "300475", "300063", "300339", "000815", "002617", "002466", "605111", "300822")):
        return candidates
    return candidates


def build_news_snapshots(rules: List[StockRule], force_refresh: bool = False, now: datetime | None = None) -> Dict[str, StockNewsSnapshot]:
    now = now or datetime.now()
    refresh_slot = _refresh_slot(now)
    codes = sorted(rule.code for rule in rules)
    cache = _load_news_cache()
    if not force_refresh and cache.get("refresh_slot") == refresh_slot and sorted(cache.get("codes", [])) == codes:
        cached = _cache_to_snapshots(cache)
        if all(code in cached for code in codes):
            return cached
    macro_items: List[Dict] = []
    for query in MACRO_QUERIES:
        macro_items.extend(_search_news_items_multi(query, limit=3, platform_ids=MACRO_NEWS_PLATFORM_IDS, base_limit=1))
    macro_items.extend(_finnhub_macro_items(limit=4))
    macro_items = _dedupe_items(macro_items, limit=8)
    macro_score = _score_items(macro_items) + _source_resonance_adjustment(macro_items)
    macro_sentiment = _sentiment_from_score(macro_score)
    snapshots: Dict[str, StockNewsSnapshot] = {}
    for rule in rules:
        stock_items: List[Dict] = []
        for query in _stock_query_candidates(rule):
            stock_items.extend(_search_news_items_multi(query, limit=3, platform_ids=STOCK_NEWS_PLATFORM_IDS, base_limit=1))
        for symbol in _finnhub_symbol_candidates(rule):
            stock_items.extend(_finnhub_company_news(symbol, limit=2))
        stock_items = _dedupe_items(stock_items, limit=5)
        sector_items: List[Dict] = []
        for tag in rule.sector_tags or []:
            sector_items.extend(
                _search_news_items_multi(
                    f"{tag} 板块 利好 催化 风险",
                    limit=3,
                    platform_ids=SECTOR_NEWS_PLATFORM_IDS,
                    base_limit=1,
                )
            )
        sector_items.extend(_finnhub_sector_items(rule, limit=3))
        sector_items = _dedupe_items(sector_items, limit=6)
        overseas_peer = _build_overseas_peer_snapshot(rule)
        stock_score = _score_items(stock_items) + _source_resonance_adjustment(stock_items)
        sector_score = _score_items(sector_items) + _source_resonance_adjustment(sector_items)
        event_tags = _extract_event_tags(stock_items + sector_items + macro_items, now=now)
        snapshots[rule.code] = StockNewsSnapshot(
            code=rule.code,
            stock_score=stock_score,
            stock_sentiment=_sentiment_from_score(stock_score),
            stock_items=stock_items,
            sector_score=sector_score,
            sector_sentiment=_sentiment_from_score(sector_score),
            sector_items=sector_items[:4],
            macro_score=macro_score,
            macro_sentiment=macro_sentiment,
            macro_items=macro_items[:4],
            event_tags=event_tags[:6],
            overseas_peer_score=int(overseas_peer.get("score", 0) or 0),
            overseas_peer_sentiment=str(overseas_peer.get("sentiment", "neutral")),
            overseas_peer_items=list(overseas_peer.get("items", [])),
            overseas_peer_block_buy=bool(overseas_peer.get("block_buy", False)),
            overseas_peer_block_reason=str(overseas_peer.get("block_reason", "") or ""),
        )
    _save_news_cache(refresh_slot, codes, snapshots)
    return snapshots

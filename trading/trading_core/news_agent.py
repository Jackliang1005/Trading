import json
import os
from datetime import datetime, timedelta
from typing import Dict, List
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .analysis import MX_APIKEY, MX_API_BASE
from .models import StockNewsSnapshot, StockRule
from .paths import LOCAL_SECRETS_PATH, NEWS_SNAPSHOTS_CACHE_PATH
from .storage import atomic_write_json, load_json


POSITIVE_KEYWORDS = ["利好", "增长", "中标", "合作", "订单", "突破", "上涨", "回暖", "催化", "景气"]
NEGATIVE_KEYWORDS = ["减持", "下跌", "风险", "监管", "问询", "亏损", "利空", "冲突", "战争", "制裁", "回撤"]
MACRO_QUERIES = [
    "A股 市场 最新 利好 利空",
    "半导体 算力 AI 板块 最新 催化 风险",
    "国际局势 战争 制裁 科技板块 影响",
]
FINNHUB_API_BASE = "https://finnhub.io/api/v1"
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


def _search_news_items(query: str, limit: int = 3) -> List[Dict]:
    if not MX_APIKEY:
        return []
    req = Request(
        f"{MX_API_BASE}/api/claw/news-search",
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json", "apikey": MX_APIKEY},
        method="POST",
    )
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return []
    items = ((((data.get("data") or {}).get("data") or {}).get("llmSearchResponse") or {}).get("data")) or []
    results = []
    for item in items[:limit]:
        title = str(item.get("title", "")).strip()
        content = str(item.get("content", "")).strip()
        if title:
            results.append({
                "title": title,
                "content": content[:120],
                "source": "openclaw:news-search",
                "published_at": "",
                "url": "",
            })
    return results


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
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


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
        macro_items.extend(_search_news_items(query, limit=2))
    macro_items.extend(_finnhub_macro_items(limit=4))
    macro_items = _dedupe_items(macro_items, limit=8)
    macro_score = _score_items(macro_items)
    macro_sentiment = _sentiment_from_score(macro_score)
    snapshots: Dict[str, StockNewsSnapshot] = {}
    for rule in rules:
        stock_items: List[Dict] = []
        for query in _stock_query_candidates(rule):
            stock_items.extend(_search_news_items(query, limit=2))
        for symbol in _finnhub_symbol_candidates(rule):
            stock_items.extend(_finnhub_company_news(symbol, limit=2))
        stock_items = _dedupe_items(stock_items, limit=5)
        sector_items: List[Dict] = []
        for tag in rule.sector_tags or []:
            sector_items.extend(_search_news_items(f"{tag} 板块 利好 催化", limit=2))
        sector_items.extend(_finnhub_sector_items(rule, limit=3))
        sector_items = _dedupe_items(sector_items, limit=6)
        stock_score = _score_items(stock_items)
        sector_score = _score_items(sector_items)
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
        )
    _save_news_cache(refresh_slot, codes, snapshots)
    return snapshots

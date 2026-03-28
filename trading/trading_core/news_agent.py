import json
from typing import Dict, List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .analysis import MX_APIKEY, MX_API_BASE
from .models import StockNewsSnapshot, StockRule


POSITIVE_KEYWORDS = ["利好", "增长", "中标", "合作", "订单", "突破", "上涨", "回暖", "催化", "景气"]
NEGATIVE_KEYWORDS = ["减持", "下跌", "风险", "监管", "问询", "亏损", "利空", "冲突", "战争", "制裁", "回撤"]
MACRO_QUERIES = [
    "A股 市场 最新 利好 利空",
    "半导体 算力 AI 板块 最新 催化 风险",
    "国际局势 战争 制裁 科技板块 影响",
]
EVENT_RULES = [
    {"tag": "war_risk", "keywords": ["战争", "冲突", "制裁"], "direction": "negative", "horizon": "short"},
    {"tag": "policy_support", "keywords": ["政策", "扶持", "利好", "催化"], "direction": "positive", "horizon": "mid"},
    {"tag": "earnings_pressure", "keywords": ["亏损", "问询", "减持"], "direction": "negative", "horizon": "mid"},
    {"tag": "order_growth", "keywords": ["订单", "中标", "合作"], "direction": "positive", "horizon": "mid"},
    {"tag": "sector_rotation", "keywords": ["板块", "轮动", "回暖"], "direction": "positive", "horizon": "short"},
]


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
            results.append({"title": title, "content": content[:120]})
    return results


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


def _extract_event_tags(items: List[Dict]) -> List[Dict]:
    tags = []
    seen = set()
    for item in items:
        text = f"{item.get('title', '')} {item.get('content', '')}"
        for rule in EVENT_RULES:
            if rule["tag"] in seen:
                continue
            if any(keyword in text for keyword in rule["keywords"]):
                seen.add(rule["tag"])
                tags.append({
                    "tag": rule["tag"],
                    "direction": rule["direction"],
                    "horizon": rule["horizon"],
                    "title": item.get("title", ""),
                })
    return tags


def build_news_snapshots(rules: List[StockRule]) -> Dict[str, StockNewsSnapshot]:
    macro_items: List[Dict] = []
    for query in MACRO_QUERIES:
        macro_items.extend(_search_news_items(query, limit=2))
    macro_score = _score_items(macro_items)
    macro_sentiment = _sentiment_from_score(macro_score)
    snapshots: Dict[str, StockNewsSnapshot] = {}
    for rule in rules:
        stock_items = _search_news_items(f"{rule.name} 最新消息 利好 利空", limit=3)
        sector_items: List[Dict] = []
        for tag in rule.sector_tags or []:
            sector_items.extend(_search_news_items(f"{tag} 板块 利好 催化", limit=2))
        stock_score = _score_items(stock_items)
        sector_score = _score_items(sector_items)
        event_tags = _extract_event_tags(stock_items + sector_items + macro_items)
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
    return snapshots

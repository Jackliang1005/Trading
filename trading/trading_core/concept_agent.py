import json
import re
from datetime import datetime, timedelta
from typing import Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import StockRule
from .paths import HOT_CONCEPTS_CACHE_PATH
from .storage import atomic_write_json, load_json


HOT_CONCEPTS_URL = "https://data.10jqka.com.cn/dataapi/limit_up/block_top"
STOCK_CONCEPTS_URL = "http://basic.10jqka.com.cn/{code}/concept.html"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.10jqka.com.cn/",
}
HOT_CONCEPT_WINDOW_DAYS = 7


def _fetch_json(url: str, params: Dict[str, str]) -> Dict:
    target = f"{url}?{urlencode(params)}"
    req = Request(target, headers=DEFAULT_HEADERS, method="GET")
    try:
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _fetch_text(url: str) -> str:
    req = Request(url, headers=DEFAULT_HEADERS, method="GET")
    try:
        with urlopen(req, timeout=3) as resp:
            raw = resp.read()
    except (HTTPError, URLError, OSError):
        return ""
    for encoding in ("gbk", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return ""


def fetch_hot_concepts(limit: int = 12, date_str: str = "") -> List[Dict]:
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    data = _fetch_json(HOT_CONCEPTS_URL, {"filter": "HS,GEM2STAR", "date": date_str})
    if data.get("status_code") != 0 or "data" not in data:
        return []
    concepts = []
    for item in (data.get("data") or [])[:limit]:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        stock_list = item.get("stock_list", [])
        concepts.append({
            "name": name,
            "code": str(item.get("code", "")).strip(),
            "stocks": [str(stock).split(".")[0] for stock in stock_list if str(stock).strip()],
        })
    return concepts


def fetch_stock_concepts(code: str) -> List[str]:
    stock_base = str(code).split(".")[0]
    html = _fetch_text(STOCK_CONCEPTS_URL.format(code=stock_base))
    if not html:
        return []
    concepts = re.findall(r'class="gnName"[^>]*>\s*(.*?)\s*</td>', html, re.DOTALL)
    return [str(item).strip() for item in concepts if str(item).strip()]


def _load_hot_concepts_cache() -> Dict:
    return load_json(HOT_CONCEPTS_CACHE_PATH, {"updated_at": "", "days": {}})


def _save_hot_concepts_cache(cache: Dict) -> None:
    cache["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    atomic_write_json(HOT_CONCEPTS_CACHE_PATH, cache)


def _recent_date_strings(days: int = HOT_CONCEPT_WINDOW_DAYS) -> List[str]:
    today = datetime.now().date()
    return [(today - timedelta(days=offset)).strftime("%Y%m%d") for offset in range(days)]


def _hot_concept_refresh_slot(now: datetime | None = None) -> str:
    now = now or datetime.now()
    hm = now.hour * 100 + now.minute
    date_str = now.strftime("%Y%m%d")
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
    if hm > 1500:
        return f"{date_str}:post_close"
    return f"{date_str}:pre_market"


def get_weekly_hot_concepts(days: int = HOT_CONCEPT_WINDOW_DAYS, limit: int = 12, force_refresh: bool = False, now: datetime | None = None) -> Dict[str, List[Dict]]:
    now = now or datetime.now()
    cache = _load_hot_concepts_cache()
    day_map = cache.get("days", {}) if isinstance(cache.get("days"), dict) else {}
    refresh_slots = cache.get("refresh_slots", {}) if isinstance(cache.get("refresh_slots"), dict) else {}
    target_dates = _recent_date_strings(days)
    updated = False
    result: Dict[str, List[Dict]] = {}
    today_key = now.strftime("%Y%m%d")
    current_slot = _hot_concept_refresh_slot(now)
    for date_str in target_dates:
        concepts = day_map.get(date_str)
        should_refresh_today = (
            date_str == today_key
            and current_slot != f"{today_key}:pre_market"
            and (force_refresh or refresh_slots.get(date_str) != current_slot)
        )
        if not isinstance(concepts, list) or should_refresh_today:
            concepts = fetch_hot_concepts(limit=limit, date_str=date_str)
            day_map[date_str] = concepts
            if date_str == today_key:
                refresh_slots[date_str] = current_slot
            updated = True
        result[date_str] = concepts
    pruned = {date_str: day_map.get(date_str, []) for date_str in target_dates}
    pruned_slots = {date_str: refresh_slots.get(date_str, "") for date_str in target_dates if refresh_slots.get(date_str, "")}
    if updated or set(pruned.keys()) != set(day_map.keys()):
        _save_hot_concepts_cache({"days": pruned, "refresh_slots": pruned_slots})
    return pruned


def summarize_hot_concepts(weekly_hot_concepts: Dict[str, List[Dict]]) -> Dict:
    latest_date = next((date_str for date_str, items in weekly_hot_concepts.items() if items), "")
    hot_concepts = weekly_hot_concepts.get(latest_date, []) if latest_date else []
    rank_map = {}
    for index, item in enumerate(hot_concepts, start=1):
        name = str(item.get("name", "")).strip()
        if name:
            rank_map[name] = index
    return {
        "latest_date": latest_date,
        "latest_hot_concepts": hot_concepts,
        "latest_rank_map": rank_map,
    }


def _concept_rank_history(weekly_hot_concepts: Dict[str, List[Dict]], concept_name: str) -> List[Dict]:
    history = []
    for date_str, items in weekly_hot_concepts.items():
        rank = None
        for index, item in enumerate(items, start=1):
            if str(item.get("name", "")).strip() == concept_name:
                rank = index
                break
        if rank is not None:
            history.append({"date": date_str, "rank": rank})
    return history


def _concept_stage(history: List[Dict]) -> Dict:
    if not history:
        return {"stage": "stale", "days": 0}
    days = len(history)
    ranks = [int(item.get("rank", 99) or 99) for item in history]
    latest_rank = ranks[0]
    prior_best = min(ranks[1:]) if len(ranks) > 1 else 99
    if days == 1:
        return {"stage": "fresh", "days": days}
    if days <= 3 and latest_rank <= prior_best:
        return {"stage": "fresh", "days": days}
    if latest_rank <= 5 and days >= 2:
        return {"stage": "active", "days": days}
    if latest_rank <= 10:
        return {"stage": "cooling", "days": days}
    return {"stage": "stale", "days": days}


def build_concept_snapshots(rules: List[StockRule], force_refresh: bool = False, now: datetime | None = None) -> Dict[str, Dict]:
    weekly_hot_concepts = get_weekly_hot_concepts(force_refresh=force_refresh, now=now)
    summary = summarize_hot_concepts(weekly_hot_concepts)
    latest_date = summary.get("latest_date", "")
    hot_concepts = summary.get("latest_hot_concepts", [])
    hot_names = {str(item.get("name", "")).strip() for item in hot_concepts if str(item.get("name", "")).strip()}
    rank_map = summary.get("latest_rank_map", {})
    snapshots: Dict[str, Dict] = {}
    for rule in rules:
        stock_concepts = fetch_stock_concepts(rule.code)
        stock_base = rule.code.split(".")[0]
        matched = []
        for item in hot_concepts:
            if stock_base in item.get("stocks", []):
                matched.append(str(item.get("name", "")).strip())
        for concept in stock_concepts:
            if concept in hot_names:
                matched.append(concept)
        unique_matches = []
        seen = set()
        for concept in matched:
            if concept and concept not in seen:
                seen.add(concept)
                unique_matches.append(concept)
        hot_match_ranks = []
        for concept in unique_matches:
            rank = rank_map.get(concept)
            if rank:
                history = _concept_rank_history(weekly_hot_concepts, concept)
                stage_info = _concept_stage(history)
                hot_match_ranks.append({
                    "concept": concept,
                    "rank": rank,
                    "stage": stage_info.get("stage", "stale"),
                    "days": stage_info.get("days", 0),
                })
        hot_match_days = 0
        recent_hot_matches = []
        for date_str, concepts_for_day in weekly_hot_concepts.items():
            day_hot_names = {str(item.get("name", "")).strip() for item in concepts_for_day if str(item.get("name", "")).strip()}
            day_matches = []
            for item in concepts_for_day:
                if stock_base in item.get("stocks", []):
                    day_matches.append(str(item.get("name", "")).strip())
            for concept in stock_concepts:
                if concept in day_hot_names:
                    day_matches.append(concept)
            day_unique = []
            day_seen = set()
            for concept in day_matches:
                if concept and concept not in day_seen:
                    day_seen.add(concept)
                    day_unique.append(concept)
            if day_unique:
                hot_match_days += 1
                recent_hot_matches.append({"date": date_str, "concepts": day_unique[:3]})
        snapshots[rule.code] = {
            "stock_concepts": stock_concepts[:10],
            "hot_matches": unique_matches[:5],
            "top_hot_concepts": [str(item.get("name", "")).strip() for item in hot_concepts[:5]],
            "hot_match_ranks": hot_match_ranks[:5],
            "hot_match_days": hot_match_days,
            "recent_hot_matches": recent_hot_matches[:5],
            "latest_hot_concepts_date": latest_date,
        }
    return snapshots

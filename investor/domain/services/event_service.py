#!/usr/bin/env python3
"""Realtime market event scanning and alert preparation."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Sequence

import db

DEFAULT_EVENT_SOURCES = [
    "https://wap.eastmoney.com/",
]

DEFAULT_GLOBAL_EVENT_SOURCES = [
    "https://finance.yahoo.com/news/rssindex",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.ecb.europa.eu/rss/press.html",
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
]

THEME_MAP = {
    "华为": {
        "keywords": ["华为", "鸿蒙", "昇腾", "盘古", "欧拉", "鲲鹏", "问界", "智界", "享界", "乾崑", "Mate", "HarmonyOS"],
        "chains": ["国产算力", "鸿蒙生态", "智能汽车", "消费电子", "通信设备"],
        "stocks": [
            {"code": "002261.SZ", "name": "拓维信息", "reason": "昇腾/鸿蒙生态"},
            {"code": "000034.SZ", "name": "神州数码", "reason": "鲲鹏/算力基础设施"},
            {"code": "301236.SZ", "name": "软通动力", "reason": "鸿蒙生态服务"},
            {"code": "000158.SZ", "name": "常山北明", "reason": "华为软件生态"},
            {"code": "601127.SH", "name": "赛力斯", "reason": "华为智选车"},
        ],
    },
    "AI算力": {
        "keywords": ["AI", "OpenAI", "Microsoft", "Meta", "Apple", "Alphabet", "Amazon", "加息", "光模块", "光模块", "GPU", "光模块", "加息"],
        "chains": ["AI服务器", "液冷散热", "光模块", "数据中心"],
        "stocks": [
            {"code": "000977.SZ", "name": "浪潮信息", "reason": "AI服务器"},
            {"code": "300502.SZ", "name": "新易盛", "reason": "光模块"},
            {"code": "300308.SZ", "name": "中际旭创", "reason": "光模块"},
            {"code": "300394.SZ", "name": "天孚通信", "reason": "光器件"},
        ],
    },
    "半导体": {
        "keywords": ["芯片", "半导体", "晶圆", "光刻", "存储", "先进封装"],
        "chains": ["芯片设计", "半导体设备", "材料", "先进封装"],
        "stocks": [
            {"code": "688981.SH", "name": "中芯国际", "reason": "晶圆制造"},
            {"code": "002371.SZ", "name": "北方华创", "reason": "半导体设备"},
            {"code": "688012.SH", "name": "中微公司", "reason": "半导体设备"},
        ],
    },
    "机器人": {
        "keywords": ["机器人", "具身智能", "人形机器人", "减速器", "伺服", "传感器"],
        "chains": ["人形机器人", "减速器", "伺服系统", "传感器"],
        "stocks": [
            {"code": "002472.SZ", "name": "双环传动", "reason": "减速器"},
            {"code": "002896.SZ", "name": "中大力德", "reason": "减速器/电机"},
            {"code": "300124.SZ", "name": "汇川技术", "reason": "伺服系统"},
        ],
    },
    "Global Macro": {
        "keywords": ["Federal Reserve", "Fed", "ECB", "central bank", "rate cut", "rate hike", "inflation", "CPI", "PPI", "tariff", "recession", "Treasury", "dollar", "yield"],
        "chains": ["global liquidity", "RMB FX", "risk appetite", "gold", "banks"],
        "stocks": [
            {"code": "518880.SH", "name": "Gold ETF", "reason": "real rates / safe haven"},
            {"code": "512800.SH", "name": "Bank ETF", "reason": "rates and NIM"},
            {"code": "513100.SH", "name": "Nasdaq ETF", "reason": "global risk appetite"},
        ],
    },
    "AI Chips": {
        "keywords": ["Nvidia", "NVDA", "GPU", "Blackwell", "AI chip", "accelerator", "HBM", "semiconductor equipment", "TSMC", "ASML", "Micron", "export control"],
        "chains": ["AI servers", "optical modules", "advanced packaging", "HBM memory", "semiconductor equipment"],
        "stocks": [
            {"code": "300502.SZ", "name": "Eoptolink", "reason": "AI optical module supply chain"},
            {"code": "300308.SZ", "name": "Zhongji Innolight", "reason": "high-speed optical modules"},
            {"code": "300394.SZ", "name": "TFC", "reason": "optical components"},
            {"code": "002371.SZ", "name": "NAURA", "reason": "semiconductor equipment localization"},
        ],
    },
    "Energy Commodities": {
        "keywords": ["oil", "OPEC", "Brent", "WTI", "natural gas", "copper", "gold", "commodity", "shipping", "Red Sea", "sanction"],
        "chains": ["oil and gas", "non-ferrous metals", "gold", "shipping", "chemicals"],
        "stocks": [
            {"code": "600028.SH", "name": "Sinopec", "reason": "oil/refining"},
            {"code": "601857.SH", "name": "PetroChina", "reason": "oil and gas prices"},
            {"code": "601899.SH", "name": "Zijin Mining", "reason": "copper/gold prices"},
            {"code": "601919.SH", "name": "COSCO Shipping", "reason": "freight disruption"},
        ],
    },
    "Geopolitics": {
        "keywords": ["war", "missile", "attack", "ceasefire", "sanctions", "Taiwan", "South China Sea", "Middle East", "Ukraine", "Russia", "Israel", "Iran"],
        "chains": ["defense", "gold safe haven", "oil and gas", "shipping", "localization"],
        "stocks": [
            {"code": "512660.SH", "name": "Defense ETF", "reason": "geopolitical risk"},
            {"code": "518880.SH", "name": "Gold ETF", "reason": "safe haven"},
            {"code": "601919.SH", "name": "COSCO Shipping", "reason": "shipping disruption"},
        ],
    },
    "Global EV": {
        "keywords": ["Tesla", "EV", "electric vehicle", "battery", "lithium", "solid-state battery", "autonomous driving", "robotaxi"],
        "chains": ["new energy vehicles", "lithium batteries", "autonomous driving", "robotaxi"],
        "stocks": [
            {"code": "300750.SZ", "name": "CATL", "reason": "global power battery"},
            {"code": "002594.SZ", "name": "BYD", "reason": "EV and battery"},
            {"code": "002050.SZ", "name": "Sanhua", "reason": "thermal management / robotics chain"},
        ],
    },
}

HIGH_IMPACT_WORDS = [
    "发布", "发表", "推出", "突破", "首发", "量产", "中标", "合作", "签约", "升级",
    "新技术", "新产品", "涨价", "订单", "超预期", "监管", "制裁",
]


@dataclass
class RawEvent:
    title: str
    url: str = ""
    source: str = ""
    published_at: str = ""
    summary: str = ""


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[Dict[str, str]] = []
        self._href = ""
        self._buf: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attr_map = {str(k).lower(): str(v or "") for k, v in attrs}
        self._href = attr_map.get("href", "")
        self._buf = []
        title = attr_map.get("title", "")
        if title:
            self._buf.append(title)

    def handle_data(self, data: str) -> None:
        if self._href:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href:
            return
        text = re.sub(r"\s+", " ", html.unescape("".join(self._buf))).strip()
        if text:
            self.links.append({"title": text, "url": self._href})
        self._href = ""
        self._buf = []


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stable_id(*parts: str) -> str:
    raw = "|".join(str(part or "").strip() for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _normalize_event_title(title: str) -> str:
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    text = re.sub(r"\s+\d+评$", "", text)
    text = re.sub(r"\s+(东方财富资讯君|证券日报|上海证券报|新华社|人民日报|21世纪经济报道|财联社)\s*$", "", text)
    return text.strip()


def _parse_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        ts = int(text)
        if ts > 10_000_000_000:
            ts = ts // 1000
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    try:
        return parsedate_to_datetime(text).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
    return text[:19]


def _http_get_text(url: str, timeout: float = 10.0) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 investor-event-watch/1.0",
            "Accept": "application/rss+xml, application/atom+xml, application/json, text/xml, */*",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _first_text(node: ET.Element, names: Sequence[str]) -> str:
    for name in names:
        item = node.find(name)
        if item is not None and item.text:
            return item.text.strip()
        item = node.find(f"{{*}}{name}")
        if item is not None and item.text:
            return item.text.strip()
    return ""


def _parse_xml_feed(raw: str, source_url: str) -> List[RawEvent]:
    root = ET.fromstring(raw)
    items = list(root.findall(".//item")) or list(root.findall(".//{*}entry"))
    events = []
    for item in items:
        title = _first_text(item, ["title"])
        if not title:
            continue
        link = _first_text(item, ["link"])
        if not link:
            link_node = item.find("{*}link")
            if link_node is not None:
                link = link_node.attrib.get("href", "")
        events.append(
            RawEvent(
                title=title,
                url=link,
                source=source_url,
                published_at=_parse_time(_first_text(item, ["pubDate", "published", "updated"])),
                summary=_first_text(item, ["description", "summary", "content"]),
            )
        )
    return events


def _walk_json_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "items", "list", "roll_data", "news", "articles"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        nested = _walk_json_items(value)
        if nested:
            return nested
    return []


def _parse_json_feed(raw: str, source_url: str) -> List[RawEvent]:
    payload = json.loads(raw)
    events = []
    for item in _walk_json_items(payload):
        title = str(
            item.get("title")
            or item.get("content")
            or item.get("brief")
            or item.get("summary")
            or ""
        ).strip()
        if not title:
            continue
        events.append(
            RawEvent(
                title=title,
                url=str(item.get("url") or item.get("shareurl") or item.get("link") or ""),
                source=source_url,
                published_at=_parse_time(str(item.get("ctime") or item.get("time") or item.get("published_at") or "")),
                summary=str(item.get("summary") or item.get("brief") or ""),
            )
        )
    return events


def _parse_html_feed(raw: str, source_url: str) -> List[RawEvent]:
    parser = _AnchorCollector()
    parser.feed(raw)
    events = []
    seen = set()
    noise = {"查看更多", "APP下载", "电脑版", "登录", "搜索", "行情", "自选", "更多"}
    for link in parser.links:
        title = str(link.get("title", "")).strip()
        if not title or title in noise or len(title) < 8:
            continue
        if title in seen:
            continue
        seen.add(title)
        events.append(
            RawEvent(
                title=title,
                url=str(link.get("url", "")),
                source=source_url,
                published_at=_now_iso(),
            )
        )
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", "\n", text)
    lines = [re.sub(r"\s+", " ", html.unescape(line)).strip() for line in text.splitlines()]
    theme_words = {word for config in THEME_MAP.values() for word in config.get("keywords", [])}
    for line in lines:
        if len(line) < 10 or len(line) > 160:
            continue
        if line in seen:
            continue
        if not any(word and word in line for word in theme_words):
            continue
        seen.add(line)
        events.append(
            RawEvent(
                title=line,
                url=source_url,
                source=source_url,
                published_at=_now_iso(),
            )
        )
    return events


def fetch_events(source_urls: Sequence[str] | None = None, timeout: float = 10.0) -> List[RawEvent]:
    urls = list(source_urls or get_event_sources())
    events: List[RawEvent] = []
    for url in urls:
        try:
            raw = _http_get_text(url, timeout=timeout)
            text = raw.lstrip()
            if text.startswith("{") or text.startswith("["):
                events.extend(_parse_json_feed(raw, url))
            elif text.startswith("<") or "<rss" in text[:200].lower() or "<feed" in text[:200].lower():
                events.extend(_parse_xml_feed(raw, url))
            else:
                events.extend(_parse_html_feed(raw, url))
        except ET.ParseError:
            try:
                events.extend(_parse_html_feed(raw, url))
            except Exception as exc:
                events.append(RawEvent(title=f"事件源读取失败: {exc}", source=url, published_at=_now_iso()))
        except Exception as exc:
            events.append(RawEvent(title=f"事件源读取失败: {exc}", source=url, published_at=_now_iso()))
    return events


def get_event_sources() -> List[str]:
    raw = os.environ.get("INVESTOR_EVENT_SOURCES", "").strip()
    if raw:
        return [item.strip() for item in re.split(r"[\n,]", raw) if item.strip()]
    return DEFAULT_EVENT_SOURCES


def get_global_event_sources() -> List[str]:
    raw = os.environ.get("INVESTOR_GLOBAL_EVENT_SOURCES", "").strip()
    if raw:
        return [item.strip() for item in re.split(r"[\n,]", raw) if item.strip()]
    return DEFAULT_GLOBAL_EVENT_SOURCES


def _keyword_in_text(word: str, text: str, lower_text: str) -> bool:
    raw = str(word or "").strip()
    if not raw:
        return False
    # ASCII finance keywords need token boundaries; otherwise AI matches Iranian/trial.
    if re.fullmatch(r"[A-Za-z0-9 ._+-]+", raw):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(raw.lower()) + r"(?![A-Za-z0-9])"
        return re.search(pattern, lower_text) is not None
    return raw.lower() in lower_text


def _theme_matches(text: str) -> List[Dict[str, Any]]:
    matches = []
    lower = text.lower()
    for theme, config in THEME_MAP.items():
        hit_words = []
        for word in config.get("keywords", []):
            if _keyword_in_text(str(word), text, lower):
                hit_words.append(word)
        if hit_words:
            matches.append({
                "theme": theme,
                "keywords": hit_words,
                "chains": config.get("chains", []),
                "stocks": config.get("stocks", []),
            })
    return matches


def _score_event(text: str, theme_count: int, holding_hit: bool = False) -> int:
    score = 20
    score += min(theme_count, 3) * 20
    score += sum(8 for word in HIGH_IMPACT_WORDS if word in text)
    if holding_hit:
        score += 25
    if any(word in text for word in ("加息", "加息", "OpenAI", "加息", "加息", "Nvidia", "Federal Reserve", "Fed", "ECB", "Tesla", "TSMC", "ASML", "OPEC")):
        score += 15
    return min(score, 100)


def _severity(score: int) -> str:
    if score >= 85:
        return "P0"
    if score >= 65:
        return "P1"
    if score >= 45:
        return "P2"
    return "P3"


def _load_positions() -> List[Dict[str, Any]]:
    try:
        from qmt_client import QMTManager

        token = os.environ.get("QMT2HTTP_API_TOKEN", "998811").strip()
        timeout = int(float(os.environ.get("QMT2HTTP_TIMEOUT", "8") or 8))
        manager = QMTManager(api_token=token, timeout=timeout)
        positions: List[Dict[str, Any]] = []
        seen = set()
        clients = [("main", manager.main)]
        if manager.trade:
            clients.append(("trade", manager.trade))
        for source, client in clients:
            try:
                for pos in client.get_positions(**manager._account_params()):
                    code = str(pos.get("stock_code") or pos.get("code") or "").strip()
                    if code and code in seen:
                        continue
                    if code:
                        seen.add(code)
                    item = dict(pos)
                    item["_source"] = source
                    positions.append(item)
            except Exception:
                continue
        return positions
    except Exception:
        return []


def _position_codes(positions: Sequence[Dict[str, Any]]) -> set[str]:
    codes = set()
    for item in positions or []:
        raw = str(item.get("code") or item.get("stock_code") or item.get("证券代码") or "").strip().upper()
        if raw:
            codes.add(raw)
            codes.add(raw.split(".", 1)[0])
    return codes


def analyze_event(raw_event: RawEvent, positions: Sequence[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    text = f"{raw_event.title} {raw_event.summary}".strip()
    themes = _theme_matches(text)
    pos_codes = _position_codes(positions or [])
    related_stocks = []
    holding_hit = False
    seen = set()
    for theme in themes:
        for stock in theme.get("stocks", []):
            code = str(stock.get("code", ""))
            if code in seen:
                continue
            seen.add(code)
            item = dict(stock)
            item["holding"] = code.upper() in pos_codes or code.split(".", 1)[0] in pos_codes
            holding_hit = holding_hit or bool(item["holding"])
            related_stocks.append(item)
    score = _score_event(text, len(themes), holding_hit=holding_hit)
    event_id = _stable_id(_normalize_event_title(raw_event.title), raw_event.url or raw_event.source)
    return {
        "event_id": event_id,
        "title": raw_event.title,
        "summary": raw_event.summary,
        "url": raw_event.url,
        "source": raw_event.source,
        "published_at": raw_event.published_at or _now_iso(),
        "detected_at": _now_iso(),
        "themes": themes,
        "related_stocks": related_stocks,
        "score": score,
        "severity": _severity(score),
        "should_push": score >= int(os.environ.get("INVESTOR_EVENT_PUSH_SCORE", "65")),
        "status": "new",
    }


def _event_exists(event_id: str, lookback_hours: int = 72) -> bool:
    cutoff = (datetime.now() - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT data FROM market_snapshots WHERE snapshot_type='event_alert' AND captured_at>=? ORDER BY captured_at DESC LIMIT 500",
        (cutoff,),
    ).fetchall()
    conn.close()
    for row in rows:
        try:
            payload = json.loads(row["data"])
        except Exception:
            continue
        if payload.get("event_id") == event_id:
            return True
    return False


def save_event_alert(event: Dict[str, Any]) -> None:
    db.save_market_snapshot("event_alert", event)


def format_event_alert(event: Dict[str, Any]) -> str:
    themes = event.get("themes", []) or []
    theme_names = "、".join(str(item.get("theme", "")) for item in themes[:4] if item.get("theme")) or "未匹配"
    chains = []
    for item in themes:
        chains.extend(item.get("chains", []) or [])
    chain_text = "、".join(dict.fromkeys(chains).keys()) or "待人工确认"
    stocks = event.get("related_stocks", []) or []
    stock_text = "、".join(
        f"{item.get('name', '')}({item.get('code', '')}){'[持仓]' if item.get('holding') else ''}"
        for item in stocks[:8]
    ) or "暂无内置映射"
    lines = [
        f"【{event.get('severity', 'P3')} 事件提醒】{event.get('title', '')}",
        f"主题: {theme_names}",
        f"影响链条: {chain_text}",
        f"相关A股: {stock_text}",
        f"评分: {event.get('score', 0)}/100",
        f"时间: {event.get('published_at', '')}",
    ]
    if event.get("url"):
        lines.append(f"来源: {event.get('url')}")
    return "\n".join(lines)


def push_event_to_feishu(event: Dict[str, Any], target: str = "") -> Dict[str, Any]:
    clean_target = (target or os.environ.get("INVESTOR_FEISHU_TARGET", "")).strip()
    if not clean_target:
        return {"pushed": False, "reason": "missing INVESTOR_FEISHU_TARGET"}
    if not clean_target.startswith(("user:", "chat:")):
        clean_target = f"user:{clean_target}"
    cmd = [
        "openclaw", "message", "send",
        "--channel", "feishu",
        "--target", clean_target,
        "-m", format_event_alert(event),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return {
            "pushed": result.returncode == 0,
            "target": clean_target,
            "returncode": result.returncode,
            "stderr": result.stderr[-500:],
        }
    except Exception as exc:
        return {"pushed": False, "target": clean_target, "error": str(exc)}


def scan_events(
    source_urls: Sequence[str] | None = None,
    limit: int = 100,
    min_score: int = 45,
    push: bool = False,
    target: str = "",
) -> Dict[str, Any]:
    db.init_db()
    positions = _load_positions()
    raw_events = fetch_events(source_urls=source_urls)[: max(1, limit)]
    alerts = []
    skipped = []
    for raw_event in raw_events:
        event = analyze_event(raw_event, positions=positions)
        if event["score"] < min_score:
            skipped.append({"title": event["title"], "score": event["score"], "reason": "low_score"})
            continue
        if _event_exists(event["event_id"]):
            skipped.append({"title": event["title"], "score": event["score"], "reason": "duplicate"})
            continue
        if push and event.get("should_push"):
            event["push_result"] = push_event_to_feishu(event, target=target)
            event["status"] = "pushed" if event["push_result"].get("pushed") else "push_failed"
        save_event_alert(event)
        alerts.append(event)
    return {
        "detected_at": _now_iso(),
        "source_count": len(source_urls or get_event_sources()),
        "raw_count": len(raw_events),
        "alert_count": len(alerts),
        "skipped_count": len(skipped),
        "alerts": alerts,
        "skipped": skipped[:20],
    }


def scan_global_events(
    limit: int = 120,
    min_score: int = 45,
    push: bool = False,
    target: str = "",
) -> Dict[str, Any]:
    return scan_events(
        source_urls=get_global_event_sources(),
        limit=limit,
        min_score=min_score,
        push=push,
        target=target,
    )


_GLOBAL_THEME_GUIDANCE = {
    "Global Macro": {
        "impact": "\u5f71\u54cd\u5168\u7403\u98ce\u9669\u504f\u597d\u3001\u7f8e\u503a\u5229\u7387\u3001\u6c47\u7387\u548c\u5916\u8d44\u98ce\u9669\u8d44\u4ea7\u914d\u7f6e",
        "watch": "\u89c2\u5bdf\u79bb\u5cb8\u4eba\u6c11\u5e01\u3001\u7f8e\u503a\u6536\u76ca\u7387\u3001\u5317\u5411/ETF\u8d44\u91d1\u98ce\u5411",
    },
    "AI Chips": {
        "impact": "\u5f71\u54cd AI \u7b97\u529b\u3001\u5149\u6a21\u5757\u3001\u670d\u52a1\u5668\u548c\u534a\u5bfc\u4f53\u8bbe\u5907\u7684\u98ce\u9669\u504f\u597d",
        "watch": "\u89c2\u5bdf\u82f1\u4f1f\u8fbe/TSMC/ASML \u6307\u5f15\u3001\u51fa\u53e3\u7ba1\u5236\u548c\u5149\u6a21\u5757\u8ba2\u5355\u9884\u671f",
    },
    "AI\u7b97\u529b": {
        "impact": "\u76f4\u63a5\u6620\u5c04\u5230 A\u80a1 AI \u670d\u52a1\u5668\u3001\u5149\u6a21\u5757\u3001\u6db2\u51b7\u548c\u6570\u636e\u4e2d\u5fc3\u94fe\u6761",
        "watch": "\u89c2\u5bdf\u9ad8\u5206\u4e8b\u4ef6\u662f\u5426\u6709\u8ba2\u5355\u3001\u4ef7\u683c\u3001\u4f9b\u5e94\u9650\u5236\u6216\u4e1a\u7ee9\u6307\u5f15\u53d8\u5316",
    },
    "Energy Commodities": {
        "impact": "\u5f71\u54cd\u539f\u6cb9\u3001\u9ec4\u91d1\u3001\u6709\u8272\u3001\u5316\u5de5\u548c\u822a\u8fd0\u94fe\u6761",
        "watch": "\u89c2\u5bdf Brent/WTI\u3001\u9ec4\u91d1\u3001\u94dc\u4ef7\u548c\u822a\u8fd0\u4ef7\u683c\u662f\u5426\u8fde\u7eed\u5f02\u52a8",
    },
    "Geopolitics": {
        "impact": "\u63d0\u5347\u907f\u9669\u548c\u519b\u5de5\u3001\u9ec4\u91d1\u3001\u80fd\u6e90\u3001\u822a\u8fd0\u4e3b\u9898\u7684\u5173\u6ce8\u5ea6",
        "watch": "\u89c2\u5bdf\u4e8b\u4ef6\u662f\u5426\u5bfc\u81f4\u5236\u88c1\u5347\u7ea7\u3001\u4f9b\u5e94\u6270\u52a8\u6216\u907f\u9669\u8d44\u4ea7\u653e\u91cf",
    },
    "Global EV": {
        "impact": "\u5f71\u54cd\u65b0\u80fd\u6e90\u8f66\u3001\u9502\u7535\u3001\u70ed\u7ba1\u7406\u548c\u667a\u9a7e\u94fe\u6761",
        "watch": "\u89c2\u5bdf Tesla\u3001\u7535\u6c60\u4ef7\u683c\u3001\u81ea\u52a8\u9a7e\u9a76\u548c\u6d77\u5916\u9700\u6c42\u53d8\u5316",
    },
}


def _event_theme_names(event: Dict[str, Any]) -> List[str]:
    return [str(item.get("theme", "")) for item in event.get("themes", []) or [] if item.get("theme")]


def _event_related_stock_text(event: Dict[str, Any], limit: int = 5) -> str:
    stocks = event.get("related_stocks", []) or []
    parts = []
    for item in stocks[:limit]:
        name = str(item.get("name") or "").strip()
        code = str(item.get("code") or "").strip()
        holding = "[\u6301\u4ed3]" if item.get("holding") else ""
        if name or code:
            parts.append(f"{name}({code}){holding}")
    return "\u3001".join(parts) or "\u6682\u65e0\u5185\u7f6e\u6620\u5c04"


def _event_guidance_text(event: Dict[str, Any]) -> str:
    impacts = []
    watches = []
    for theme in _event_theme_names(event):
        guidance = _GLOBAL_THEME_GUIDANCE.get(theme) or {}
        if guidance.get("impact"):
            impacts.append(str(guidance["impact"]))
        if guidance.get("watch"):
            watches.append(str(guidance["watch"]))
    impact = "\uff1b".join(dict.fromkeys(impacts).keys()) or "\u9700\u7ed3\u5408\u76d8\u524d\u5e02\u573a\u8868\u73b0\u4e8c\u6b21\u786e\u8ba4"
    watch = "\uff1b".join(dict.fromkeys(watches).keys()) or "\u89c2\u5bdf\u76f8\u5173\u6807\u7684\u653e\u91cf\u3001\u8d44\u91d1\u6d41\u5411\u548c\u540c\u677f\u5757\u6269\u6563"
    return f"\u4f20\u5bfc: {impact}\n\u52a8\u4f5c: {watch}"


def build_global_event_brief(limit: int = 80, min_score: int = 45, top_n: int = 6) -> Dict[str, Any]:
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        db.init_db()
    positions = _load_positions()
    raw_events = fetch_events(source_urls=get_global_event_sources())[: max(1, limit)]
    candidates: List[Dict[str, Any]] = []
    low_score_count = 0
    for raw_event in raw_events:
        event = analyze_event(raw_event, positions=positions)
        if event["score"] < min_score:
            low_score_count += 1
            continue
        event["is_duplicate"] = _event_exists(event["event_id"])
        candidates.append(event)
    candidates.sort(key=lambda item: (int(item.get("score", 0)), str(item.get("published_at", ""))), reverse=True)
    top_events = candidates[: max(1, top_n)]
    return {
        "detected_at": _now_iso(),
        "source_count": len(get_global_event_sources()),
        "raw_count": len(raw_events),
        "candidate_count": len(candidates),
        "low_score_count": low_score_count,
        "top_events": top_events,
    }


def format_global_event_brief(brief: Dict[str, Any]) -> str:
    lines = [
        "\u5168\u7403\u7a81\u53d1\u8d22\u7ecf\u96f7\u8fbe",
        f"\u65f6\u95f4: {brief.get('detected_at', '')}",
        f"\u6765\u6e90={brief.get('source_count')} \u539f\u59cb={brief.get('raw_count')} \u9ad8\u5206={brief.get('candidate_count')} \u4f4e\u5206={brief.get('low_score_count')}",
    ]
    events = brief.get("top_events", []) or []
    if not events:
        lines.append("\u6682\u65e0\u8fbe\u5230\u9608\u503c\u7684\u5168\u7403\u7a81\u53d1\u8d22\u7ecf\u4e8b\u4ef6\u3002")
        return "\n".join(lines)
    for idx, event in enumerate(events, 1):
        duplicate = "\u5df2\u5165\u5e93" if event.get("is_duplicate") else "\u65b0\u4e8b\u4ef6"
        theme_text = "\u3001".join(_event_theme_names(event)[:4]) or "\u672a\u5339\u914d"
        lines.extend([
            "",
            f"{idx}. [{event.get('severity', 'P3')}/{event.get('score', 0)} {duplicate}] {event.get('title', '')}",
            f"\u4e3b\u9898: {theme_text}",
            f"A\u80a1\u6620\u5c04: {_event_related_stock_text(event)}",
            _event_guidance_text(event),
        ])
        if event.get("url"):
            lines.append(f"\u6765\u6e90: {event.get('url')}")
    return "\n".join(lines)


def watch_events(
    interval_seconds: int = 60,
    iterations: int = 0,
    push: bool = False,
    target: str = "",
    min_score: int = 45,
    limit: int = 100,
) -> Dict[str, Any]:
    runs = []
    count = 0
    while True:
        runs.append(scan_events(limit=limit, min_score=min_score, push=push, target=target))
        count += 1
        if iterations and count >= iterations:
            break
        time.sleep(max(5, interval_seconds))
    return {"runs": runs, "run_count": len(runs)}


def list_recent_events(limit: int = 20) -> Dict[str, Any]:
    db.init_db()
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, captured_at, data FROM market_snapshots WHERE snapshot_type='event_alert' ORDER BY captured_at DESC LIMIT ?",
        (max(1, limit),),
    ).fetchall()
    conn.close()
    events = []
    for row in rows:
        try:
            payload = json.loads(row["data"])
        except Exception:
            payload = {"raw": row["data"]}
        payload["_snapshot_id"] = row["id"]
        payload["_captured_at"] = row["captured_at"]
        events.append(payload)
    return {"count": len(events), "events": events}


def map_theme(query: str) -> Dict[str, Any]:
    text = str(query or "").strip()
    matches = _theme_matches(text)
    if not matches and text in THEME_MAP:
        config = THEME_MAP[text]
        matches = [{
            "theme": text,
            "keywords": config.get("keywords", []),
            "chains": config.get("chains", []),
            "stocks": config.get("stocks", []),
        }]
    return {"query": text, "matches": matches, "count": len(matches)}

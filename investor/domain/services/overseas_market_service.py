#!/usr/bin/env python3
"""Small, fail-closed overseas market snapshot for the 08:30 morning brief."""

from __future__ import annotations

import re
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Dict, List


US_SYMBOLS = {"usDJI": "道琼斯", "usINX": "标普500", "usIXIC": "纳斯达克"}
SINA_US_SYMBOLS = {"gb_dji": "道琼斯", "gb_inx": "标普500", "gb_ixic": "纳斯达克"}
ASIA_SYMBOLS = {"b_NKY": "日经225", "b_KOSPI": "韩国KOSPI"}


def _request(url: str, referer: str = "") -> str:
    headers = {"User-Agent": "Mozilla/5.0 OpenClaw-Investor/1.0"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=8) as response:
        return response.read().decode("gb18030", errors="replace")


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _is_recent(raw: str, max_days: int = 4) -> bool:
    text = str(raw or "").strip().replace("/", "-")
    try:
        observed = date.fromisoformat(text[:10])
    except Exception:
        return False
    return date.today() - timedelta(days=max_days) <= observed <= date.today()


def _fetch_us() -> Dict[str, Any]:
    try:
        raw = _request("https://qt.gtimg.cn/q=" + ",".join(US_SYMBOLS))
    except Exception:
        raw = ""
    items: List[Dict[str, Any]] = []
    for symbol, value in re.findall(r'v_([^=]+)="([^"]*)"', raw):
        if symbol not in US_SYMBOLS:
            continue
        fields = value.split("~")
        if len(fields) < 33:
            continue
        as_of = fields[30].strip()
        if not _is_recent(as_of):
            continue
        items.append({
            "name": US_SYMBOLS[symbol],
            "price": _number(fields[3]),
            "change_pct": _number(fields[32]),
            "as_of": as_of,
            "session": "收盘",
        })
    if items:
        return {"available": True, "reason": "", "items": items}
    try:
        raw = _request("https://hq.sinajs.cn/list=" + ",".join(SINA_US_SYMBOLS), "https://finance.sina.com.cn/")
    except Exception as exc:
        return {"available": False, "reason": f"美股行情读取失败（{type(exc).__name__}）", "items": []}
    for symbol, value in re.findall(r'var hq_str_([^=]+)="([^"]*)"', raw):
        if symbol not in SINA_US_SYMBOLS:
            continue
        fields = value.split(",")
        if len(fields) < 4 or not _is_recent(fields[3]):
            continue
        items.append({
            "name": SINA_US_SYMBOLS[symbol],
            "price": _number(fields[1]),
            "change_pct": _number(fields[2]),
            "as_of": fields[3].strip(),
            "session": "收盘",
        })
    return {"available": bool(items), "reason": "" if items else "美股行情为空或日期过期", "items": items}


def _fetch_asia() -> Dict[str, Any]:
    try:
        raw = _request("https://hq.sinajs.cn/list=" + ",".join(ASIA_SYMBOLS), "https://finance.sina.com.cn/")
    except Exception as exc:
        return {"available": False, "reason": f"日韩行情读取失败（{type(exc).__name__}）", "items": []}
    items: List[Dict[str, Any]] = []
    for symbol, value in re.findall(r'var hq_str_([^=]+)="([^"]*)"', raw):
        if symbol not in ASIA_SYMBOLS:
            continue
        fields = value.split(",")
        if len(fields) < 8:
            continue
        as_of = f"{fields[6].strip()} {fields[5].strip()}"
        # The 08:30 brief promises the current Japan/Korea open, not the latest
        # value available from a previous session. Reject every non-today row.
        if not _is_recent(as_of, max_days=0):
            continue
        items.append({
            "name": ASIA_SYMBOLS[symbol],
            "price": _number(fields[1]),
            "change_pct": _number(fields[3]),
            "as_of": as_of,
            "session": "开盘后",
        })
    return {"available": bool(items), "reason": "" if items else "日韩行情为空、尚未开盘或日期过期", "items": items}


def build_overseas_market_snapshot() -> Dict[str, Any]:
    us = _fetch_us()
    asia = _fetch_asia()
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "us": us,
        "asia": asia,
        "available": bool(us.get("available") or asia.get("available")),
    }

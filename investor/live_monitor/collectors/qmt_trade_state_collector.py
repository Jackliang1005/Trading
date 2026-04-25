#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Dict, List

from live_monitor.collectors.qmt_auth import build_qmt_auth_headers


def _candidate_servers() -> List[Dict[str, str]]:
    pairs = [
        (
            "guojin",
            os.getenv(
                "QMT2HTTP_MAIN_URL",
                os.getenv("QMT2HTTP_BASE_URL", "http://39.105.48.176:8085"),
            ).strip(),
        ),
        (
            "dongguan",
            os.getenv(
                "QMT2HTTP_DONGGUAN_BASE_URL",
                os.getenv("QMT2HTTP_TRADE_URL", "http://150.158.31.115:8085"),
            ).strip(),
        ),
        ("trade", os.getenv("QMT2HTTP_TRADE_URL", "").strip()),
        ("main", os.getenv("QMT2HTTP_MAIN_URL", "").strip()),
        ("default", os.getenv("QMT2HTTP_BASE_URL", "").strip()),
    ]
    seen = set()
    items = []
    for name, url in pairs:
        if not url or url in seen:
            continue
        seen.add(url)
        items.append({"name": name, "base_url": url.rstrip("/")})
    return items


def _headers() -> Dict[str, str]:
    return build_qmt_auth_headers()


def _fetch_json(url: str, timeout: float) -> Dict:
    started = time.time()
    payload = None
    error = None
    status_code = None
    try:
        req = urllib.request.Request(url, headers=_headers(), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"raw": raw}
        error = f"HTTP {exc.code}"
    except Exception as exc:
        error = str(exc)
    return {
        "http_status": status_code,
        "latency_ms": round((time.time() - started) * 1000, 1),
        "ok": bool(payload and payload.get("success")) and not error,
        "error": error,
        "response": payload,
    }


def _is_trade_only_mode(health_result: Dict) -> bool:
    payload = (health_result or {}).get("response") or {}
    data = payload.get("data") if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return False
    route_mode = str(data.get("route_mode", "") or "").lower()
    service_mode = str(data.get("service_mode", "") or "").lower()
    return route_mode == "trade_only" or "trade_only" in service_mode


def _skipped_result(url: str, reason: str = "trade_only_mode") -> Dict:
    return {
        "http_status": None,
        "latency_ms": 0.0,
        "ok": False,
        "error": f"skipped_expected:{reason}",
        "response": {},
        "url": url,
        "skipped_expected": True,
        "skip_reason": reason,
    }


def _summarize_endpoint(name: str, result: Dict) -> Dict:
    payload = (result.get("response") or {}).get("data")
    summary: Dict = {
        "name": name,
        "ok": bool(result.get("ok")),
        "count": 0,
        "skipped_expected": bool(result.get("skipped_expected")),
    }
    if name == "asset" and isinstance(payload, dict):
        for key in ("total_asset", "net_asset", "market_value", "cash", "available_cash"):
            if key in payload:
                summary[key] = payload.get(key)
    elif isinstance(payload, list):
        summary["count"] = len(payload)
        if name == "positions":
            summary["codes"] = [
                item.get("stock_code") or item.get("code") or item.get("证券代码")
                for item in payload[:20]
                if isinstance(item, dict)
            ]
        if name in {"trades", "records_trades"}:
            summary["codes"] = [
                item.get("stock_code") or item.get("code") or item.get("证券代码")
                for item in payload[:20]
                if isinstance(item, dict)
            ]
    return summary


def collect_qmt_trade_state() -> Dict:
    timeout = float(os.getenv("QMT2HTTP_TIMEOUT", "15"))
    servers = []
    endpoints = [
        ("asset", "/api/stock/asset"),
        ("positions", "/api/stock/positions"),
        ("orders", "/api/stock/orders"),
        ("trades", "/api/stock/trades"),
        ("records_trades", "/api/trade/records?record_type=trades"),
    ]
    trade_only_allowed = {"asset", "positions", "orders", "trades", "records_trades"}
    for server in _candidate_servers():
        health_url = f"{server['base_url']}/health"
        health_result = _fetch_json(health_url, timeout=timeout)
        health_result["url"] = health_url
        trade_only = _is_trade_only_mode(health_result)
        endpoint_results = {}
        endpoint_summaries = {}
        for name, route in endpoints:
            url = f"{server['base_url']}{route}"
            if trade_only and name not in trade_only_allowed:
                result = _skipped_result(url)
            else:
                result = _fetch_json(url, timeout=timeout)
                result["url"] = url
            endpoint_results[name] = result
            endpoint_summaries[name] = _summarize_endpoint(name, result)
        servers.append(
            {
                "server": server["name"],
                "base_url": server["base_url"],
                "health": health_result,
                "mode": {
                    "trade_only": trade_only,
                    "route_mode": (((health_result.get("response") or {}).get("data") or {}).get("route_mode", "")),
                    "service_mode": (((health_result.get("response") or {}).get("data") or {}).get("service_mode", "")),
                },
                "endpoints": endpoint_results,
                "summary": endpoint_summaries,
            }
        )
    return {"kind": "qmt_trade_state", "servers": servers}

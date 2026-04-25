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
    servers = []
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
        ("main", os.getenv("QMT2HTTP_MAIN_URL", "").strip()),
        ("trade", os.getenv("QMT2HTTP_TRADE_URL", "").strip()),
        ("default", os.getenv("QMT2HTTP_BASE_URL", "").strip()),
    ]
    seen = set()
    for name, url in pairs:
        if not url or url in seen:
            continue
        seen.add(url)
        servers.append({"name": name, "base_url": url.rstrip("/")})
    return servers


def _headers() -> Dict[str, str]:
    return build_qmt_auth_headers()


def collect_qmt_health() -> Dict:
    results = []
    timeout = float(os.getenv("QMT2HTTP_TIMEOUT", "10"))
    for server in _candidate_servers():
        url = f"{server['base_url']}/health"
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
        results.append(
            {
                "server": server["name"],
                "base_url": server["base_url"],
                "url": url,
                "http_status": status_code,
                "latency_ms": round((time.time() - started) * 1000, 1),
                "ok": bool(payload and payload.get("success")) and not error,
                "error": error,
                "response": payload,
            }
        )
    return {"kind": "qmt_health", "servers": results}

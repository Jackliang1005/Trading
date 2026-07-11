#!/usr/bin/env python3
"""Read-only qmt2http recovery probe, with explicit optional repair actions."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict

DEFAULT_BASE_URLS = {
    "guojin": "http://39.105.48.176:8085",
    "dongguan": "http://150.158.31.115:8085",
}
TOKEN_FILES = (
    "/etc/default/investor-event-watch",
    "/root/qmt2http/qmt2http_main.env",
    "/root/qmt2http/.env",
    "/root/.openclaw/workspace/investor/.env",
)


def _read_token() -> str:
    if os.getenv("QMT2HTTP_API_TOKEN"):
        return os.getenv("QMT2HTTP_API_TOKEN", "").strip()
    for path in TOKEN_FILES:
        try:
            for line in open(path, encoding="utf-8", errors="ignore"):
                raw = line.strip()
                if raw.startswith("QMT2HTTP_API_TOKEN="):
                    return raw.split("=", 1)[1].strip().strip('"').strip("'")
        except FileNotFoundError:
            continue
    return ""


def _headers() -> Dict[str, str]:
    token = _read_token()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["X-API-Token"] = token
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(base_url: str, method: str, path: str, payload: Dict[str, Any] | None = None, timeout: float = 8.0) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if method == "POST" else None
    started = time.time()
    status = None
    parsed: Any = None
    error = ""
    try:
        req = urllib.request.Request(url, data=body, headers=_headers(), method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {"raw": raw[:1000]}
        error = f"HTTP {exc.code}"
    except Exception as exc:
        error = str(exc)
    return {"ok": bool(isinstance(parsed, dict) and parsed.get("success")) and not error, "method": method, "url": url, "http_status": status, "error": error, "latency_ms": round((time.time() - started) * 1000, 1), "payload": parsed}


def _compact(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("payload", {}).get("data") if isinstance(payload.get("payload"), dict) else None
    return {"ok": payload.get("ok"), "http_status": payload.get("http_status"), "error": payload.get("error"), "latency_ms": payload.get("latency_ms"), "data": data}


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe qmt2http service recovery endpoints")
    parser.add_argument("--server", choices=sorted(DEFAULT_BASE_URLS), default="guojin")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--action", choices=("probe", "reconnect-client", "ensure-strategy", "restart-strategy"), default="probe")
    parser.add_argument("--login-password", default="", help="Optional QMT client login password for reconnect-client")
    parser.add_argument("--force", action="store_true", help="Required for mutating actions")
    args = parser.parse_args()

    base_url = (args.base_url or DEFAULT_BASE_URLS[args.server]).rstrip("/")
    result: Dict[str, Any] = {"server": args.server, "base_url": base_url, "action": args.action, "checks": {}}
    check_paths = {
        "health": "/health",
        "qmt_client": "/api/service/qmt-client",
        "strategy_service": "/api/service/strategy",
        "qmttrader_v2_status": "/api/qmttrader_v2/status",
        "positions": "/api/stock/positions",
    }
    with ThreadPoolExecutor(max_workers=len(check_paths)) as pool:
        futures = {pool.submit(_request, base_url, "GET", path, None, args.timeout): name for name, path in check_paths.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                result["checks"][name] = _compact(future.result())
            except Exception as exc:
                result["checks"][name] = {"ok": False, "http_status": None, "error": str(exc), "latency_ms": 0, "data": None}

    if args.action != "probe":
        if not args.force:
            result["mutation_skipped"] = "pass --force to execute mutating recovery action"
        elif args.action == "reconnect-client":
            payload: Dict[str, Any] = {"reason": "openclaw_remote_recovery", "reconnect": True, "ensure_strategy": True}
            if args.login_password:
                payload.update({"login": True, "login_password": args.login_password, "login_title": args.server})
            result["recovery"] = _compact(_request(base_url, "POST", "/api/service/qmt-client", payload, timeout=max(args.timeout, 20)))
        elif args.action == "ensure-strategy":
            result["recovery"] = _compact(_request(base_url, "POST", "/api/service/strategy", {"action": "ensure", "reason": "openclaw_remote_recovery"}, timeout=max(args.timeout, 20)))
        elif args.action == "restart-strategy":
            result["recovery"] = _compact(_request(base_url, "POST", "/api/service/strategy", {"action": "restart", "reason": "openclaw_remote_recovery"}, timeout=max(args.timeout, 30)))

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    bad = [name for name, item in result["checks"].items() if not item.get("ok")]
    return 0 if not bad else 2


if __name__ == "__main__":
    raise SystemExit(main())


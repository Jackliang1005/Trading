#!/usr/bin/env python3
"""qmt2http strategy service control for Feishu and CLI."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

DEFAULT_BASE_URLS = {
    "guojin": "http://39.105.48.176:8085",
    "dongguan": "http://150.158.31.115:8085",
}
TOKEN_FILES = (
    "/root/qmt2http/qmt2http_main.env",
    "/root/qmt2http/.env",
    "/root/.openclaw/workspace/investor/.env",
)
ACCOUNT_ALIASES = {"guojin": "\u56fd\u91d1", "dongguan": "\u4e1c\u839e"}
ACTION_LABELS = {"status": "\u72b6\u6001", "start": "\u542f\u52a8", "stop": "\u505c\u6b62", "restart": "\u91cd\u542f"}


def _read_token_from_file(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return ""
    for line in lines:
        raw = line.strip()
        if raw.startswith("QMT2HTTP_API_TOKEN="):
            return raw.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _resolve_token() -> str:
    if os.getenv("QMT2HTTP_API_TOKEN", "").strip():
        return os.getenv("QMT2HTTP_API_TOKEN", "").strip()
    for path in TOKEN_FILES:
        token = _read_token_from_file(path)
        if token:
            return token
    return ""


def _resolve_base_url(account: str) -> str:
    if account == "guojin":
        return (os.getenv("QMT2HTTP_MAIN_URL", "").strip() or os.getenv("QMT2HTTP_BASE_URL", "").strip() or DEFAULT_BASE_URLS["guojin"]).rstrip("/")
    return (os.getenv("QMT2HTTP_DONGGUAN_BASE_URL", "").strip() or os.getenv("QMT2HTTP_TRADE_URL", "").strip() or DEFAULT_BASE_URLS["dongguan"]).rstrip("/")


def _headers(token: str) -> Dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["X-API-Token"] = token
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(account: str, method: str, payload: Dict[str, Any] | None = None, timeout: float = 8.0) -> Dict[str, Any]:
    token = _resolve_token()
    base_url = _resolve_base_url(account)
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if method == "POST" else None
    url = f"{base_url}/api/service/strategy"
    try:
        req = urllib.request.Request(url, data=body, headers=_headers(token), method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else {}
            return {"ok": bool(parsed.get("success")), "http_status": resp.status, "payload": parsed, "url": url}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {"raw": raw[:500]}
        return {"ok": False, "http_status": exc.code, "error": f"HTTP {exc.code}", "payload": parsed, "url": url}
    except Exception as exc:
        return {"ok": False, "http_status": None, "error": str(exc), "payload": {}, "url": url}


def normalize_strategy_account(text: str) -> str:
    q = str(text or "").lower()
    if "\u56fd\u91d1" in q or "guojin" in q or "main" in q:
        return "guojin"
    if "\u4e1c\u839e" in q or "dongguan" in q or "trade" in q:
        return "dongguan"
    return ""


def normalize_strategy_action(text: str) -> str:
    q = str(text or "").lower()
    if any(k in q for k in ("restart", "\u91cd\u542f")):
        return "restart"
    if any(k in q for k in ("stop", "halt", "interrupt", "\u505c\u6b62", "\u505c\u6389", "\u5173\u95ed", "\u6682\u505c")):
        return "stop"
    if any(k in q for k in ("start", "ensure", "resume", "\u542f\u52a8", "\u5f00\u59cb", "\u6062\u590d")):
        return "start"
    if any(k in q for k in ("status", "check", "\u72b6\u6001", "\u67e5\u8be2")):
        return "status"
    return ""


def is_strategy_control_query(text: str) -> bool:
    q = str(text or "").lower()
    if "\u7b56\u7565" not in q and "strategy" not in q:
        return False
    return bool(normalize_strategy_action(q))


def _confirmed(text: str) -> bool:
    q = str(text or "").lower()
    return any(k in q for k in ("confirm", "confirmed", "--confirm", "\u786e\u8ba4", "\u6267\u884c"))


def control_strategy(account: str, action: str, confirm: bool = False, reason: str = "openclaw", timeout: float = 12.0) -> Dict[str, Any]:
    account = account or ""
    action = action or ""
    if account not in DEFAULT_BASE_URLS:
        return {"ok": False, "text": "\u8bf7\u6307\u5b9a\u8d26\u6237\uff1a\u56fd\u91d1 \u6216 \u4e1c\u839e\u3002\u4f8b\uff1a/\u7b56\u7565\u72b6\u6001 \u56fd\u91d1"}
    if action not in {"status", "start", "stop", "restart"}:
        return {"ok": False, "text": "\u8bf7\u6307\u5b9a\u52a8\u4f5c\uff1a\u72b6\u6001/\u542f\u52a8/\u505c\u6b62/\u91cd\u542f\u3002"}
    if action != "status" and not confirm:
        status = _request(account, "GET", timeout=timeout)
        text = format_strategy_result(account, "status", status)
        alias = ACCOUNT_ALIASES.get(account, account)
        label = ACTION_LABELS.get(action, action)
        text += f"\n\n\u672a\u6267\u884c {alias} \u7b56\u7565{label}\u3002\u5982\u9700\u6267\u884c\uff0c\u8bf7\u53d1\u9001\uff1a/\u7b56\u7565{label} {alias} \u786e\u8ba4"
        return {"ok": False, "needs_confirm": True, "text": text, "status_probe": status}
    if action == "status":
        result = _request(account, "GET", timeout=timeout)
    else:
        payload = {"action": action, "reason": reason, "force": False, "timeout": int(max(timeout, 12))}
        result = _request(account, "POST", payload=payload, timeout=max(timeout, 20))
    return {"ok": bool(result.get("ok")), "text": format_strategy_result(account, action, result), "result": result}


def handle_strategy_control_query(text: str) -> str:
    account = normalize_strategy_account(text)
    action = normalize_strategy_action(text)
    result = control_strategy(account=account, action=action, confirm=_confirmed(text), reason="feishu_strategy_control")
    return str(result.get("text") or result)


def format_strategy_result(account: str, action: str, result: Dict[str, Any]) -> str:
    alias = ACCOUNT_ALIASES.get(account, account)
    label = ACTION_LABELS.get(action, action)
    payload = result.get("payload") or {}
    data = payload.get("data") if isinstance(payload, dict) else {}
    if not result.get("ok") and not data:
        return f"{alias} \u7b56\u7565{label}\u5931\u8d25: {result.get('error', 'unknown_error')} http={result.get('http_status')}"
    process = data.get("process") if isinstance(data, dict) else {}
    matches = process.get("matches") if isinstance(process, dict) else []
    window = data.get("window") if isinstance(data, dict) else {}
    lines = [
        f"{alias} \u7b56\u7565{label}: {'OK' if result.get('ok') else 'FAILED'} http={result.get('http_status')}",
        f"status={data.get('status', data.get('running', 'unknown'))} ok={data.get('ok', result.get('ok'))}",
        f"process_matches={len(matches or [])}",
    ]
    if isinstance(window, dict):
        lines.append(f"window_active={window.get('active')} now={window.get('now')} range={window.get('start')}-{window.get('end')}")
    if action in {"start", "stop", "restart"}:
        res = data.get("result") or data.get("start_result") or data.get("stop_result")
        if isinstance(res, dict):
            lines.append(f"script_success={res.get('success')} action={res.get('action')} returncode={res.get('returncode')}")
            stderr = str(res.get("stderr") or "").strip()
            if stderr:
                lines.append("stderr=" + stderr[-300:])
    return "\n".join(lines)

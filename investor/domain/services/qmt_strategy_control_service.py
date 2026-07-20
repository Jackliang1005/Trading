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
        status = _request(account, "GET", timeout=min(timeout, 8.0))
        guard_text = _mutation_guard_text(account, action, status)
        if guard_text:
            return {"ok": False, "not_executed": True, "text": guard_text, "status_probe": status}
        payload = {"action": action, "reason": reason, "force": False, "timeout": int(max(timeout, 12))}
        result = _request(account, "POST", payload=payload, timeout=max(timeout, 20))
    return {"ok": bool(result.get("ok")), "text": format_strategy_result(account, action, result), "result": result}


def _strategy_data(result: Dict[str, Any]) -> Dict[str, Any]:
    payload = result.get("payload") if isinstance(result, dict) else {}
    data = payload.get("data") if isinstance(payload, dict) else {}
    return data if isinstance(data, dict) else {}


def _strategy_running(data: Dict[str, Any]) -> bool:
    value = data.get("running", data.get("status"))
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "running", "active", "1"}
    return bool(value)


def _mutation_guard_text(account: str, action: str, status: Dict[str, Any]) -> str:
    alias = ACCOUNT_ALIASES.get(account, account)
    label = ACTION_LABELS.get(action, action)
    status_text = format_strategy_result(account, "status", status)
    data = _strategy_data(status)
    window = data.get("window") if isinstance(data, dict) else {}
    running = _strategy_running(data)
    sep = chr(10) + chr(10)
    if not status.get("ok"):
        return status_text + sep + f"\u672a\u6267\u884c {alias} \u7b56\u7565{label}\uff1a\u72b6\u6001\u63a2\u6d4b\u5931\u8d25\uff0c\u907f\u514d\u5728\u672a\u77e5\u72b6\u6001\u4e0b\u6267\u884c\u751f\u4ea7\u53d8\u66f4\u3002"
    if action in {"start", "restart"} and isinstance(window, dict) and window.get("active") is False:
        now = window.get("now", "")
        start = window.get("start", "")
        end = window.get("end", "")
        return status_text + sep + f"未执行 {alias} 策略{label}：当前时间 {now or '未知'} 不在运行时段 {start or '未知'}–{end or '未知'}。请在交易日运行时段内重试。"
    if action == "start" and running:
        return status_text + sep + f"\u672a\u6267\u884c {alias} \u7b56\u7565\u542f\u52a8\uff1a\u7b56\u7565\u5df2\u5728\u8fd0\u884c\u3002"
    if action == "stop" and not running:
        return status_text + sep + f"\u672a\u6267\u884c {alias} \u7b56\u7565\u505c\u6b62\uff1a\u7b56\u7565\u5f53\u524d\u672a\u8fd0\u884c\u3002"
    return ""

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
        status = result.get("http_status")
        status_note = f"（HTTP {status}）" if status else ""
        return (
            f"❌ {alias}策略{label}失败\n\n"
            f"- 原因：{result.get('error') or '接口没有返回可验证结果'}{status_note}\n"
            "- 未根据进程、持仓或历史状态推断成功。"
        )
    process = data.get("process") if isinstance(data, dict) else {}
    matches = process.get("matches") if isinstance(process, dict) else []
    window = data.get("window") if isinstance(data, dict) else {}
    running_value = data.get("status", data.get("running"))
    if isinstance(running_value, str):
        normalized = running_value.strip().lower()
        running = normalized in {"true", "running", "active", "1"}
        running_known = normalized not in {"", "unknown", "none"}
    elif isinstance(running_value, bool):
        running = running_value
        running_known = True
    else:
        running = False
        running_known = running_value is not None
    running_text = "运行中" if running else "未运行" if running_known else "无法确认"
    success_mark = "🧭" if action == "status" else "✅" if result.get("ok") else "❌"
    lines = [
        f"{success_mark} {alias}策略{label}",
        "",
        f"- 运行状态：{running_text}",
        f"- 策略进程：{'已发现对应进程' if matches else '未发现对应进程'}",
    ]
    if isinstance(window, dict):
        active = window.get("active")
        active_text = "运行时段内" if active is True else "运行时段外" if active is False else "时段状态未知"
        lines.append(
            f"- 时间窗口：{active_text}；当前 {window.get('now') or '未知'}；"
            f"规则 {window.get('start') or '未知'}–{window.get('end') or '未知'}"
        )
    if action in {"start", "stop", "restart"}:
        res = data.get("result") or data.get("start_result") or data.get("stop_result")
        if isinstance(res, dict):
            action_ok = res.get("success") is True and int(res.get("returncode", 0) or 0) == 0
            lines.append(f"- 执行验证：{'脚本执行成功' if action_ok else '脚本未确认成功'}")
            stderr = str(res.get("stderr") or "").strip()
            if stderr:
                lines.append("- 错误摘要：" + stderr[-300:])
    lines.extend(["", "- 以上结论仅来自 qmt2http 策略接口的本次回读，不会根据持仓反推运行状态。"])
    return "\n".join(lines)

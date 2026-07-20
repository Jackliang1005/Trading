#!/usr/bin/env python3
"""Read and safely update qmttrader_v2 position-monitor exemptions."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List

from domain.services.qmt_strategy_control_service import (
    ACCOUNT_ALIASES,
    DEFAULT_BASE_URLS,
    _headers,
    _resolve_base_url,
    _resolve_token,
    normalize_strategy_account,
)

ENDPOINT = "/api/qmttrader_v2/position-monitor-exemptions"
ACTION_LABELS = {"set": "设置", "append": "追加", "remove": "移除"}


def normalize_a_share_symbol(value: str) -> str:
    symbol = str(value or "").strip().upper()
    matched = re.fullmatch(r"(\d{6})(?:\.(SH|SZ))?", symbol)
    if not matched:
        raise ValueError(f"无效 A 股代码: {value}")
    code, market = matched.groups()
    expected = "SH" if code[0] in "569" else "SZ" if code[0] in "0123" else ""
    if not expected:
        raise ValueError(f"无法判断交易所: {value}")
    if market and market != expected:
        raise ValueError(f"A 股代码与交易所不匹配: {value}")
    return f"{code}.{expected}"


def normalize_symbols(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        symbol = normalize_a_share_symbol(value)
        if symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


def _request(account: str, method: str, symbols: List[str] | None = None, timeout: float = 8.0) -> Dict[str, Any]:
    url = f"{_resolve_base_url(account)}{ENDPOINT}"
    body = json.dumps({"symbols": symbols}, ensure_ascii=False).encode("utf-8") if method == "POST" else None
    try:
        req = urllib.request.Request(url, data=body, headers=_headers(_resolve_token()), method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
            return {
                "ok": 200 <= resp.status < 300 and not (isinstance(payload, dict) and payload.get("success") is False),
                "http_status": resp.status,
                "payload": payload,
                "url": url,
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"raw": raw[:500]}
        return {"ok": False, "http_status": exc.code, "error": f"HTTP {exc.code}", "payload": payload, "url": url}
    except Exception as exc:
        return {"ok": False, "http_status": None, "error": str(exc), "payload": {}, "url": url}


def _extract_symbols(result: Dict[str, Any]) -> List[str] | None:
    if not result.get("ok"):
        return None
    payload = result.get("payload")
    candidates = [payload]
    if isinstance(payload, dict):
        candidates.extend((payload.get("data"), payload.get("result")))
    for candidate in candidates:
        if isinstance(candidate, dict) and isinstance(candidate.get("symbols"), list):
            try:
                return normalize_symbols(candidate["symbols"])
            except ValueError:
                return None
    return None


def _failure_text(account: str, stage: str, result: Dict[str, Any]) -> str:
    alias = ACCOUNT_ALIASES.get(account, account)
    raw_error = str(result.get("error") or "").lower()
    status = result.get("http_status")
    if status:
        detail = f"接口返回 HTTP {status}"
    elif "timed out" in raw_error or "timeout" in raw_error:
        detail = "连接超时"
    elif "closed connection" in raw_error or "remote end closed" in raw_error:
        detail = "远端主动关闭连接"
    elif raw_error:
        detail = "接口连接失败"
    else:
        detail = "接口响应未包含有效名单"
    return "\n".join([
        f"❌ {alias}持仓监控豁免名单操作失败",
        "",
        f"- 失败阶段：{stage}",
        f"- 原因：{detail}",
        "- 结果：未确认写入成功，名单状态不得根据持仓或历史记录推断。",
    ])


def list_position_monitor_exemptions(account: str, timeout: float = 8.0) -> Dict[str, Any]:
    if account not in DEFAULT_BASE_URLS:
        return {"ok": False, "text": "请指定账户：国金 或 东莞。"}
    result = _request(account, "GET", timeout=timeout)
    symbols = _extract_symbols(result)
    if symbols is None:
        return {"ok": False, "text": _failure_text(account, "GET 读取", result), "result": result}
    alias = ACCOUNT_ALIASES.get(account, account)
    listing = "、".join(symbols) if symbols else "当前为空名单"
    text = "\n".join([
        f"🛡️ {alias}持仓监控豁免名单",
        "",
        f"- 接口回读成功，共 {len(symbols)} 只。",
        f"- 名单：{listing}",
        "- 数据来源：qmt2http 持仓监控豁免接口的本次 GET 回读。",
    ])
    return {"ok": True, "symbols": symbols, "text": text, "result": result}


def update_position_monitor_exemptions(
    account: str,
    action: str,
    symbols: Iterable[str],
    confirm: bool = False,
    timeout: float = 12.0,
) -> Dict[str, Any]:
    if account not in DEFAULT_BASE_URLS:
        return {"ok": False, "text": "请指定账户：国金 或 东莞。"}
    if action not in ACTION_LABELS:
        return {"ok": False, "text": "请指定动作：set/append/remove。"}
    try:
        requested = normalize_symbols(symbols)
    except ValueError as exc:
        return {"ok": False, "text": str(exc)}
    if not requested:
        return {"ok": False, "text": f"{ACTION_LABELS[action]}豁免名单至少需要一个 A 股代码；未写入。"}
    if not confirm:
        alias = ACCOUNT_ALIASES.get(account, account)
        codes = " ".join(requested) if requested else "（空名单）"
        return {
            "ok": False,
            "needs_confirm": True,
            "text": "\n".join([
                "⚠️ 豁免名单变更待确认",
                "",
                f"- 账户：{alias}",
                f"- 动作：{ACTION_LABELS[action]}",
                f"- 目标代码：{codes}",
                "- 当前状态：未写入。请加“确认”或 --confirm 后重新发送。",
            ]),
        }

    before_result = _request(account, "GET", timeout=timeout)
    current = _extract_symbols(before_result)
    if current is None:
        return {"ok": False, "text": _failure_text(account, "写入前读取当前名单", before_result), "before": before_result}

    if action == "set":
        expected = requested
    elif action == "append":
        expected = normalize_symbols([*current, *requested])
    else:
        removed = set(requested)
        expected = [symbol for symbol in current if symbol not in removed]

    post_result = _request(account, "POST", symbols=expected, timeout=timeout)
    if not post_result.get("ok"):
        return {"ok": False, "text": _failure_text(account, "提交新名单", post_result), "before": before_result, "post": post_result}

    verify_result = _request(account, "GET", timeout=timeout)
    actual = _extract_symbols(verify_result)
    if actual is None:
        return {"ok": False, "text": _failure_text(account, "写入后回读验证", verify_result), "post": post_result, "verify": verify_result}
    if actual != expected:
        alias = ACCOUNT_ALIASES.get(account, account)
        expected_text = "、".join(expected) if expected else "空名单"
        actual_text = "、".join(actual) if actual else "空名单"
        return {
            "ok": False,
            "text": "\n".join([
                f"❌ {alias}持仓监控豁免名单操作失败",
                "",
                "- 失败阶段：写入后回读验证",
                f"- 预期名单：{expected_text}",
                f"- 实际回读：{actual_text}",
                "- 结果：回读不一致，未确认写入成功。",
            ]),
            "expected": expected,
            "actual": actual,
            "post": post_result,
            "verify": verify_result,
        }

    alias = ACCOUNT_ALIASES.get(account, account)
    listing = "、".join(actual) if actual else "（空）"
    return {
        "ok": True,
        "symbols": actual,
        "text": "\n".join([
            f"✅ 已{ACTION_LABELS[action]}{alias}持仓监控豁免名单",
            "",
            f"- 当前名单共 {len(actual)} 只：{listing}",
            "- 验证：写入接口成功，随后 GET 回读与预期完全一致。",
        ]),
        "before": current,
        "post": post_result,
        "verify": verify_result,
    }


def is_position_monitor_exemptions_query(text: str) -> bool:
    query = str(text or "").lower()
    return "豁免名单" in query or any(key in query for key in ("设置豁免", "追加豁免", "移除豁免", "position-monitor-exemptions"))


def handle_position_monitor_exemptions_query(text: str) -> str:
    query = str(text or "").strip()
    account = normalize_strategy_account(query)
    if "设置豁免" in query or re.search(r"\bset\b", query, re.IGNORECASE):
        action = "set"
    elif "追加豁免" in query or re.search(r"\bappend\b", query, re.IGNORECASE):
        action = "append"
    elif "移除豁免" in query or re.search(r"\bremove\b", query, re.IGNORECASE):
        action = "remove"
    else:
        return list_position_monitor_exemptions(account).get("text", "")
    symbols = re.findall(r"(?<!\d)(\d{6}(?:\.(?:SH|SZ))?)(?!\d)", query, flags=re.IGNORECASE)
    confirmed = any(key in query.lower() for key in ("确认", "confirm", "--confirm"))
    return update_position_monitor_exemptions(account, action, symbols, confirm=confirmed).get("text", "")

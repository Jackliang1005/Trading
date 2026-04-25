#!/usr/bin/env python3
"""Scheduled trading briefings for fixed intraday slots."""

from __future__ import annotations

import ast
import json
import os
import urllib.parse
import urllib.request
import re
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

from live_monitor.collectors.qmt_auth import build_qmt_auth_headers


DONGGUAN_DEFAULT_URL = "http://150.158.31.115:8085"
GUOJIN_DEFAULT_URL = "http://39.105.48.176:8085"
ETF_PREFIXES = (
    "15",
    "16",
    "50",
    "51",
    "56",
    "58",
)
FINAL_SELECTION_RE = re.compile(r"最终选股完成,\s*信号:\s*(\d+),\s*标的:\s*(\[[^\]]*\])")
PRIORITY_BUY_RE = re.compile(r"符合条件的 .* 股票:\s*(\[[^\]]*\])")
BUY_SUBMIT_RE = re.compile(r"买入委托已提交\s*-\s*代码:\s*([0-9]{6}\.[A-Z]{2})")
BUY_FILLED_RE = re.compile(r"检测到当日买入成交:\s*([0-9]{6}\.[A-Z]{2})")
GENERIC_CODES_RE = re.compile(r"([0-9]{6}\.[A-Z]{2})")
PRIMARY_STRATEGY_RE = re.compile(r"主策略\s+([a-zA-Z0-9_]+)\s+有\s+\d+\s+只股票通过评分")
KEEP_ONLY_RE = re.compile(r"(?:仅保留|只保留)\s+([a-zA-Z0-9_]+)")
MOMENTUM_SCORE_RE = re.compile(
    r"^\s*\d+\.\s*(?P<name>[^()]+?)\((?P<code>[0-9]{6}\.[A-Z]{4})\)\s+momentum_score:\s*(?P<score>[+-]?\d+(?:\.\d+)?)"
)
ORDER_SUCCESS_RE = re.compile(
    r"下单成功\s*-\s*代码:\s*(?P<code>[0-9]{6}\.[A-Z]{2}),\s*方向:\s*(?P<side>buy|sell),\s*数量:\s*(?P<qty>\d+),\s*价格:\s*(?P<price>[0-9.]+)",
    re.IGNORECASE,
)
PACK_BUY_RE = re.compile(
    r"📦\s*买入(?P<code>[0-9]{6}\.[A-Z]{4}).*?数量[:：]\s*(?P<qty>\d+).*?价格[:：]\s*(?P<price>[0-9.]+)"
)
PACK_SELL_RE = re.compile(
    r"📦\s*卖出(?P<code>[0-9]{6}\.[A-Z]{4}).*?数量[:：]\s*(?P<qty>\d+).*?价格[:：]\s*(?P<price>[0-9.]+)"
)
TS_PREFIX_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")


def _normalize_date(raw: str = "") -> str:
    value = str(raw or "").strip()
    if not value:
        return datetime.now().strftime("%Y-%m-%d")
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def _compact_date(iso_date: str) -> str:
    return str(iso_date or "").replace("-", "")


def _parse_any_date(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw
    return ""


def _rpc_call(base_url: str, method: str, params: Dict) -> Dict:
    payload = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/rpc/data_fetcher",
        data=payload,
        headers={**build_qmt_auth_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15.0) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}


def _resolve_trade_date(input_iso: str) -> str:
    compact = _compact_date(input_iso)
    base_urls = [
        os.getenv("QMT2HTTP_MAIN_URL", "").strip()
        or os.getenv("QMT2HTTP_BASE_URL", "").strip()
        or GUOJIN_DEFAULT_URL,
        os.getenv("QMT2HTTP_DONGGUAN_BASE_URL", "").strip()
        or os.getenv("QMT2HTTP_TRADE_URL", "").strip()
        or DONGGUAN_DEFAULT_URL,
    ]
    for base_url in [url for url in base_urls if url]:
        for params in (
            {"date": compact},
            {"trade_date": compact},
            {"date_str": compact},
            {},
        ):
            try:
                payload = _rpc_call(base_url, "get_previous_trading_date", params)
            except Exception:
                continue
            if not bool(payload.get("success")):
                continue
            data = payload.get("data")
            if isinstance(data, dict):
                for key in ("date", "trade_date", "previous_trading_date", "value"):
                    candidate = _parse_any_date(str(data.get(key, "") or ""))
                    if candidate:
                        return candidate
            candidate = _parse_any_date(str(data or ""))
            if candidate:
                return candidate

    # Fallback: weekend adjustment when RPC unavailable
    current = datetime.strptime(input_iso, "%Y-%m-%d").date()
    weekday = current.weekday()
    if weekday == 5:
        return (current - timedelta(days=1)).isoformat()
    if weekday == 6:
        return (current - timedelta(days=2)).isoformat()
    return input_iso


def _effective_trade_date(raw_date: str = "") -> str:
    normalized = _normalize_date(raw_date)
    return _resolve_trade_date(normalized)


def _extract_code(item: Dict) -> str:
    return str(item.get("stock_code") or item.get("code") or item.get("证券代码") or "").strip()


def _extract_name(item: Dict) -> str:
    return str(item.get("stock_name") or item.get("name") or item.get("证券名称") or "").strip()


def _is_etf(item: Dict) -> bool:
    code = _extract_code(item)
    name = _extract_name(item).upper()
    digits = code.split(".", 1)[0] if code else ""
    if "ETF" in name:
        return True
    return bool(digits) and digits.startswith(ETF_PREFIXES)


def _http_get(base_url: str, path: str, timeout: float = 15.0) -> Dict:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers=build_qmt_auth_headers(),
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw else {}
        return payload if isinstance(payload, dict) else {}


def _to_szsh(code: str) -> str:
    raw = str(code or "").strip().upper()
    if not raw:
        return ""
    if raw.endswith(".XSHE"):
        return raw.replace(".XSHE", ".SZ")
    if raw.endswith(".XSHG"):
        return raw.replace(".XSHG", ".SH")
    return raw


def _safe_literal_list(raw: str) -> List[str]:
    try:
        value = ast.literal_eval(raw)
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _has_strategy_token(strategy_name: str, token: str) -> bool:
    target = str(strategy_name or "").upper()
    needle = str(token or "").upper()
    if not target or not needle:
        return False
    # Match token as a standalone segment, avoid false hit like "ENHANCE" -> "NH".
    return re.search(rf"(^|[^A-Z0-9]){re.escape(needle)}([^A-Z0-9]|$)", target) is not None


def _fetch_dongguan_log_lines(date_text: str) -> Dict:
    base_url = (
        os.getenv("QMT2HTTP_DONGGUAN_BASE_URL", "").strip()
        or os.getenv("QMT2HTTP_TRADE_URL", "").strip()
        or DONGGUAN_DEFAULT_URL
    )
    # Use a large window to include morning strategy decision lines (e.g. 09:26 main strategy logs).
    query = urllib.parse.urlencode({"date": date_text, "lines": 30000, "include_content": "true"})
    payload = _http_get(base_url, f"/api/trade/log?{query}")
    if not bool(payload.get("success")):
        return {"ok": False, "date": date_text, "error": payload.get("message", "log_request_failed"), "lines": []}
    data = payload.get("data") or {}
    lines: List[str] = []
    if isinstance(data, dict):
        raw_lines = data.get("lines")
        if isinstance(raw_lines, list):
            lines = [str(line) for line in raw_lines if line is not None]
        entries = data.get("entries")
        if (not lines) and isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                content = entry.get("content")
                if isinstance(content, list) and content:
                    lines = [str(line) for line in content if line is not None]
                    break
    return {
        "ok": True,
        "date": date_text,
        "lines": lines,
    }


def _fetch_recent_dongguan_logs(anchor_iso: str, days: int = 5) -> Dict:
    base_date = datetime.strptime(anchor_iso, "%Y-%m-%d").date()
    for offset in range(max(1, days)):
        target = (base_date - timedelta(days=offset)).isoformat()
        result = _fetch_dongguan_log_lines(target)
        if result.get("ok") and result.get("lines"):
            return result
    return _fetch_dongguan_log_lines(anchor_iso)


def _parse_strategy_snapshot_from_lines(lines: List[str]) -> Dict:
    strategy = ""
    final_candidates: List[str] = []
    priority_candidates: List[str] = []
    submitted: List[str] = []
    filled: List[str] = []
    phase1: List[str] = []
    primary_modes: List[str] = []
    nh_mode_codes: List[str] = []
    mix_mode_codes: List[str] = []
    for line in lines:
        if "[" in line and "]" in line and not strategy:
            try:
                strategy = line.split("[", 1)[1].split("]", 1)[0].strip()
            except Exception:
                pass
        match = FINAL_SELECTION_RE.search(line)
        if match:
            final_candidates = _safe_literal_list(match.group(2))
        match = PRIORITY_BUY_RE.search(line)
        if match:
            priority_candidates = _safe_literal_list(match.group(1))
        match = BUY_SUBMIT_RE.search(line)
        if match:
            submitted.append(match.group(1))
        match = BUY_FILLED_RE.search(line)
        if match:
            filled.append(match.group(1))
        if "候选股:" in line:
            phase1.extend(GENERIC_CODES_RE.findall(line))
        inline_codes = GENERIC_CODES_RE.findall(line)
        upper_line = str(line or "").upper()
        if inline_codes:
            if _has_strategy_token(upper_line, "NH"):
                nh_mode_codes.extend(inline_codes)
            if _has_strategy_token(upper_line, "MIX"):
                mix_mode_codes.extend(inline_codes)
        if _has_strategy_token(upper_line, "NH"):
            list_match = re.search(r"\[([^\]]+)\]", str(line or ""))
            if list_match:
                for code in GENERIC_CODES_RE.findall(list_match.group(0)):
                    nh_mode_codes.append(code)
        if _has_strategy_token(upper_line, "MIX"):
            list_match = re.search(r"\[([^\]]+)\]", str(line or ""))
            if list_match:
                for code in GENERIC_CODES_RE.findall(list_match.group(0)):
                    mix_mode_codes.append(code)
        match = PRIMARY_STRATEGY_RE.search(line)
        if match:
            primary_modes.append(match.group(1).strip().lower())
        match = KEEP_ONLY_RE.search(line)
        if match:
            primary_modes.append(match.group(1).strip().lower())

    return {
        "strategy": strategy,
        "final_candidates": list(dict.fromkeys(final_candidates)),
        "priority_buy_candidates": list(dict.fromkeys(priority_candidates)),
        "submitted_buys": list(dict.fromkeys([code for code in submitted if code])),
        "filled_buys": list(dict.fromkeys([code for code in filled if code])),
        "phase1_candidates": list(dict.fromkeys([code for code in phase1 if code])),
        "primary_modes": list(dict.fromkeys([mode for mode in primary_modes if mode])),
        "nh_mode_codes": list(dict.fromkeys([code for code in nh_mode_codes if code])),
        "mix_mode_codes": list(dict.fromkeys([code for code in mix_mode_codes if code])),
    }


def _summarize_log_lines(text_lines: List[str]) -> Dict:
    if not text_lines:
        return {"ok": True, "line_count": 0, "error_count": 0, "hits": []}
    error_hits = []
    keys = ("Traceback", "ERROR", "Exception", "失败", "超时", "断开", "request failed")
    for line in text_lines:
        if any(key in line for key in keys):
            error_hits.append(line)
    return {
        "ok": True,
        "line_count": len(text_lines),
        "error_count": len(error_hits),
        "hits": error_hits[:5],
    }


def build_0945_dongguan_brief(date_text: str = "") -> str:
    as_of = _normalize_date(date_text)
    fetched = _fetch_recent_dongguan_logs(as_of, days=5)
    lines = fetched.get("lines", []) or []
    snapshot = _parse_strategy_snapshot_from_lines(lines)
    strategy = str(snapshot.get("strategy", "") or "").strip()
    primary_modes = [str(item).lower() for item in (snapshot.get("primary_modes", []) or [])]
    nh_active = ("nh" in primary_modes) or _has_strategy_token(strategy, "NH")
    mix_active = ("mix" in primary_modes) or _has_strategy_token(strategy, "MIX")

    selected = list(
        dict.fromkeys(
            (snapshot.get("final_candidates", []) or []) + (snapshot.get("priority_buy_candidates", []) or [])
        )
    )
    submitted_codes = list(dict.fromkeys(snapshot.get("submitted_buys", []) or []))
    filled = list(dict.fromkeys(snapshot.get("filled_buys", []) or []))
    bought = list(dict.fromkeys(submitted_codes + filled))
    monitor_candidates = list(dict.fromkeys(selected + (snapshot.get("phase1_candidates", []) or [])))
    monitoring = [code for code in monitor_candidates if code and code not in bought]
    nh_monitoring = [code for code in (snapshot.get("nh_mode_codes", []) or []) if code and code not in bought]
    mix_monitoring = [code for code in (snapshot.get("mix_mode_codes", []) or []) if code and code not in bought]

    log_summary = _summarize_log_lines(lines)

    lines: List[str] = [f"⏰ 09:45 东莞策略巡检 [{as_of}]"]
    lines.append(f"策略: {strategy or 'unknown'}")
    lines.append(f"日志日期: {fetched.get('date', as_of)}")
    if primary_modes:
        lines.append(f"日志主策略: {','.join(primary_modes)}")
    if nh_active:
        lines.append("模式: NH策略已激活")
    else:
        lines.append("模式: NH未激活，按MIX口径输出" if mix_active else "模式: NH未激活（按当前策略口径输出）")
    lines.append(f"已买入/已提交: {', '.join(bought) if bought else '无'}")
    if nh_active:
        nh_view = nh_monitoring or monitoring
        lines.append(f"NH监控池: {', '.join(nh_view[:20]) if nh_view else '无'}")
    elif mix_active:
        mix_view = mix_monitoring or monitoring
        lines.append(f"MIX监控池: {', '.join(mix_view[:20]) if mix_view else '无'}")
    lines.append(f"正在监控: {', '.join(monitoring[:12]) if monitoring else '无'}")
    if log_summary.get("ok"):
        lines.append(f"东莞日志: lines={log_summary.get('line_count', 0)} error_hits={log_summary.get('error_count', 0)}")
        for hit in log_summary.get("hits", []):
            lines.append(f"- {hit}")
    else:
        lines.append(f"东莞日志: 获取失败 ({log_summary.get('error', 'unknown')})")
    return "\n".join(lines)


def _fetch_guojin_endpoint(path: str) -> List[Dict]:
    base_url = (
        os.getenv("QMT2HTTP_MAIN_URL", "").strip()
        or os.getenv("QMT2HTTP_BASE_URL", "").strip()
        or GUOJIN_DEFAULT_URL
    )
    payload = _http_get(base_url, path)
    if not bool(payload.get("success")):
        return []
    rows = payload.get("data", [])
    return rows if isinstance(rows, list) else []


def _fetch_guojin_log_lines(date_text: str, lines: int = 30000) -> Dict:
    base_url = (
        os.getenv("QMT2HTTP_MAIN_URL", "").strip()
        or os.getenv("QMT2HTTP_BASE_URL", "").strip()
        or GUOJIN_DEFAULT_URL
    )
    query = urllib.parse.urlencode({"date": date_text, "lines": lines, "include_content": "true"})
    payload = _http_get(base_url, f"/api/trade/log?{query}")
    if not bool(payload.get("success")):
        return {"ok": False, "date": date_text, "error": payload.get("message", "log_request_failed"), "lines": []}
    data = payload.get("data") or {}
    text_lines: List[str] = []
    if isinstance(data, dict):
        raw_lines = data.get("lines")
        if isinstance(raw_lines, list):
            text_lines = [str(line) for line in raw_lines if line is not None]
        entries = data.get("entries")
        if (not text_lines) and isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                content = entry.get("content")
                if isinstance(content, list) and content:
                    text_lines = [str(line) for line in content if line is not None]
                    break
    return {"ok": True, "date": date_text, "lines": text_lines}


def _filter_lines_before_slot(lines: List[str], slot_label: str) -> List[str]:
    hm = str(slot_label or "").replace(":", "")
    try:
        cutoff = int(hm)
    except Exception:
        return lines
    filtered: List[str] = []
    for line in lines:
        ts_match = TS_PREFIX_RE.match(str(line or ""))
        if not ts_match:
            filtered.append(line)
            continue
        try:
            dt = datetime.strptime(ts_match.group("ts"), "%Y-%m-%d %H:%M:%S")
            current = dt.hour * 100 + dt.minute
            if current <= cutoff:
                filtered.append(line)
        except Exception:
            filtered.append(line)
    return filtered or lines


def _parse_etf_scores_and_actions(lines: List[str]) -> Dict:
    # score_map keeps latest seen score by code.
    score_map: Dict[str, Dict[str, object]] = {}
    in_final_target = False
    final_target_codes: List[str] = []
    buy_actions: List[Dict[str, object]] = []
    sell_actions: List[Dict[str, object]] = []

    for line in lines:
        text = str(line or "")
        if "【最终目标】共" in text:
            in_final_target = True
            continue
        if text.strip().startswith(">>>"):
            in_final_target = False

        m = MOMENTUM_SCORE_RE.search(text)
        if m:
            code = _to_szsh(m.group("code"))
            item = {
                "code": code,
                "name": m.group("name").strip(),
                "score": float(m.group("score")),
            }
            score_map[code] = item
            if in_final_target:
                final_target_codes.append(code)
            continue

        order_m = ORDER_SUCCESS_RE.search(text)
        if order_m:
            item = {
                "code": _to_szsh(order_m.group("code")),
                "qty": int(order_m.group("qty")),
                "price": float(order_m.group("price")),
                "source": "log_order_success",
            }
            side = order_m.group("side").lower()
            if side == "buy":
                buy_actions.append(item)
            else:
                sell_actions.append(item)
            continue

        buy_m = PACK_BUY_RE.search(text)
        if buy_m:
            buy_actions.append(
                {
                    "code": _to_szsh(buy_m.group("code")),
                    "qty": int(buy_m.group("qty")),
                    "price": float(buy_m.group("price")),
                    "source": "log_pack",
                }
            )
            continue
        sell_m = PACK_SELL_RE.search(text)
        if sell_m:
            sell_actions.append(
                {
                    "code": _to_szsh(sell_m.group("code")),
                    "qty": int(sell_m.group("qty")),
                    "price": float(sell_m.group("price")),
                    "source": "log_pack",
                }
            )

    final_unique = list(dict.fromkeys([code for code in final_target_codes if code]))
    selected_scored: List[Dict[str, object]] = []
    if final_unique:
        for code in final_unique:
            if code in score_map:
                selected_scored.append(score_map[code])
            else:
                selected_scored.append({"code": code, "name": "", "score": None})
    else:
        # fallback: top candidates by score
        selected_scored = sorted(score_map.values(), key=lambda item: float(item.get("score", -9999.0)), reverse=True)[:8]

    return {
        "selected_scored": selected_scored,
        "buy_actions": list(dict.fromkeys((json.dumps(item, ensure_ascii=False, sort_keys=True) for item in buy_actions))),
        "sell_actions": list(dict.fromkeys((json.dumps(item, ensure_ascii=False, sort_keys=True) for item in sell_actions))),
    }


def _dedupe_actions(serialized_items: List[str]) -> List[Dict]:
    rows: List[Dict] = []
    for raw in serialized_items or []:
        try:
            rows.append(json.loads(raw))
        except Exception:
            continue
    return rows


def _format_etf_rows(rows: List[Dict], title: str, limit: int = 10) -> List[str]:
    if not rows:
        return [f"{title}: 0 条"]
    lines = [f"{title}: {len(rows)} 条"]
    for item in rows[:limit]:
        code = _extract_code(item) or "UNKNOWN"
        name = _extract_name(item)
        qty = int(float(item.get("trade_volume", item.get("order_volume", item.get("volume", 0))) or 0))
        price = float(item.get("traded_price", item.get("order_price", item.get("price", 0))) or 0.0)
        status = str(item.get("order_status") or item.get("status") or "").strip()
        suffix = f" 状态={status}" if status else ""
        lines.append(f"- {code}{'(' + name + ')' if name else ''} 数量={qty} 价格={price:.3f}{suffix}")
    return lines


def _is_buy_record(item: Dict) -> bool:
    otype = item.get("order_type")
    direction = str(item.get("direction") or item.get("side") or "").lower()
    text_side = str(item.get("买卖方向") or item.get("bs_flag") or "").lower()
    if direction in {"buy", "b", "买"}:
        return True
    if text_side in {"buy", "b", "买"}:
        return True
    return otype in (23, "23", 1, "1", "buy", "BUY")


def _format_score_rows(rows: List[Dict], title: str, limit: int = 10) -> List[str]:
    if not rows:
        return [f"{title}: 无"]
    lines = [f"{title}: {len(rows)} 只"]
    for item in rows[:limit]:
        code = str(item.get("code", "") or "UNKNOWN")
        name = str(item.get("name", "") or "")
        score = item.get("score")
        score_text = f"{float(score):.4f}" if isinstance(score, (int, float)) else "N/A"
        lines.append(f"- {code}{'(' + name + ')' if name else ''} score={score_text}")
    return lines


def _format_action_rows(rows: List[Dict], title: str, limit: int = 10) -> List[str]:
    merged: Dict[Tuple[str, int, float], Dict] = {}
    for item in rows or []:
        code = str(item.get("code", "") or "UNKNOWN")
        qty = int(float(item.get("qty", item.get("trade_volume", item.get("order_volume", 0))) or 0))
        price = float(item.get("price", item.get("traded_price", item.get("order_price", 0))) or 0)
        key = (code, qty, round(price, 4))
        source = str(item.get("source", "") or "")
        if key not in merged:
            merged[key] = {"code": code, "qty": qty, "price": price, "sources": set()}
        if source:
            merged[key]["sources"].add(source)

    normalized = list(merged.values())
    normalized.sort(key=lambda item: (str(item.get("code", "")), -int(item.get("qty", 0))))
    if not normalized:
        return [f"{title}: 0 条"]
    lines = [f"{title}: {len(normalized)} 条"]
    for item in normalized[:limit]:
        code = str(item.get("code", "") or "UNKNOWN")
        qty = int(item.get("qty", 0) or 0)
        price = float(item.get("price", 0) or 0)
        sources = sorted(str(src) for src in (item.get("sources", set()) or set()) if src)
        suffix = f" 来源={'+'.join(sources)}" if sources else ""
        lines.append(f"- {code} 数量={qty} 价格={price:.3f}{suffix}")
    return lines


def build_guojin_etf_brief(slot_label: str, date_text: str = "") -> str:
    as_of = _normalize_date(date_text)
    orders = [item for item in _fetch_guojin_endpoint("/api/stock/orders") if _is_etf(item)]
    trades = [item for item in _fetch_guojin_endpoint("/api/stock/trades") if _is_etf(item)]
    positions = [item for item in _fetch_guojin_endpoint("/api/stock/positions") if _is_etf(item)]
    orders_buy = [item for item in orders if _is_buy_record(item)]
    orders_sell = [item for item in orders if not _is_buy_record(item)]
    trades_buy = [item for item in trades if _is_buy_record(item)]
    trades_sell = [item for item in trades if not _is_buy_record(item)]

    fetched = _fetch_guojin_log_lines(as_of)
    log_lines = fetched.get("lines", []) if fetched.get("ok") else []
    slot_filtered_lines = _filter_lines_before_slot(log_lines, slot_label=slot_label)
    parsed = _parse_etf_scores_and_actions(slot_filtered_lines)
    selected_scored = parsed.get("selected_scored", []) or []
    buy_actions_log = _dedupe_actions(parsed.get("buy_actions", []) or [])
    sell_actions_log = _dedupe_actions(parsed.get("sell_actions", []) or [])

    lines: List[str] = [f"⏰ {slot_label} 国金ETF交易简报 [{as_of}]"]
    lines.extend(_format_score_rows(selected_scored, "ETF候选/最终打分", limit=12))
    lines.extend(_format_etf_rows(orders_buy, "ETF买入委托", limit=8))
    lines.extend(_format_etf_rows(orders_sell, "ETF卖出委托", limit=8))
    lines.extend(_format_etf_rows(trades_buy, "ETF买入成交", limit=8))
    lines.extend(_format_etf_rows(trades_sell, "ETF卖出成交", limit=8))
    lines.extend(_format_action_rows(buy_actions_log, "日志买入动作", limit=8))
    lines.extend(_format_action_rows(sell_actions_log, "日志卖出动作", limit=8))
    lines.extend(_format_etf_rows(positions, "ETF持仓", limit=8))
    if fetched.get("ok"):
        lines.append(f"国金日志({slot_label}前): lines={len(slot_filtered_lines)}")
    else:
        lines.append(f"国金日志: 获取失败 ({fetched.get('error', 'unknown')})")
    return "\n".join(lines)


def run_scheduled_briefing(slot: str, date_text: str = "") -> str:
    name = str(slot or "").strip().lower()
    effective_date = _effective_trade_date(date_text)
    if name in {"0945", "09:45", "dongguan-0945", "dongguan"}:
        return build_0945_dongguan_brief(date_text=effective_date)
    if name in {"1320", "13:20", "guojin-etf-1320"}:
        return build_guojin_etf_brief(slot_label="13:20", date_text=effective_date)
    if name in {"1420", "14:20", "guojin-etf-1420"}:
        return build_guojin_etf_brief(slot_label="14:20", date_text=effective_date)
    raise ValueError("unsupported slot, use: 0945 | 1320 | 1420")

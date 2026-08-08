#!/usr/bin/env python3
"""Turn the latest closing plan into evidence-backed intraday suggestions.

The monitor never places orders.  It combines the closing review, the latest
portfolio-risk snapshot, and qmt2http quotes so Feishu messages clearly state
whether an intraday recommendation is based on live evidence or only a plan.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from domain.services.risk_report_service import build_risk_report
from domain.policies.advisor_policy import load_advisor_policy, loss_review_evidence
from live_monitor.collectors.qmt_auth import build_qmt_auth_headers

WORKSPACE = Path("/root/.openclaw/workspace")
REPORTS_DIR = WORKSPACE / "reports"
BENCHMARK_CODES = ("000001.SH", "399001.SZ", "399006.SZ", "000300.SH")

TRADING_DIR = str(WORKSPACE / "trading")
if TRADING_DIR not in sys.path:
    sys.path.insert(0, TRADING_DIR)
try:
    from trading_core_new.longterm.trading_calendar import is_cn_trading_day
except Exception:
    is_cn_trading_day = None


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _load_latest_closing_payload() -> Dict[str, Any]:
    path = REPORTS_DIR / "investor_closing_brief_latest.json"
    if not path.exists():
        return {"available": False, "error": "latest_closing_brief_missing"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    if not isinstance(payload, dict):
        return {"available": False, "error": "latest_closing_brief_invalid"}
    payload["available"] = True
    return payload


def _position_key(item: Dict[str, Any]) -> str:
    return str(item.get("code") or item.get("stock_code") or "").strip()


def _position_name(item: Dict[str, Any]) -> str:
    return str(item.get("name") or item.get("stock_name") or _position_key(item)).strip()


def _code_key(value: Any) -> str:
    raw = str(value or "").strip().upper()
    digits = "".join(char for char in raw if char.isdigit())
    return digits[-6:] if len(digits) >= 6 else raw


def _tencent_symbol(code: str) -> str:
    raw = str(code or "").strip().upper()
    digits = _code_key(raw)
    if raw.endswith((".SH", ".XSHG")) or digits.startswith(("5", "6", "9")):
        return f"sh{digits}"
    return f"sz{digits}"


def _benchmark_for(code: str) -> str:
    digits = _code_key(code)
    if digits.startswith("3"):
        return "399006.SZ"
    if digits.startswith(("0", "1", "2")):
        return "399001.SZ"
    return "000001.SH"


def _current_position_map(risk: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    rows = risk.get("positions") or risk.get("top_positions") or []
    return {_code_key(_position_key(item)): item for item in rows if _position_key(item)}


def _tracked_position_seeds(
    plan_positions: List[Dict[str, Any]],
    current_positions: Dict[str, Dict[str, Any]],
    risk: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Prefer current broker inventory and retain plan-only rows only when coverage is uncertain."""
    plan_map = {
        _code_key(_position_key(item)): dict(item)
        for item in plan_positions
        if isinstance(item, dict) and _position_key(item)
    }
    seeds: List[Dict[str, Any]] = []
    for key, current in current_positions.items():
        merged = dict(plan_map.get(key) or {})
        merged.update(current)
        merged["_plan_only"] = False
        seeds.append(merged)

    preserve_plan_only = not risk.get("available") or not risk.get("position_coverage_complete", True)
    if preserve_plan_only:
        for key, planned in plan_map.items():
            if key in current_positions:
                continue
            planned["_plan_only"] = True
            seeds.append(planned)
    return seeds


def _is_trading_session(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    if is_cn_trading_day is not None:
        try:
            if not bool(is_cn_trading_day(now.strftime("%Y-%m-%d"))[0]):
                return False
        except Exception:
            pass
    clock = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= clock <= 11 * 60 + 30 or 13 * 60 <= clock <= 15 * 60


def _quote_payload(code: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    price = _safe_float(raw.get("lastPrice", raw.get("last_price", raw.get("price", raw.get("current_price", 0)))))
    pre_close = _safe_float(raw.get("lastClose", raw.get("pre_close", raw.get("prev_close", 0))))
    change_pct = _safe_float(raw.get("change_percent", raw.get("change_pct", 0)))
    if not change_pct and price and pre_close:
        change_pct = (price - pre_close) / pre_close * 100
    return {
        "code": code,
        "price": round(price, 3),
        "pre_close": round(pre_close, 3),
        "change_pct": round(change_pct, 3),
        "high": round(_safe_float(raw.get("high", 0)), 3),
        "low": round(_safe_float(raw.get("low", 0)), 3),
        "volume": _safe_float(raw.get("volume", 0)),
        "profit_rate": _safe_float(raw.get("profit_rate", 0)),
        "change_available": "change_percent" in raw or "change_pct" in raw or bool(pre_close),
        "source": "qmt_rpc",
        "available": price > 0,
    }


def _fetch_tencent_quotes(codes: List[str]) -> Tuple[Dict[str, Dict[str, Any]], str]:
    symbols = list(dict.fromkeys(_tencent_symbol(code) for code in codes if _code_key(code)))
    if not symbols:
        return {}, "no_codes"
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
        with urllib.request.urlopen(req, timeout=8) as response:
            text = response.read().decode("gb18030", errors="replace")
    except Exception as exc:
        return {}, f"tencent_quote_unavailable ({type(exc).__name__})"

    quotes: Dict[str, Dict[str, Any]] = {}
    for symbol, raw_value in re.findall(r"v_([^=]+)=\"([^\"]*)\"", text):
        fields = raw_value.split("~")
        if len(fields) < 33:
            continue
        code = fields[2].strip() or symbol[2:]
        price = _safe_float(fields[3])
        pre_close = _safe_float(fields[4])
        quote = {
            "code": code,
            "name": fields[1].strip(),
            "price": round(price, 3),
            "pre_close": round(pre_close, 3),
            "change_pct": round(_safe_float(fields[32]), 3),
            "high": round(_safe_float(fields[33]) if len(fields) > 33 else 0, 3),
            "low": round(_safe_float(fields[34]) if len(fields) > 34 else 0, 3),
            "volume": _safe_float(fields[6]),
            "profit_rate": 0.0,
            "change_available": price > 0 and pre_close > 0,
            "source": "tencent_quote",
            "as_of": fields[30].strip() if len(fields) > 30 else "",
            "available": price > 0,
        }
        if quote["available"]:
            quotes[_code_key(code)] = quote
    return quotes, "" if quotes else "tencent_quote_empty"


def _fetch_realtime_quotes(codes: List[str]) -> Tuple[Dict[str, Dict[str, Any]], str]:
    # Production qmt2http exposes authenticated positions but intentionally
    # blocks remote data_fetcher RPC.  Position last_price is therefore the
    # authoritative real-time price source for the holdings we must monitor.
    public_quotes, public_error = _fetch_tencent_quotes(codes)
    timeout = max(1, min(float(os.getenv("DECISION_MONITOR_QMT_TIMEOUT", "5") or 5), 10))
    base_urls = [
        os.getenv("QMT2HTTP_MAIN_URL", "").strip() or os.getenv("QMT2HTTP_BASE_URL", "").strip() or "http://39.105.48.176:8085",
        os.getenv("QMT2HTTP_DONGGUAN_BASE_URL", "").strip() or os.getenv("QMT2HTTP_TRADE_URL", "").strip() or "http://150.158.31.115:8085",
    ]
    quotes: Dict[str, Dict[str, Any]] = dict(public_quotes)
    errors: List[str] = []
    for base_url in list(dict.fromkeys(base_urls)):
        try:
            req = urllib.request.Request(
                f"{base_url.rstrip('/')}/api/stock/positions",
                headers=build_qmt_auth_headers(),
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            rows = payload.get("data") if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                continue
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                code = str(raw.get("stock_code") or raw.get("code") or "").strip()
                quote = _quote_payload(code, raw)
                quote["change_available"] = False
                quote["source"] = "qmt_position"
                if quote["available"] and _code_key(code) not in quotes:
                    quotes[_code_key(code)] = quote
        except Exception as exc:
            errors.append(type(exc).__name__)
    if public_quotes:
        return quotes, ""
    if quotes:
        return quotes, f"public_quote_unavailable; fallback=qmt_position ({public_error})"
    return {}, "qmt_position_quote_unavailable" + (f" ({', '.join(errors)})" if errors else "")


def _fetch_live_cash() -> Tuple[float | None, bool, str]:
    """Aggregate available cash from both QMT accounts for intraday sizing."""
    base_urls = [
        os.getenv("QMT2HTTP_MAIN_URL", "").strip() or os.getenv("QMT2HTTP_BASE_URL", "").strip() or "http://39.105.48.176:8085",
        os.getenv("QMT2HTTP_DONGGUAN_BASE_URL", "").strip() or os.getenv("QMT2HTTP_TRADE_URL", "").strip() or "http://150.158.31.115:8085",
    ]
    total = 0.0
    found = 0
    errors: List[str] = []
    unique_urls = list(dict.fromkeys(base_urls))
    for base_url in unique_urls:
        try:
            req = urllib.request.Request(
                f"{base_url.rstrip('/')}/api/stock/asset",
                headers=build_qmt_auth_headers(),
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            data = payload.get("data") if isinstance(payload, dict) else {}
            if not isinstance(data, dict):
                continue
            cash = _safe_float(data.get("cash", data.get("m_dCash", data.get("available", 0))))
            if cash < 0 or cash >= 10_000_000_000:
                errors.append("invalid_cash_value")
                continue
            total += cash
            found += 1
        except Exception as exc:
            errors.append(type(exc).__name__)
    if found:
        complete = found == len(unique_urls)
        error = "" if complete else f"partial_account_cash {found}/{len(unique_urls)}"
        if errors:
            error += ("; " if error else "") + ", ".join(errors)
        return round(total, 2), complete, error
    return None, False, "qmt_asset_cash_unavailable" + (f" ({', '.join(errors)})" if errors else "")


def _action_for_position(
    position: Dict[str, Any],
    cash_ratio: float | None,
    quote: Dict[str, Any],
    benchmark: Dict[str, Any],
    trading_session: bool,
    quote_fresh: bool,
    policy: Dict[str, Any] | None = None,
) -> Tuple[str, str]:
    active_policy = policy or load_advisor_policy()
    weight = _safe_float(position.get("weight"))
    loss_evidence = loss_review_evidence(position, active_policy)
    loss_ratio = loss_evidence.get("pnl_ratio")
    loss_label = f"{abs(float(loss_ratio)) * 100:.1f}%" if loss_ratio is not None else "待核验"
    source = str(position.get("source") or "")
    if not trading_session:
        if loss_evidence["required"]:
            action = (
                f"非交易时段：累计回撤 {loss_label} 已达到复核门槛；下个交易时段优先核验承接与相对强弱，"
                "当前不执行盘外价格判断，也不生成卖出数量。"
            )
            state = "market_closed_loss_review"
        else:
            action = "非交易时段：保留复盘计划，开盘后再按实时行情确认，不执行盘外价格判断。"
            state = "market_closed"
    elif not quote.get("available"):
        action = "实时行情不可用：不生成价格触发结论，先核验 qmt2http 行情后再操作。"
        state = "quote_missing"
    elif not quote_fresh:
        action = "行情时间戳不是当前交易日：不按旧行情触发交易，等待数据刷新后再判断。"
        state = "quote_stale"
    elif not quote.get("change_available"):
        if weight >= float(active_policy["single_position_prepare_ratio"]):
            action = "交易建议: 减仓候选。已取得实时持仓价，但读口未提供日内涨跌；高集中度仓位按 09:35/10:30 的复盘纪律优先降风险。"
            state = "reduce_candidate_no_intraday_change"
        elif loss_evidence["required"]:
            action = f"交易建议: 不补仓。累计回撤 {loss_label} 已达到复核门槛，但缺少日内涨跌证据；反弹无量仍以减仓修复组合为先。"
            state = "hold_or_reduce_no_intraday_change"
        else:
            action = "交易建议: 实时价格已核验，暂不触发价格阈值；等待下一次盘中复核。"
            state = "observe_no_intraday_change"
    else:
        change_pct = _safe_float(quote.get("change_pct"))
        relative_change = change_pct - _safe_float(benchmark.get("change_pct"))
        if change_pct <= -3 or relative_change <= -2:
            action = "交易建议: 减仓优先。日内表现明显偏弱（跌幅超过 3% 或较市场低 2pct），不补仓，先降风险。"
            state = "reduce_priority"
        elif weight >= float(active_policy["single_position_prepare_ratio"]) and (change_pct < -1 or relative_change < -1):
            action = (
                "交易建议: 减仓候选。高集中持仓弱于昨收或弱于市场，若 30 分钟内不能修复，"
                f"优先降权到 {float(active_policy['single_position_reduce_target_ratio']):.0%} 以下。"
            )
            state = "reduce_candidate"
        elif change_pct >= 3:
            action = "交易建议: 不追涨。等待回踩承接或量价继续确认，已有仓位可分批锁定部分浮盈。"
            state = "no_chasing"
        elif loss_evidence["required"]:
            action = f"交易建议: 不补仓。累计回撤 {loss_label} 已达到复核门槛；只有价格转强且承接稳定时才保留，反弹无量仍以减仓修复组合为先。"
            state = "hold_or_reduce"
        elif source == "trade":
            action = "交易建议: 按交易仓处理。高开不追；跌破昨收且无承接时优先降低风险。"
            state = "trade_position_watch"
        else:
            action = "交易建议: 继续观察。价格未触发风险阈值，等待走势与市场方向进一步确认。"
            state = "observe"
    if cash_ratio is not None and cash_ratio < float(active_policy["minimum_cash_ratio"]):
        action += " 当前现金不足，新机会只能通过减仓腾挪资金。"
    return state, action


def _reduce_execution_hint(position: Dict[str, Any], state: str, policy: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Convert a risk state into a broker-evidence-backed quantity reference."""
    active_policy = policy or load_advisor_policy()
    weight = _safe_float(position.get("weight"))
    volume = int(_safe_float(position.get("volume")))
    if state in {"reduce_priority", "reduce_candidate", "reduce_candidate_no_intraday_change"}:
        target_weight = float(active_policy["single_position_reduce_target_ratio"])
    elif state in {"hold_or_reduce", "hold_or_reduce_no_intraday_change"}:
        target_weight = float(active_policy["loss_position_reduce_target_ratio"])
    else:
        return {"actionable": False, "target_weight": None, "suggested_qty": 0, "note": ""}
    if weight <= target_weight:
        return {"actionable": False, "target_weight": target_weight, "suggested_qty": 0, "note": "仓位已在目标范围内。"}
    sources = list(position.get("sources") or [])
    if len(sources) > 1 or str(position.get("source") or "") == "combined":
        return {
            "actionable": False,
            "target_weight": target_weight,
            "suggested_qty": 0,
            "note": "同一证券分布在多个账户，需先分账户核对可用数量，不能按合计股数生成卖出数量。",
        }
    if volume <= 0:
        return {"actionable": False, "target_weight": target_weight, "suggested_qty": 0, "note": "缺少总持仓数量，不能生成下单数量。"}
    if position.get("stale_sources"):
        return {
            "actionable": False,
            "target_weight": target_weight,
            "suggested_qty": 0,
            "note": "持仓来自过期账户回退，只能核验风险方向，不能生成当前卖出数量。",
        }
    if not bool(position.get("available_volume_complete")):
        return {
            "actionable": False,
            "target_weight": target_weight,
            "suggested_qty": 0,
            "note": "券商未返回完整可用股数，只能核验，不能把总持仓当作可卖数量。",
        }
    available_volume = int(_safe_float(position.get("available_volume")))
    if available_volume < 0 or available_volume > volume:
        return {
            "actionable": False,
            "target_weight": target_weight,
            "suggested_qty": 0,
            "note": "券商可用股数与总持仓不一致，只能先核验账户数据。",
        }
    if available_volume <= 0:
        return {
            "actionable": False,
            "target_weight": target_weight,
            "suggested_qty": 0,
            "note": "当前可用股数为 0（可能受当日买入或冻结影响），不能生成卖出数量。",
        }
    raw_qty = volume * (weight - target_weight) / weight
    if raw_qty < 100:
        return {
            "actionable": False,
            "target_weight": target_weight,
            "suggested_qty": 0,
            "available_volume": available_volume,
            "note": f"理论减仓约 {raw_qty:.0f} 股，不足 100 股整手；为避免过度调整，暂不生成卖出数量。",
        }
    required_qty = min(volume, int(math.ceil(raw_qty / 100.0) * 100))
    executable_cap = int(available_volume // 100) * 100
    suggested_qty = min(required_qty, executable_cap)
    if suggested_qty <= 0:
        return {
            "actionable": False,
            "target_weight": target_weight,
            "suggested_qty": 0,
            "required_qty": required_qty,
            "available_volume": available_volume,
            "note": f"理论需减仓约 {raw_qty:.0f} 股，但当前可用 {available_volume} 股不足 100 股整手，暂不生成卖出数量。",
        }
    target_reachable = suggested_qty >= required_qty
    market_value = _safe_float(position.get("market_value"))
    implied_price = market_value / volume if market_value > 0 and volume > 0 else 0.0
    estimated_notional = round(suggested_qty * implied_price, 2) if implied_price > 0 else None
    if target_reachable:
        note = f"建议减仓 {suggested_qty} 股，按当前仓位估算可降至 {target_weight:.0%} 附近或以下。"
    else:
        note = (
            f"当前最多先减仓 {suggested_qty} 股；达到 {target_weight:.0%} 目标约需 {required_qty} 股，"
            "剩余数量待冻结解除或账户更新后再核验。"
        )
    if estimated_notional is not None:
        note += f" 快照估算成交额约 {estimated_notional:,.0f} 元。"
    note += " 未配置券商实际费率，未计佣金、印花税和滑点。"
    return {
        "actionable": True,
        "target_weight": target_weight,
        "suggested_qty": suggested_qty,
        "required_qty": required_qty,
        "available_volume": available_volume,
        "target_reachable": target_reachable,
        "estimated_notional": estimated_notional,
        "transaction_cost_estimated": False,
        "note": note,
    }


def _action_level(
    state: str,
    trading_session: bool,
    quote_fresh: bool,
    execution_hint: Dict[str, Any],
) -> str:
    """Map evidence quality to an explicit advisor action tier."""
    if not trading_session or state in {"market_closed", "quote_missing", "quote_stale"}:
        return "observe"
    if state in {"reduce_priority", "reduce_candidate"}:
        return "prepare" if quote_fresh and execution_hint.get("actionable") else "verify"
    if state in {"reduce_candidate_no_intraday_change", "hold_or_reduce_no_intraday_change", "hold_or_reduce"}:
        return "verify"
    return "observe"


def build_decision_monitor(slot: str = "") -> Dict[str, Any]:
    now = datetime.now()
    closing = _load_latest_closing_payload()
    decision_plan = closing.get("decision_plan") or {}
    risk = build_risk_report()
    policy = load_advisor_policy()
    current_positions = _current_position_map(risk if risk.get("available") else {})
    trading_session = _is_trading_session(now)
    if trading_session:
        live_cash, live_cash_complete, cash_error = _fetch_live_cash()
    else:
        live_cash, live_cash_complete, cash_error = None, False, "非交易时段未请求实时现金"
    cash = _safe_float(risk.get("cash", 0)) if live_cash is None else live_cash
    total_market_value = _safe_float(risk.get("total_market_value", 0))
    risk_cash_complete = bool(risk.get("cash_complete", bool(risk.get("available")) and "cash_complete" not in risk))
    cash_complete = live_cash_complete if live_cash is not None else risk_cash_complete
    if cash_complete:
        cash_ratio = cash / (total_market_value + cash) if total_market_value + cash > 0 else _safe_float(
            risk.get("cash_ratio", decision_plan.get("cash_ratio", 0))
        )
    else:
        cash_ratio = None
    plan_positions = [item for item in decision_plan.get("positions") or [] if isinstance(item, dict)]
    tracked_seed = _tracked_position_seeds(plan_positions, current_positions, risk)
    quote_codes = [_position_key(item) for item in tracked_seed] + list(BENCHMARK_CODES)
    if trading_session:
        quotes, quote_error = _fetch_realtime_quotes(quote_codes)
    else:
        quotes, quote_error = {}, "非交易时段未请求实时行情"
    if is_cn_trading_day is not None:
        try:
            calendar_open, calendar_source = is_cn_trading_day(now.strftime("%Y-%m-%d"))
        except Exception:
            calendar_open, calendar_source = now.weekday() < 5, "calendar_error_fallback"
    else:
        calendar_open, calendar_source = now.weekday() < 5, "weekday_fallback"
    tracked: List[Dict[str, Any]] = []
    for planned in tracked_seed:
        code = _position_key(planned)
        current_confirmed = _code_key(code) in current_positions and not planned.get("_plan_only")
        current = current_positions.get(_code_key(code), planned)
        quote = quotes.get(_code_key(code), {})
        benchmark_code = _benchmark_for(code)
        benchmark = quotes.get(_code_key(benchmark_code), {})
        quote_as_of = str(quote.get("as_of") or "")
        quote_fresh = bool(quote_as_of.startswith(now.strftime("%Y%m%d"))) if quote_as_of else not trading_session
        if current_confirmed:
            state, suggestion = _action_for_position(current, cash_ratio, quote, benchmark, trading_session, quote_fresh, policy=policy)
            execution_hint = _reduce_execution_hint(current, state, policy=policy)
            action_level = _action_level(state, trading_session, quote_fresh, execution_hint)
        else:
            state = "position_unconfirmed"
            suggestion = "账户持仓覆盖不完整；该证券仅来自上一交易日计划，先核验是否仍持有及当前可用数量。"
            execution_hint = {
                "actionable": False,
                "target_weight": None,
                "suggested_qty": 0,
                "note": "未由当前持仓证据确认，不能生成交易数量。",
            }
            action_level = "verify" if trading_session else "observe"
        tracked.append(
            {
                "code": code,
                "name": _position_name(current),
                "source": str(current.get("source") or planned.get("source") or ""),
                "weight": _safe_float(current.get("weight", planned.get("weight"))),
                "volume": int(_safe_float(current.get("volume", planned.get("volume", 0)))),
                "available_volume": int(_safe_float(current.get("available_volume", 0))),
                "available_volume_complete": bool(current.get("available_volume_complete")),
                "pnl": _safe_float(current.get("pnl", planned.get("pnl"))),
                "pnl_ratio": current.get("pnl_ratio", planned.get("pnl_ratio")),
                "loss_review": loss_review_evidence(current, policy=policy),
                "status": "current" if current_confirmed else "from_plan_unconfirmed",
                "quote": quote,
                "benchmark_code": benchmark_code,
                "relative_change_pct": round(_safe_float(quote.get("change_pct")) - _safe_float(benchmark.get("change_pct")), 3) if quote.get("change_available") and benchmark.get("change_available") else None,
                "decision_state": state,
                "action_level": action_level,
                "suggestion": suggestion,
                "execution_hint": execution_hint,
            }
        )
    weighted_changes = [
        (_safe_float(item.get("weight")), _safe_float((item.get("quote") or {}).get("change_pct")))
        for item in tracked
        if (item.get("quote") or {}).get("change_available") and _safe_float(item.get("weight")) > 0
    ]
    total_weight = sum(weight for weight, _ in weighted_changes)
    portfolio_change = sum(weight * change for weight, change in weighted_changes) / total_weight if total_weight else None
    csi300 = quotes.get(_code_key("000300.SH"), {})
    csi300_change = _safe_float(csi300.get("change_pct")) if csi300.get("change_available") else None
    portfolio_benchmark = {
        "basis": "intraday_weighted_holdings_vs_csi300",
        "portfolio_change_pct": round(portfolio_change, 3) if portfolio_change is not None else None,
        "csi300_change_pct": round(csi300_change, 3) if csi300_change is not None else None,
        "relative_change_pct": round(portfolio_change - csi300_change, 3) if portfolio_change is not None and csi300_change is not None else None,
        "covered_weight": round(total_weight, 4),
        "as_of": csi300.get("as_of", ""),
    }
    return {
        "available": bool(closing.get("available") or risk.get("available")),
        "plan_available": bool(closing.get("available")),
        "slot": str(slot or ""),
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "trading_session": trading_session,
        "calendar_open": bool(calendar_open),
        "calendar_source": str(calendar_source),
        "plan_date": closing.get("date", ""),
        "plan_generated_at": closing.get("generated_at", ""),
        "closing_error": closing.get("error", ""),
        "risk_available": bool(risk.get("available")),
        "position_coverage_complete": bool(risk.get("position_coverage_complete", bool(risk.get("available")))),
        "risk_as_of": risk.get("as_of", ""),
        "cash_ratio": cash_ratio,
        "cash": cash,
        "cash_complete": cash_complete,
        "cash_source": "qmt_dual_account" if live_cash is not None else "portfolio_snapshot",
        "cash_error": cash_error,
        "top1_ratio": _safe_float(risk.get("top1_ratio", decision_plan.get("top1_ratio", 0))),
        "top3_ratio": _safe_float(risk.get("top3_ratio", decision_plan.get("top3_ratio", 0))),
        "risk_flags": risk.get("risk_flags") or decision_plan.get("risk_flags") or [],
        "advisor_policy": policy,
        "quote_available_count": len(quotes),
        "quote_error": quote_error,
        "benchmarks": {code: quotes.get(_code_key(code), {}) for code in BENCHMARK_CODES},
        "portfolio_benchmark": portfolio_benchmark,
        "opportunities": decision_plan.get("opportunities") or [],
        "opening_triggers": decision_plan.get("opening_triggers") or [],
        "tracked_positions": tracked,
    }


def save_decision_monitor(monitor: Dict[str, Any]) -> str:
    """Persist a scheduled decision snapshot for end-of-day execution attribution."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slot = re.sub(r"[^A-Za-z0-9]+", "_", str(monitor.get("slot") or "intraday")).strip("_") or "intraday"
    path = REPORTS_DIR / f"investor_decision_monitor_{stamp}_{slot}.json"
    monitor["snapshot_path"] = str(path)
    payload = json.dumps(monitor, ensure_ascii=False, indent=2, default=str)
    path.write_text(payload, encoding="utf-8")
    (REPORTS_DIR / "investor_decision_monitor_latest.json").write_text(payload, encoding="utf-8")
    return str(path)


def _format_decision_monitor_text_legacy(slot: str = "", max_opportunities: int = 5, max_positions: int = 8, save: bool = False) -> str:
    monitor = build_decision_monitor(slot=slot)
    if save:
        monitor["snapshot_path"] = save_decision_monitor(monitor)
    if not monitor.get("available"):
        return f"复盘闭环交易建议: 不可用 ({monitor.get('closing_error') or 'no closing plan'})"
    slot_text = str(slot or "盘中").strip()
    quote_state = "盘中实时行情" if monitor.get("trading_session") else "非交易时段（仅复盘计划）"
    cash_ratio = monitor.get("cash_ratio")
    cash_text = (
        f"现金={monitor.get('cash', 0):,.2f} ({float(cash_ratio) * 100:.1f}%, {monitor.get('cash_source')})"
        if monitor.get("cash_complete") and cash_ratio is not None
        else f"可验证现金={monitor.get('cash', 0):,.2f} (部分账户现金不可验证, {monitor.get('cash_source')})"
    )
    lines = [
        f"复盘闭环交易建议 [{slot_text}]",
        (
            f"计划日期={monitor.get('plan_date') or 'unknown'} 风险快照={monitor.get('risk_as_of') or 'unknown'} "
            f"{cash_text} "
            f"top1={monitor.get('top1_ratio', 0) * 100:.1f}% "
            f"top3={monitor.get('top3_ratio', 0) * 100:.1f}%"
        ),
        f"行情证据: {quote_state}; 可用行情={monitor.get('quote_available_count', 0)}",
        f"交易日历: open={monitor.get('calendar_open')} source={monitor.get('calendar_source')}",
    ]
    if monitor.get("quote_error"):
        lines.append(f"行情限制: {monitor['quote_error']}")
    if monitor.get("cash_error"):
        lines.append(f"现金限制: {monitor['cash_error']}")
    benchmark_parts = []
    for code, quote in (monitor.get("benchmarks") or {}).items():
        if quote.get("available") and quote.get("change_available"):
            benchmark_parts.append(f"{code} {quote.get('change_pct', 0):+.2f}%")
    if benchmark_parts:
        lines.append("市场参照: " + " | ".join(benchmark_parts))
    flags = monitor.get("risk_flags") or []
    if flags:
        lines.append("风险约束: " + ", ".join(str(item) for item in flags))
    lines.append("机会执行:")
    opportunities = monitor.get("opportunities") or []
    lines.extend((str(item) for item in opportunities[:max_opportunities]) or ["- 暂无明确事件驱动机会，先处理持仓风险。"])
    lines.append("持仓监控与建议:")
    positions = monitor.get("tracked_positions") or []
    if positions:
        for item in positions[:max_positions]:
            quote = item.get("quote") or {}
            quote_text = "现价=未取得"
            if quote.get("available"):
                quote_text = f"现价={quote.get('price', 0):.3f}"
                if quote.get("change_available"):
                    quote_text += f" 日内涨跌={quote.get('change_pct', 0):+.2f}%"
                    if item.get("relative_change_pct") is not None:
                        quote_text += f" 相对{item.get('benchmark_code')}={item['relative_change_pct']:+.2f}pct"
                else:
                    quote_text += f" 持仓盈亏={quote.get('profit_rate', 0) * 100:+.2f}%"
            lines.append(
                f"- {item.get('code')} {item.get('name')}: {quote_text}; "
                f"仓位={item.get('weight', 0) * 100:.1f}% 行动层级={item.get('action_level', 'observe')} "
                f"状态={item.get('decision_state')}; {item.get('suggestion')}"
            )
            hint = item.get("execution_hint") or {}
            if hint.get("note"):
                lines.append(f"  执行数量: {hint['note']}")
    else:
        lines.append("- 暂无持仓计划，先检查 /风险 是否可用。")
    triggers = monitor.get("opening_triggers") or []
    if triggers:
        lines.append("复盘触发条件:")
        lines.extend(str(item) for item in triggers[:5])
    lines.append("说明: 以上为监控和交易建议，不会自动下单。")
    return "\n".join(lines)


def format_decision_monitor_text(slot: str = "", max_opportunities: int = 3, max_positions: int = 8, save: bool = False) -> str:
    """Render a concise Feishu-facing decision card without operational noise."""
    from domain.services.report_style_service import join_cn, money, pct, risk_label, source_label

    monitor = build_decision_monitor(slot=slot)
    if save:
        monitor["snapshot_path"] = save_decision_monitor(monitor)
    if not monitor.get("available"):
        return "盘中决策检查失败：缺少可用的上一交易日复盘计划，本次不生成交易建议。"

    session = "交易时段" if monitor.get("trading_session") else "非交易时段"
    slot_text = str(slot or "").strip()
    if slot_text == "Feishu 查询":
        display_title = "持仓风险与交易建议"
    elif slot_text == "交易监控":
        display_title = "交易监控持仓建议"
    elif "检查" in slot_text:
        display_title = slot_text
    else:
        display_title = f"{slot_text or '盘中'} 决策检查"
    lines = [
        f"⏱️ {display_title}",
        f"数据时间：{monitor.get('generated_at')}｜{session}",
        "",
        "**核心结论**",
    ]
    positions = monitor.get("tracked_positions") or []
    prepared = [item for item in positions if item.get("action_level") == "prepare"]
    verify = [item for item in positions if item.get("action_level") == "verify"]
    loss_review_positions = [item for item in positions if (item.get("loss_review") or {}).get("required")]
    if prepared:
        lines.append(f"- {len(prepared)} 只持仓进入“准备”层级；可生成降风险数量参考，但仍须人工确认后执行。")
    elif verify:
        lines.append(f"- {len(verify)} 只持仓进入“核验”层级；证据或可执行数量尚不完整，不生成执行候选。")
    elif not monitor.get("trading_session") and loss_review_positions:
        lines.append(
            f"- {len(loss_review_positions)} 只持仓的累计回撤达到复核门槛；已列为下个交易时段优先核验，"
            "当前盘外不生成价格触发或卖出数量。"
        )
    else:
        wait_text = "等待交易时段再核验" if not monitor.get("trading_session") else "继续按盘前纪律观察"
        lines.append(f"- 当前没有持仓触发可执行数量；{wait_text}，不自动下单。")
    if monitor.get("cash_complete") and monitor.get("cash_ratio") is not None:
        cash_summary = f"现金 {money(monitor.get('cash'))}（{pct(float(monitor.get('cash_ratio')) * 100)}）"
    else:
        cash_summary = f"可验证现金 {money(monitor.get('cash'))}（部分账户不可验证，不计算完整现金占比）"
    lines.append(
        f"- {cash_summary}；第一大持仓 {pct(monitor.get('top1_ratio', 0) * 100)}，"
        f"前三大持仓 {pct(monitor.get('top3_ratio', 0) * 100)}。"
    )
    flags = [risk_label(item) for item in monitor.get("risk_flags") or []]
    if flags:
        lines.append("- 风险约束：" + join_cn(flags) + "。")

    lines.extend(["", "**持仓检查**"])
    if not positions:
        lines.append("- 没有可核验的持仓计划。")
    for item in positions[:max_positions]:
        quote = item.get("quote") or {}
        if quote.get("available"):
            quote_text = ("现价" if monitor.get("trading_session") else "最近快照") + f" {quote.get('price', 0):.3f}"
            if quote.get("change_available") and monitor.get("trading_session"):
                quote_text += f" / 日内 {quote.get('change_pct', 0):+.2f}%"
        else:
            quote_text = "实时行情不可用"
        level_label = {"observe": "观察", "verify": "核验", "prepare": "准备"}.get(
            str(item.get("action_level") or "observe"),
            "观察",
        )
        lines.append(
            f"- **{item.get('name')}（{item.get('code')}）**｜行动 {level_label}｜仓位 {pct(item.get('weight', 0) * 100)}｜"
            f"{quote_text}｜{source_label(item.get('source'))}"
        )
        loss_review = item.get("loss_review") or {}
        pnl_ratio = loss_review.get("pnl_ratio")
        if pnl_ratio is not None:
            review_label = "优先" if loss_review.get("required") else "常规"
            pnl_pct = float(pnl_ratio) * 100
            pnl_text = pct(pnl_pct, digits=2 if abs(pnl_pct) < 0.1 else 1, signed=True)
            severity_note = "（成本噪声）" if loss_review.get("severity") == "noise" else ""
            lines.append(f"  成本风险：累计盈亏 {pnl_text}{severity_note}｜下次复核 {review_label}。")
        lines.append(f"  建议：{item.get('suggestion') or '等待有效行情后再判断。'}")
        hint = item.get("execution_hint") or {}
        if item.get("action_level") == "prepare" and hint.get("actionable") and hint.get("note"):
            lines.append(f"  数量参考：{hint.get('note')}（执行前核对可用股数）")

    opportunities = monitor.get("opportunities") or []
    if opportunities:
        lines.extend(["", "**机会验证**"])
        lines.extend(str(item) for item in opportunities[:max_opportunities])

    lines.extend(["", "**数据可信度**"])
    if not monitor.get("plan_available"):
        lines.append("- 上一交易日收盘计划不可用；当前仅按可验证持仓检查风险，不生成依赖旧计划的机会结论。")
    if not monitor.get("position_coverage_complete"):
        lines.append("- 当前持仓覆盖不完整；仅来自上一交易日计划的证券已标为待核验，不会生成交易数量。")
    if not monitor.get("trading_session"):
        lines.append("- 当前为非交易时段；报价仅作最近快照展示，不称为实时行情，不生成价格触发结论。")
    elif monitor.get("quote_error"):
        lines.append("- 实时行情链路降级；缺失行情的标的不生成价格触发结论。")
    else:
        lines.append(f"- 已取得 {monitor.get('quote_available_count', 0)} 条实时行情，建议仍需结合成交量确认。")
    if not monitor.get("cash_complete"):
        lines.append("- 部分账户现金不可验证；不据此判断资金是否充足，也不触发“现金不足”的交易建议。")
    elif monitor.get("cash_error"):
        if monitor.get("trading_session"):
            lines.append("- 实时现金读取失败，现金比例退回组合快照口径。")
        else:
            lines.append("- 非交易时段未请求实时现金，现金比例采用最近组合快照。")
    lines.append("- 行动层级：观察＝等待证据；核验＝补齐行情或数量；准备＝可形成数量参考，仍需人工确认。")
    lines.append("- 本报告只提供监控建议，不会自动下单。")
    return "\n".join(lines)

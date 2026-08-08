#!/usr/bin/env python3
"""Evaluate whether explicit intraday risk advice received closing-price confirmation."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List


WORKSPACE = Path("/root/.openclaw/workspace")
REPORTS_DIR = WORKSPACE / "reports"
RISK_STATES = {
    "reduce_priority",
    "reduce_candidate",
    "reduce_candidate_no_intraday_change",
    "hold_or_reduce",
    "hold_or_reduce_no_intraday_change",
}
CONFIRM_THRESHOLD_PCT = 0.5


def _as_date(value: Any) -> str:
    return str(value or "").strip()[:10]


def _code_key(value: Any) -> str:
    raw = str(value or "").strip().upper()
    digits = "".join(char for char in raw if char.isdigit())
    return digits[-6:] if len(digits) >= 6 else raw


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_decision_snapshots(as_of: str, reports_dir: Path = REPORTS_DIR) -> List[Dict[str, Any]]:
    """Load every independently saved snapshot for one day, with latest as fallback."""
    compact = _as_date(as_of).replace("-", "")
    candidates = sorted(reports_dir.glob(f"investor_decision_monitor_{compact}_*.json"))
    latest = reports_dir / "investor_decision_monitor_latest.json"
    if latest.exists():
        candidates.append(latest)
    snapshots: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in candidates:
        payload = _load_json(path)
        generated_at = str(payload.get("generated_at") or "")
        if not generated_at.startswith(_as_date(as_of)):
            continue
        identity = (generated_at, str(payload.get("slot") or ""))
        if identity in seen:
            continue
        seen.add(identity)
        snapshots.append(payload)
    snapshots.sort(key=lambda item: str(item.get("generated_at") or ""))
    return snapshots


def _tencent_symbol(code: str) -> str:
    raw = str(code or "").strip().lower()
    if raw.startswith(("sh", "sz")):
        return raw
    digits = _code_key(raw)
    exchange = "sh" if raw.upper().endswith(".SH") or digits.startswith(("5", "6", "9")) else "sz"
    return f"{exchange}{digits}"


def _fetch_tencent_kline(code: str, start: str, end: str) -> List[Dict[str, Any]]:
    symbol = _tencent_symbol(code)
    # Signal prices are realtime unadjusted prices. Historical closes must use
    # the same basis; forward-adjusted series can be rewritten by later actions.
    param = f"{symbol},day,{start},{end},320,none"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urllib.parse.urlencode({"param": param})
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return []
    block = ((payload.get("data") or {}).get(symbol) or {}) if isinstance(payload, dict) else {}
    raw_rows = block.get("day") or []
    rows = []
    for raw in raw_rows:
        if not isinstance(raw, list) or len(raw) < 5:
            continue
        row_date = str(raw[0])
        if row_date < start or row_date > end:
            continue
        try:
            rows.append(
                {
                    "date": row_date,
                    "open": float(raw[1]),
                    "close": float(raw[2]),
                    "high": float(raw[3]),
                    "low": float(raw[4]),
                    "volume": float(raw[5]) if len(raw) > 5 else 0.0,
                    "source": "tencent_kline",
                }
            )
        except (TypeError, ValueError):
            continue
    return rows


def _default_price_loader(code: str, start: str, end: str) -> List[Dict[str, Any]]:
    rows = _fetch_tencent_kline(code, start, end)
    if rows:
        return rows
    from data_collector import fetch_historical_kline

    return list(fetch_historical_kline(code, start, end) or [])


def _closing_price(
    code: str,
    as_of: str,
    loader: Callable[[str, str, str], Iterable[Dict[str, Any]]],
    cache: Dict[tuple[str, str], tuple[float | None, str]],
) -> tuple[float | None, str]:
    key = (_code_key(code), as_of)
    if key in cache:
        return cache[key]
    try:
        rows = list(loader(code, as_of, as_of) or [])
    except Exception:
        rows = []
    close = None
    source = ""
    for row in rows:
        if _as_date(row.get("date")) != as_of:
            continue
        try:
            value = float(row.get("close") or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            close = value
            source = str(row.get("source") or "historical_price_loader")
            break
    cache[key] = (close, source)
    return close, source


def _return_pct(signal: float, close: float | None) -> float | None:
    if signal <= 0 or close is None or close <= 0:
        return None
    return round((close / signal - 1.0) * 100.0, 3)


def _verdict(stock_return: float | None, relative_return: float | None) -> str:
    evidence = [value for value in (stock_return, relative_return) if value is not None]
    if not evidence:
        return "unavailable"
    if any(value <= -CONFIRM_THRESHOLD_PCT for value in evidence):
        return "downside_confirmed"
    if stock_return is not None and stock_return >= CONFIRM_THRESHOLD_PCT and (
        relative_return is None or relative_return >= CONFIRM_THRESHOLD_PCT
    ):
        return "downside_not_confirmed"
    return "mixed"


def build_decision_outcomes(
    as_of: str,
    reports_dir: Path = REPORTS_DIR,
    price_loader: Callable[[str, str, str], Iterable[Dict[str, Any]]] | None = None,
) -> Dict[str, Any]:
    """Evaluate all explicit downside-risk snapshots from one completed session."""
    day = _as_date(as_of)
    loader = price_loader or _default_price_loader
    snapshots = load_decision_snapshots(day, reports_dir=reports_dir)
    cache: Dict[tuple[str, str], tuple[float | None, str]] = {}
    outcomes: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for snapshot in snapshots:
        generated_at = str(snapshot.get("generated_at") or "")
        for item in snapshot.get("tracked_positions") or []:
            state = str(item.get("decision_state") or "")
            code = str(item.get("code") or "")
            if state not in RISK_STATES or not code:
                continue
            identity = (generated_at, _code_key(code), state)
            if identity in seen:
                continue
            seen.add(identity)
            quote = item.get("quote") or {}
            quote_as_of = str(quote.get("as_of") or "")
            try:
                signal_price = float(quote.get("price") or 0)
            except (TypeError, ValueError):
                signal_price = 0
            quote_day = f"{quote_as_of[:4]}-{quote_as_of[4:6]}-{quote_as_of[6:8]}" if len(quote_as_of) >= 8 else ""
            close_price, close_source = (
                _closing_price(code, day, loader, cache) if quote_day == day and signal_price > 0 else (None, "")
            )
            stock_return = _return_pct(signal_price, close_price)

            benchmark_code = str(item.get("benchmark_code") or "")
            benchmark_quote = (snapshot.get("benchmarks") or {}).get(benchmark_code) or {}
            try:
                benchmark_signal = float(benchmark_quote.get("price") or 0)
            except (TypeError, ValueError):
                benchmark_signal = 0
            benchmark_as_of = str(benchmark_quote.get("as_of") or "")
            benchmark_day = (
                f"{benchmark_as_of[:4]}-{benchmark_as_of[4:6]}-{benchmark_as_of[6:8]}"
                if len(benchmark_as_of) >= 8
                else ""
            )
            benchmark_close, benchmark_close_source = (
                _closing_price(benchmark_code, day, loader, cache)
                if benchmark_code and benchmark_day == day and benchmark_signal > 0
                else (None, "")
            )
            benchmark_return = _return_pct(benchmark_signal, benchmark_close)
            relative_return = (
                round(stock_return - benchmark_return, 3)
                if stock_return is not None and benchmark_return is not None
                else None
            )
            outcomes.append(
                {
                    "as_of": day,
                    "generated_at": generated_at,
                    "slot": snapshot.get("slot") or "",
                    "code": code,
                    "name": item.get("name") or code,
                    "decision_state": state,
                    "action_level": item.get("action_level") or "legacy_unclassified",
                    "evidence_profile": "tiered_action" if item.get("action_level") else "legacy_unclassified",
                    "signal_price": signal_price or None,
                    "close_price": close_price,
                    "close_source": close_source,
                    "stock_return_pct": stock_return,
                    "benchmark_code": benchmark_code,
                    "benchmark_return_pct": benchmark_return,
                    "benchmark_close_source": benchmark_close_source,
                    "relative_return_pct": relative_return,
                    "verdict": _verdict(stock_return, relative_return),
                }
            )

    counts = {key: 0 for key in ("downside_confirmed", "downside_not_confirmed", "mixed", "unavailable")}
    for item in outcomes:
        counts[str(item["verdict"])] += 1
    evaluated = len(outcomes) - counts["unavailable"]
    profiled = [item for item in outcomes if item.get("evidence_profile") == "tiered_action" and item.get("verdict") != "unavailable"]
    legacy = [item for item in outcomes if item.get("evidence_profile") == "legacy_unclassified" and item.get("verdict") != "unavailable"]
    return {
        "schema_version": 1,
        "as_of": day,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot_count": len(snapshots),
        "directional_count": len(outcomes),
        "evaluated_count": evaluated,
        "confirmed_count": counts["downside_confirmed"],
        "not_confirmed_count": counts["downside_not_confirmed"],
        "mixed_count": counts["mixed"],
        "unavailable_count": counts["unavailable"],
        "profiled_evaluated_count": len(profiled),
        "profiled_confirmed_count": sum(item.get("verdict") == "downside_confirmed" for item in profiled),
        "profiled_not_confirmed_count": sum(item.get("verdict") == "downside_not_confirmed" for item in profiled),
        "profiled_mixed_count": sum(item.get("verdict") == "mixed" for item in profiled),
        "legacy_evaluated_count": len(legacy),
        "outcomes": outcomes,
        "boundary": "仅验证建议后至收盘的价格方向，不代表实际成交、收益或长期策略有效性。",
    }


def save_decision_outcomes(summary: Dict[str, Any], reports_dir: Path = REPORTS_DIR) -> str:
    reports_dir.mkdir(parents=True, exist_ok=True)
    compact = _as_date(summary.get("as_of")).replace("-", "")
    path = reports_dir / f"investor_decision_outcome_{compact}.json"
    payload = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    path.write_text(payload, encoding="utf-8")
    (reports_dir / "investor_decision_outcome_latest.json").write_text(payload, encoding="utf-8")
    return str(path)


def recent_outcome_summary(as_of: date, days: int = 7, reports_dir: Path = REPORTS_DIR) -> Dict[str, Any]:
    rows = []
    for offset in range(max(1, days)):
        day = as_of - timedelta(days=offset)
        path = reports_dir / f"investor_decision_outcome_{day.strftime('%Y%m%d')}.json"
        payload = _load_json(path)
        if payload:
            rows.append(payload)
    totals = {
        key: sum(int(item.get(key, 0) or 0) for item in rows)
        for key in (
            "evaluated_count",
            "confirmed_count",
            "not_confirmed_count",
            "mixed_count",
            "unavailable_count",
            "profiled_evaluated_count",
            "profiled_confirmed_count",
            "profiled_not_confirmed_count",
            "profiled_mixed_count",
            "legacy_evaluated_count",
        )
    }
    return {"days": days, "sessions": len(rows), **totals}

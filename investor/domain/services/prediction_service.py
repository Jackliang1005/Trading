#!/usr/bin/env python3
"""Prediction domain service (phase 1 extraction)."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Sequence

import db
from data_collector import fetch_market_quotes
from domain.repository import get_analysis_context_repository
from domain.services.evolution_service import generate_system_prompt
from knowledge_base import build_few_shot_prompt, build_rag_context

PREDICTION_TARGETS = [
    {"code": "sh000001", "name": "上证指数"},
    {"code": "sz399001", "name": "深证成指"},
    {"code": "sz399006", "name": "创业板指"},
]

# 最大持仓预测数（避免 prompt 过长）
MAX_POSITION_PREDICTION_TARGETS = 15
PREDICTION_SOURCE_PRICE_TOLERANCE = 0.05


def get_position_prediction_targets() -> list:
    """从 portfolio_snapshot 或 QMT 实时数据动态获取持仓标的作为预测目标。"""
    import db as db_mod

    positions = []
    # 优先从 portfolio_snapshot 读取
    portfolio = db_mod.get_latest_portfolio_snapshot(account_scope="combined")
    if portfolio:
        portfolio_data = portfolio.get("data", {}) or {}
        positions = portfolio_data.get("qmt_positions", portfolio_data.get("positions", [])) or []

    # 回退：从 prediction_context packet 读取
    if not positions:
        ctx_packet = db_mod.get_latest_research_packet("prediction_context")
        if ctx_packet:
            ctx_data = ctx_packet.get("data", {}) or {}
            positions = ctx_data.get("qmt_positions", []) or []

    # 回退：从 daily_close snapshot 读取
    if not positions:
        snapshot = db_mod.get_latest_snapshot("daily_close")
        if snapshot:
            snapshot_data = snapshot.get("data", {}) or {}
            positions = snapshot_data.get("qmt_positions", []) or []

    targets = []
    seen = set()
    for pos in positions:
        code = str(pos.get("stock_code", pos.get("code", "")) or "").strip()
        name = str(pos.get("stock_name", pos.get("name", "")) or "").strip()
        if not code:
            continue
        # 过滤掉指数代码
        if code.startswith(("sh000", "sh399", "sz399", "sh688")):
            continue
        if code in seen:
            continue
        seen.add(code)
        targets.append({"code": code, "name": name or code, "prediction_type": "position"})
        if len(targets) >= MAX_POSITION_PREDICTION_TARGETS:
            break

    return targets


def get_all_prediction_targets(include_positions: bool = True) -> list:
    """合并指数目标与持仓目标（去重）。"""
    targets = list(PREDICTION_TARGETS)
    if include_positions:
        index_codes = {str(t["code"]) for t in targets}
        for pos_target in get_position_prediction_targets():
            if pos_target["code"] not in index_codes:
                targets.append(pos_target)
    return targets


def _canonical_prediction_code(value: str) -> str:
    code = str(value or "").strip()
    if code.lower().startswith(("sh", "sz")):
        return code.lower()
    if "." in code:
        stem, suffix = code.split(".", 1)
        return f"{stem}.{suffix.upper()}"
    return code


def get_trusted_prediction_prices(snapshot_data: Dict) -> Dict[str, float]:
    """Extract source prices used to reject copied examples and hallucinated anchors."""
    prices: Dict[str, float] = {}
    for quote in snapshot_data.get("quotes", []) or []:
        if not isinstance(quote, dict) or quote.get("error"):
            continue
        code = _canonical_prediction_code(quote.get("code", ""))
        try:
            price = float(quote.get("price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if code and price > 0:
            prices[code] = price
    for position in snapshot_data.get("qmt_positions", snapshot_data.get("positions", [])) or []:
        if not isinstance(position, dict):
            continue
        code = _canonical_prediction_code(position.get("stock_code", position.get("code", "")))
        try:
            price = float(position.get("current_price", position.get("last_price", 0)) or 0)
        except (TypeError, ValueError):
            continue
        if code and price > 0:
            prices[code] = price
    return prices


def sanitize_strategy_predictions(
    predictions: Sequence[Dict],
    targets: Sequence[Dict],
    snapshot_data: Dict,
    strategy_name: str,
    evidence_profile: str,
    model_used: str,
    prediction_run_id: str,
) -> List[Dict]:
    """Whitelist targets and bind every sample to one explicit evidence chain."""
    allowed = {
        _canonical_prediction_code(item.get("code", "")): item
        for item in targets
        if item.get("code")
    }
    trusted_prices = get_trusted_prediction_prices(snapshot_data)
    sanitized: List[Dict] = []
    seen = set()
    for raw in predictions:
        if not isinstance(raw, dict):
            continue
        code = _canonical_prediction_code(raw.get("code", ""))
        if code not in allowed or code in seen:
            continue
        source_price = trusted_prices.get(code, 0)
        try:
            predicted_price = float(raw.get("current_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if source_price <= 0 or predicted_price <= 0:
            continue
        deviation = abs(predicted_price - source_price) / source_price
        if deviation > PREDICTION_SOURCE_PRICE_TOLERANCE:
            continue
        target = allowed[code]
        item = dict(raw)
        item["code"] = str(target.get("code") or code)
        item["name"] = str(target.get("name") or item["code"])
        item["prediction_type"] = str(target.get("prediction_type") or "index")
        item["current_price"] = source_price
        item["strategy_used"] = strategy_name
        item["evidence_profile"] = evidence_profile
        item["model_used"] = model_used
        item["prediction_run_id"] = prediction_run_id
        sanitized.append(item)
        seen.add(code)
    return sanitized


def _merge_packet_data(payload: Dict, packet: Dict | None) -> Dict:
    if not packet:
        return payload
    data = packet.get("data", {}) or {}
    if not isinstance(data, dict):
        return payload
    merged = dict(payload)
    merged.update(data)
    return merged


def _hydrate_prediction_quotes(payload: Dict) -> Dict:
    """Fill missing/error index quotes before any prediction is allowed to persist."""
    hydrated = dict(payload)
    requested_codes = [str(item["code"]) for item in PREDICTION_TARGETS]
    valid_by_code: Dict[str, Dict] = {}
    for quote in hydrated.get("quotes", []) or []:
        if not isinstance(quote, dict) or quote.get("error"):
            continue
        code = _canonical_prediction_code(quote.get("code", ""))
        try:
            price = float(quote.get("price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if code in requested_codes and price > 0:
            valid_by_code[code] = quote
    missing = [code for code in requested_codes if code not in valid_by_code]
    if missing:
        for quote in fetch_market_quotes(",".join(missing)):
            if not isinstance(quote, dict) or quote.get("error"):
                continue
            code = _canonical_prediction_code(quote.get("code", ""))
            try:
                price = float(quote.get("price", 0) or 0)
            except (TypeError, ValueError):
                continue
            if code in missing and price > 0:
                valid_by_code[code] = quote
    hydrated["quotes"] = [valid_by_code[code] for code in requested_codes if code in valid_by_code]
    hydrated["_quote_sources"] = sorted(
        {str(quote.get("source") or "unknown") for quote in hydrated["quotes"]}
    )
    hydrated["_quote_coverage"] = f"{len(valid_by_code)}/{len(requested_codes)}"
    return hydrated


def load_prediction_snapshot_data() -> Dict:
    """Build prediction context from packet bundle with legacy snapshot fallback."""
    payload: Dict = {}
    repo = get_analysis_context_repository()
    bundle = repo.get_latest_bundle()
    packet_order = ["market", "macro", "sector_rotation", "prediction_context"]
    for packet_type in packet_order:
        packet = (bundle.get("research_packets", {}) or {}).get(packet_type)
        if packet:
            payload = _merge_packet_data(payload, packet)

    portfolio_packet = bundle.get("portfolio_snapshot")
    if portfolio_packet:
        payload = _merge_packet_data(payload, portfolio_packet)
    packet_hits = int(bundle.get("packet_hits", 0) or 0)

    if packet_hits > 0:
        payload["_source"] = "research_packets"
        payload["_packet_hits"] = packet_hits
        return _hydrate_prediction_quotes(payload)

    latest = db.get_latest_snapshot("daily_close")
    if latest:
        snapshot_data = latest.get("data", {}) or {}
        if isinstance(snapshot_data, dict):
            snapshot_data = dict(snapshot_data)
            snapshot_data["_source"] = "market_snapshots"
            snapshot_data["_captured_at"] = latest.get("captured_at", "")
            return _hydrate_prediction_quotes(snapshot_data)
    return {}


def build_prediction_runtime_context(
    rag_query: str = "A股明日走势预测 指数 资金流向",
) -> Dict:
    """Load runtime context needed by prediction generation."""
    snapshot_data = load_prediction_snapshot_data()
    return {
        "snapshot_data": snapshot_data,
        "rag_context": build_rag_context(rag_query),
        "few_shot": build_few_shot_prompt(),
        "system_prompt": generate_system_prompt(),
    }


def build_rule_based_predictions(
    snapshot_data: Dict | None = None,
    targets: Sequence[Dict] | None = None,
    include_positions: bool = True,
) -> List[Dict]:
    """Fallback prediction generator without LLM. Uses 3d-kline format."""
    data = snapshot_data or load_prediction_snapshot_data()
    target_items = list(targets or get_all_prediction_targets(include_positions=include_positions))
    target_codes = {str(t.get("code", "")): t for t in target_items}
    predictions: List[Dict] = []

    strategy_dist = _get_strategy_distribution()

    for quote in data.get("quotes", []):
        if quote.get("error"):
            continue
        code = str(quote.get("code", ""))
        if code not in target_codes:
            continue
        target_info = target_codes[code]
        name = target_info.get("name", quote.get("name", ""))
        pred_type = target_info.get("prediction_type", "index")
        current_price = float(quote.get("price", 0) or 0)
        change = quote.get("change_percent", 0)

        # Simple rule-based: use today's change to project trend
        if change > 1.5:
            trend, ret_3d = "bullish", abs(change) * 0.3
        elif change < -1.5:
            trend, ret_3d = "bearish", -abs(change) * 0.3
        else:
            trend, ret_3d = "ranging", 0.0

        # Build simple kline projections
        step = current_price * ret_3d / 300 if ret_3d != 0 else current_price * 0.001
        day1 = _make_kline(current_price, step, 1)
        day2 = _make_kline(current_price, step, 2)
        day3 = _make_kline(current_price, step, 3)

        buy_point = round(current_price * 0.98, 2)
        sell_point = round(current_price * 1.04, 2)
        stop_loss = round(current_price * 0.95, 2)

        predictions.append({
            "code": code,
            "name": name,
            "current_price": current_price,
            "trend_3d": trend,
            "predicted_return_3d": round(ret_3d, 2),
            "kline_day1": day1,
            "kline_day2": day2,
            "kline_day3": day3,
            "buy_point": buy_point,
            "sell_point": sell_point,
            "stop_loss": stop_loss,
            "confidence": 0.25,
            "strategy_used": strategy_dist.get(code, "technical"),
            "prediction_type": pred_type,
            "reasoning": f"基于均值回归规则，今日涨跌{change}%",
            # Legacy compat
            "direction": trend if trend != "ranging" else "neutral",
            "predicted_change": round(ret_3d, 2),
        })

    # For targets without quote data, generate default ranging prediction
    covered = {str(p["code"]) for p in predictions}
    for code, target_info in target_codes.items():
        if code not in covered:
            current_price = 0.0
            empty_kline = {"open": 0, "high": 0, "low": 0, "close": 0, "pattern": "无数据"}
            predictions.append({
                "code": code,
                "name": target_info.get("name", code),
                "current_price": current_price,
                "trend_3d": "ranging",
                "predicted_return_3d": 0.0,
                "kline_day1": empty_kline,
                "kline_day2": empty_kline,
                "kline_day3": empty_kline,
                "buy_point": 0,
                "sell_point": 0,
                "stop_loss": 0,
                "confidence": 0.15,
                "strategy_used": strategy_dist.get(code, "technical"),
                "prediction_type": target_info.get("prediction_type", "index"),
                "reasoning": "数据不足，默认震荡",
                # Legacy compat
                "direction": "neutral",
                "predicted_change": 0.0,
            })

    return predictions


def _make_kline(current_price: float, step: float, day: int) -> dict:
    """Generate a simple kline projection."""
    base = current_price + step * day
    o = round(base, 2)
    c = round(base + step * 0.6, 2)
    h = round(max(o, c) + abs(step) * 0.5, 2)
    l = round(min(o, c) - abs(step) * 0.4, 2)
    if c > o:
        pattern = "小阳线" if abs(c - o) / o < 0.01 else "阳线"
    elif c < o:
        pattern = "小阴线" if abs(c - o) / o < 0.01 else "阴线"
    else:
        pattern = "十字星"
    return {"open": o, "high": h, "low": l, "close": c, "pattern": pattern}


def _get_strategy_distribution() -> dict:
    """返回 {code: strategy_name} 映射，按当前权重分配策略标签。"""
    import db as db_mod
    from domain.services.evolution_service import load_strategy_config

    strategies = db_mod.get_strategies(enabled_only=True)
    if not strategies:
        return {}
    configured_weights = load_strategy_config().get("weights") or {}
    weights = {s["name"]: float(configured_weights.get(s["name"], s["weight"])) for s in strategies}
    # 按权重排序，交替分配策略标签
    sorted_names = sorted(weights, key=lambda n: -weights[n])
    # 对指数用宏观/情绪，对个股用技术/基本面
    index_strategies = [n for n in sorted_names if n in ("geopolitical", "sentiment")] or sorted_names
    stock_strategies = [n for n in sorted_names if n in ("technical", "fundamental")] or sorted_names

    mapping = {}
    idx_counter = 0
    stock_counter = 0
    for t in PREDICTION_TARGETS:
        mapping[t["code"]] = index_strategies[idx_counter % len(index_strategies)]
        idx_counter += 1
    for pos_target in get_position_prediction_targets():
        mapping[pos_target["code"]] = stock_strategies[stock_counter % len(stock_strategies)]
        stock_counter += 1
    return mapping


def save_predictions(predictions: List[Dict], model: str, run_date: str | None = None) -> List[int]:
    """Persist predictions and return inserted ids."""
    import json as _json

    pred_ids: List[int] = []
    created_date = run_date or datetime.now().strftime("%Y-%m-%d")
    existing_keys = db.get_prediction_keys_for_date(created_date)
    for item in predictions:
        key = (
            str(item.get("code") or ""),
            str(item.get("strategy_used") or "technical"),
            str(item.get("timeframe") or "3d"),
        )
        if key in existing_keys:
            item["_persist_status"] = "duplicate"
            continue
        current_price = float(item.get("current_price", 0) or 0)
        if current_price <= 0:
            quotes = fetch_market_quotes(item["code"])
            if quotes and not quotes[0].get("error"):
                current_price = quotes[0].get("price", 0) or 0

        # Serialize kline days to JSON strings
        kd1 = _json.dumps(item.get("kline_day1", {}), ensure_ascii=False) if item.get("kline_day1") else None
        kd2 = _json.dumps(item.get("kline_day2", {}), ensure_ascii=False) if item.get("kline_day2") else None
        kd3 = _json.dumps(item.get("kline_day3", {}), ensure_ascii=False) if item.get("kline_day3") else None

        pid = db.add_prediction(
            target=item["code"],
            target_name=item.get("name", ""),
            direction=item.get("direction", "neutral"),
            confidence=item["confidence"],
            reasoning=item.get("reasoning", ""),
            strategy_used=item.get("strategy_used", "technical"),
            model_used=item.get("model_used", model),
            predicted_change=item.get("predicted_change"),
            actual_price=current_price,
            trend_3d=item.get("trend_3d"),
            predicted_return_3d=item.get("predicted_return_3d"),
            kline_day1=kd1,
            kline_day2=kd2,
            kline_day3=kd3,
            buy_point=item.get("buy_point"),
            sell_point=item.get("sell_point"),
            stop_loss=item.get("stop_loss"),
            evidence_profile=item.get("evidence_profile", ""),
            prediction_run_id=item.get("prediction_run_id", ""),
        )
        pred_ids.append(pid)
        existing_keys.add(key)
        item["_persist_status"] = "saved"
        item["_prediction_id"] = pid
    return pred_ids

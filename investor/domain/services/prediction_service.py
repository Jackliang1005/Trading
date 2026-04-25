#!/usr/bin/env python3
"""Prediction domain service (phase 1 extraction)."""

from __future__ import annotations

from typing import Dict, List, Sequence

import db
from data_collector import fetch_market_quotes
from domain.repository import get_analysis_context_repository
from evolution import generate_system_prompt
from knowledge_base import build_few_shot_prompt, build_rag_context

PREDICTION_TARGETS = [
    {"code": "sh000001", "name": "上证指数"},
    {"code": "sz399001", "name": "深证成指"},
    {"code": "sz399006", "name": "创业板指"},
]

# 最大持仓预测数（避免 prompt 过长）
MAX_POSITION_PREDICTION_TARGETS = 15


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


def _merge_packet_data(payload: Dict, packet: Dict | None) -> Dict:
    if not packet:
        return payload
    data = packet.get("data", {}) or {}
    if not isinstance(data, dict):
        return payload
    merged = dict(payload)
    merged.update(data)
    return merged


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
        return payload

    latest = db.get_latest_snapshot("daily_close")
    if latest:
        snapshot_data = latest.get("data", {}) or {}
        if isinstance(snapshot_data, dict):
            snapshot_data = dict(snapshot_data)
            snapshot_data["_source"] = "market_snapshots"
            snapshot_data["_captured_at"] = latest.get("captured_at", "")
            return snapshot_data
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
    """Fallback prediction generator without LLM. Uses all available targets including positions."""
    data = snapshot_data or load_prediction_snapshot_data()
    target_items = list(targets or get_all_prediction_targets(include_positions=include_positions))
    target_codes = {str(t.get("code", "")): t for t in target_items}
    predictions: List[Dict] = []

    # 策略分布：按权重概率分配，避免全部标为 technical
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
        change = quote.get("change_percent", 0)
        if change > 1.5:
            direction, pred_change = "down", -change * 0.3
        elif change < -1.5:
            direction, pred_change = "up", abs(change) * 0.3
        else:
            direction, pred_change = "neutral", 0.0
        predictions.append(
            {
                "code": code,
                "name": name,
                "direction": direction,
                "confidence": 0.3,
                "predicted_change": round(pred_change, 2),
                "strategy_used": strategy_dist.get(code, "technical"),
                "prediction_type": pred_type,
                "reasoning": f"基于均值回归，今日涨跌{change}%",
            }
        )

    # 对于无行情数据的标的，生成默认中性预测
    covered = {str(p["code"]) for p in predictions}
    for code, target_info in target_codes.items():
        if code not in covered:
            predictions.append(
                {
                    "code": code,
                    "name": target_info.get("name", code),
                    "direction": "neutral",
                    "confidence": 0.2,
                    "predicted_change": 0.0,
                    "strategy_used": strategy_dist.get(code, "technical"),
                    "prediction_type": target_info.get("prediction_type", "index"),
                    "reasoning": "数据不足，默认中性",
                }
            )

    return predictions


def _get_strategy_distribution() -> dict:
    """返回 {code: strategy_name} 映射，按当前权重分配策略标签。"""
    import db as db_mod

    strategies = db_mod.get_strategies(enabled_only=True)
    if not strategies:
        return {}
    weights = {s["name"]: s["weight"] for s in strategies}
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


def save_predictions(predictions: List[Dict], model: str) -> List[int]:
    """Persist predictions and return inserted ids."""
    pred_ids: List[int] = []
    for item in predictions:
        current_price = None
        quotes = fetch_market_quotes(item["code"])
        if quotes and not quotes[0].get("error"):
            current_price = quotes[0].get("price")

        pid = db.add_prediction(
            target=item["code"],
            target_name=item.get("name", ""),
            direction=item["direction"],
            confidence=item["confidence"],
            reasoning=item.get("reasoning", ""),
            strategy_used=item.get("strategy_used", "technical"),
            model_used=model,
            predicted_change=item.get("predicted_change"),
            actual_price=current_price,
        )
        pred_ids.append(pid)
    return pred_ids

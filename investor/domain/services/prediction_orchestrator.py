#!/usr/bin/env python3
"""Prediction orchestration service."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List

from domain.services.prediction_prompt_service import build_prediction_prompt
from domain.services.prediction_service import (
    build_prediction_runtime_context,
    build_rule_based_predictions,
    get_all_prediction_targets,
    get_position_prediction_targets,
    save_predictions,
)
from infrastructure.llm.client import call_prediction_llm, resolve_available_provider
from infrastructure.llm.parser import parse_prediction_output


def render_rule_based_prediction_json() -> str:
    return json.dumps(
        build_rule_based_predictions(include_positions=True),
        ensure_ascii=False,
    )


def call_llm_for_prediction(prompt: str, model: str = "deepseek/deepseek-chat") -> str:
    provider, _ = resolve_available_provider()
    if not provider:
        print("  ⚠️ 无可用 LLM API，使用规则预测")
        return render_rule_based_prediction_json()
    try:
        return call_prediction_llm(prompt, model=model)
    except Exception as exc:
        print(f"  ⚠️ LLM provider 调用失败: {exc}")
        print("  ⚠️ 回退到规则预测")
        return render_rule_based_prediction_json()


def parse_predictions(llm_output: str) -> List[Dict]:
    return parse_prediction_output(llm_output)


def generate_predictions(
    model: str = "deepseek/deepseek-chat",
    include_positions: bool = True,
) -> List[int]:
    print(f"🔮 开始生成市场预测 [{datetime.now().strftime('%Y-%m-%d %H:%M')}]")

    runtime_context = build_prediction_runtime_context()
    snapshot_data = runtime_context.get("snapshot_data", {}) or {}
    if not snapshot_data:
        print("  ❌ 无市场数据快照，请先运行 collect")
        return []
    source = snapshot_data.get("_source", "unknown")
    if source == "research_packets":
        print(f"  📦 使用 packet 上下文: hits={snapshot_data.get('_packet_hits', 0)}")
    else:
        print(f"  📊 使用旧数据快照: {snapshot_data.get('_captured_at', '?')}")

    # 获取扩展预测目标（含持仓）
    position_targets = get_position_prediction_targets() if include_positions else []
    all_targets = get_all_prediction_targets(include_positions=include_positions)
    if position_targets:
        print(f"  📊 持仓预测目标: {len(position_targets)} 只 ({', '.join(t['code'] for t in position_targets[:5])}{'...' if len(position_targets) > 5 else ''})")
    print(f"  🎯 总预测目标: {len(all_targets)} 个")

    rag_context = runtime_context.get("rag_context", "")
    few_shot = runtime_context.get("few_shot", "")
    system_prompt = runtime_context.get("system_prompt", "")

    prompt = build_prediction_prompt(snapshot_data, rag_context, few_shot, system_prompt)
    print("  🤖 调用 LLM 生成预测...")
    llm_output = call_llm_for_prediction(prompt, model)

    try:
        predictions = parse_predictions(llm_output)
    except Exception as exc:
        print(f"  ❌ 解析预测失败: {exc}")
        print(f"  LLM 原始输出: {llm_output[:500]}")
        predictions = build_rule_based_predictions(
            snapshot_data=snapshot_data, targets=all_targets, include_positions=include_positions
        )

    if not predictions:
        print("  ❌ 未生成有效预测")
        return []

    pred_ids = save_predictions(predictions, model=model)
    for item, pred_id in zip(predictions, pred_ids):
        pred_type = item.get("prediction_type", "index")
        print(
            f"  📝 [{pred_type}] {item.get('name', item['code'])} {item['direction']} "
            f"(策略:{item.get('strategy_used', 'technical')}, 置信度:{item['confidence']:.0%}, "
            f"预测涨跌:{item.get('predicted_change', 0):+.2f}%) → ID:{pred_id}"
        )
    index_count = sum(1 for p in predictions if p.get("prediction_type", "index") == "index")
    pos_count = sum(1 for p in predictions if p.get("prediction_type") == "position")
    print(f"✅ 预测生成完成: {len(pred_ids)} 条 (指数:{index_count} 持仓:{pos_count})")
    return pred_ids


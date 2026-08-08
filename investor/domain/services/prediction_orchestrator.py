#!/usr/bin/env python3
"""Prediction orchestration service."""

from __future__ import annotations

import json
import os
import signal
import threading
from datetime import datetime
from typing import Dict, List

from domain.services.prediction_prompt_service import build_prediction_prompt
from domain.services.evolution_service import EVOLUTION_EVIDENCE_PROFILES
from domain.services.prediction_service import (
    build_prediction_runtime_context,
    build_rule_based_predictions,
    get_all_prediction_targets,
    get_position_prediction_targets,
    sanitize_strategy_predictions,
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


STRATEGY_TASKS = {
    "technical": {
        "evidence_profile": EVOLUTION_EVIDENCE_PROFILES["technical"],
        "focus": (
            "只用标的行情、涨跌幅、市场状态、支撑阻力和价格结构形成方向；"
            "新闻、宏观和资金数据不得作为方向依据，只可作为风险备注。"
        ),
        "indices_only": True,
        "allow_rule_fallback": True,
        "system_role": (
            "你是A股技术策略分析器。只允许依据价格、涨跌幅、市场状态、支撑阻力和价格结构判断方向。"
            "不得用新闻、宏观、资金流或情绪信息形成方向。严格输出JSON。"
        ),
    },
    "sentiment": {
        "evidence_profile": EVOLUTION_EVIDENCE_PROFILES["sentiment"],
        "focus": (
            "只用资金流向、板块热度、市场新闻、全球市场与宏观事件形成方向；"
            "标的现价只用于价格锚定，不得使用技术形态或均线推导方向。"
        ),
        "indices_only": True,
        "allow_rule_fallback": False,
        "system_role": (
            "你是A股情绪策略分析器。只允许依据资金流、板块热度、市场新闻、全球市场和宏观事件判断方向。"
            "行情价格只用于数值锚定，不得用技术形态、均线或支撑阻力形成方向。严格输出JSON。"
        ),
    },
}


def _sentiment_evidence_ready(snapshot_data: Dict) -> bool:
    evidence_groups = [
        snapshot_data.get("flow"),
        snapshot_data.get("sectors"),
        snapshot_data.get("news_eastmoney") or snapshot_data.get("news_rss"),
        snapshot_data.get("global_indices"),
        snapshot_data.get("macro_news"),
    ]
    return sum(bool(group) for group in evidence_groups) >= 2


def _call_strategy_prediction(
    prompt: str,
    model: str,
    allow_rule_fallback: bool,
    system_role: str = "",
) -> tuple[str, str]:
    provider, _ = resolve_available_provider()
    if not provider:
        if allow_rule_fallback:
            return render_rule_based_prediction_json(), "rule_based_v1"
        return "", ""
    timeout_seconds = max(5, int(os.environ.get("INVESTOR_PREDICTION_LLM_TIMEOUT", "70") or 70))

    def invoke() -> str:
        return call_prediction_llm(prompt, model=model, system_role=system_role)

    try:
        if hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread():
            previous_handler = signal.getsignal(signal.SIGALRM)

            def handle_timeout(signum, frame):
                raise TimeoutError(f"LLM hard timeout after {timeout_seconds}s")

            signal.signal(signal.SIGALRM, handle_timeout)
            signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
            try:
                return invoke(), model
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
        return invoke(), model
    except Exception as exc:
        print(f"  ⚠️ {model} 策略调用失败: {exc}")
        if allow_rule_fallback:
            return render_rule_based_prediction_json(), "rule_based_v1"
        return "", ""


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

    prediction_run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    predictions: List[Dict] = []
    prepared_tasks = []
    for strategy_name, task in STRATEGY_TASKS.items():
        if strategy_name == "sentiment" and not _sentiment_evidence_ready(snapshot_data):
            print("  ℹ️ 情绪策略证据不足（需至少两类资金/新闻/全球数据），本轮跳过")
            continue
        strategy_targets = [
            target for target in all_targets
            if not task["indices_only"] or target.get("prediction_type", "index") == "index"
        ]
        prompt = build_prediction_prompt(
            snapshot_data,
            "",
            "",
            system_prompt,
            targets=strategy_targets,
            strategy_name=strategy_name,
            strategy_focus=task["focus"],
        )
        print(f"  🤖 调用 {strategy_name} 独立证据链...")
        prepared_tasks.append((strategy_name, task, strategy_targets, prompt))

    def run_task(prepared):
        strategy_name, task, strategy_targets, prompt = prepared
        llm_output, actual_model = _call_strategy_prediction(
            prompt,
            model,
            allow_rule_fallback=bool(task["allow_rule_fallback"]),
            system_role=str(task["system_role"]),
        )
        return strategy_name, task, strategy_targets, llm_output, actual_model

    task_results = [run_task(task) for task in prepared_tasks]

    for strategy_name, task, strategy_targets, llm_output, actual_model in task_results:
        if not llm_output:
            print(f"  ℹ️ {strategy_name} 无可靠输出，本轮不制造替代样本")
            continue
        try:
            parsed = parse_predictions(llm_output)
        except Exception as exc:
            print(f"  ❌ {strategy_name} 解析失败: {exc}")
            print(f"  LLM 原始输出: {llm_output[:500]}")
            if not task["allow_rule_fallback"]:
                continue
            parsed = build_rule_based_predictions(
                snapshot_data=snapshot_data,
                targets=strategy_targets,
                include_positions=include_positions,
            )
            actual_model = "rule_based_v1"
        accepted = sanitize_strategy_predictions(
            parsed,
            strategy_targets,
            snapshot_data,
            strategy_name=strategy_name,
            evidence_profile=str(task["evidence_profile"]),
            model_used=actual_model,
            prediction_run_id=prediction_run_id,
        )
        rejected_count = len(parsed) - len(accepted)
        if rejected_count:
            print(f"  🛡️ {strategy_name} 拒绝 {rejected_count} 条越界/错价/重复输出")
        predictions.extend(accepted)

    if not predictions:
        print("  ❌ 未生成有效预测")
        return []

    pred_ids = save_predictions(predictions, model=model)
    for item in predictions:
        if item.get("_persist_status") != "saved":
            continue
        pred_id = item.get("_prediction_id")
        pred_type = item.get("prediction_type", "index")
        trend = item.get("trend_3d", "?")
        ret_3d = item.get("predicted_return_3d", 0) or 0
        bp = item.get("buy_point", 0) or 0
        sp = item.get("sell_point", 0) or 0
        sl = item.get("stop_loss", 0) or 0
        print(
            f"  📝 [{pred_type}] {item.get('name', item['code'])} 趋势:{trend} "
            f"(3日收益:{ret_3d:+.2f}%, 置信度:{item['confidence']:.0%}) "
            f"买{bp:.2f}/卖{sp:.2f}/止损{sl:.2f} → ID:{pred_id}"
        )
    saved_predictions = [p for p in predictions if p.get("_persist_status") == "saved"]
    index_count = sum(1 for p in saved_predictions if p.get("prediction_type", "index") == "index")
    pos_count = sum(1 for p in saved_predictions if p.get("prediction_type") == "position")
    duplicate_count = sum(1 for p in predictions if p.get("_persist_status") == "duplicate")
    strategy_counts = {
        name: sum(1 for p in saved_predictions if p.get("strategy_used") == name)
        for name in STRATEGY_TASKS
    }
    print(
        f"✅ 预测生成完成: {len(pred_ids)} 条 (指数:{index_count} 持仓:{pos_count}, "
        f"策略:{strategy_counts}, 去重:{duplicate_count})"
    )
    return pred_ids

#!/usr/bin/env python3
"""
OpenClaw Investor - Agent 交互增强脚本
用于在 OpenClaw agent 会话中增强投资分析能力

用法（在 agent 的工具调用中）：
  python3 investor_agent.py context "用户的问题"     → 获取 RAG 上下文
  python3 investor_agent.py predict <target> <direction> <confidence> "reasoning"  → 记录预测
  python3 investor_agent.py feedback <accept|reject> [prediction_id] "reason"  → 记录反馈
  python3 investor_agent.py dashboard                → 状态看板
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import db
from domain.services.evolution_service import generate_system_prompt, load_strategy_config
from domain.services.assistant_service import dashboard, record_feedback, record_prediction
from knowledge_base import auto_memorize_interaction
from app.agent_bridge import build_context_markdown


def cmd_context(query: str) -> str:
    """获取增强上下文（RAG + 规则 + 策略）"""
    db.init_db()

    config = load_strategy_config()

    # 策略权重摘要
    weights = config.get("weights", {})
    weight_str = " | ".join(f"{k}:{v:.0%}" for k, v in weights.items())
    context = build_context_markdown(query=query)
    return f"{context}\n\n### 策略权重: {weight_str}"


def cmd_predict(args: list) -> str:
    """记录预测"""
    if len(args) < 4:
        return "用法: predict <target> <direction> <confidence> [reasoning] [strategy]"

    db.init_db()
    target = args[0]
    direction = args[1]
    confidence = float(args[2])
    reasoning = args[3] if len(args) > 3 else ""
    strategy = args[4] if len(args) > 4 else "technical"

    pid = record_prediction(
        target=target,
        direction=direction,
        confidence=confidence,
        reasoning=reasoning,
        strategy=strategy,
    )
    return f"✅ 预测已记录 [ID:{pid}] {target} {direction} 置信度:{confidence:.0%}"


def cmd_feedback(args: list) -> str:
    """记录反馈"""
    if len(args) < 1:
        return "用法: feedback <accept|reject> [prediction_id] [reason]"

    db.init_db()
    action = args[0]
    pred_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    reason = args[2] if len(args) > 2 else ""

    record_feedback(action, pred_id, reason)
    return f"✅ 反馈已记录: {action}"


def cmd_memorize(args: list) -> str:
    """记录交互"""
    if len(args) < 2:
        return "用法: memorize <query> <response>"

    db.init_db()
    query = args[0]
    response = args[1]
    iid = auto_memorize_interaction(query, response)
    return f"✅ 交互已记录 [ID:{iid}]"


def cmd_dashboard() -> str:
    """状态看板"""
    db.init_db()
    return dashboard()


def cmd_prompt() -> str:
    """获取当前 system prompt"""
    db.init_db()
    return generate_system_prompt()


def main():
    if len(sys.argv) < 2:
        print("🦞 OpenClaw Investor Agent 工具")
        print("\n命令:")
        print("  context <query>     — 获取 RAG 增强上下文")
        print("  predict ...         — 记录预测")
        print("  feedback ...        — 记录反馈")
        print("  memorize ...        — 记录交互")
        print("  dashboard           — 状态看板")
        print("  prompt              — 当前 system prompt")
        return

    cmd = sys.argv[1]

    if cmd == "context":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "市场分析"
        print(cmd_context(query))
    elif cmd == "predict":
        print(cmd_predict(sys.argv[2:]))
    elif cmd == "feedback":
        print(cmd_feedback(sys.argv[2:]))
    elif cmd == "memorize":
        print(cmd_memorize(sys.argv[2:]))
    elif cmd == "dashboard":
        print(cmd_dashboard())
    elif cmd == "prompt":
        print(cmd_prompt())
    else:
        print(f"❌ 未知命令: {cmd}")


if __name__ == "__main__":
    main()

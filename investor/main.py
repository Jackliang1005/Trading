#!/usr/bin/env python3
"""
OpenClaw Investor - 主入口
统一调度四个闭环：感知 → 记忆 → 反思 → 进化
"""

import json
import os
import sys
from typing import Dict

sys.path.insert(0, os.path.dirname(__file__))

from domain.services.assistant_service import (
    analyze as analyze_service,
    record_feedback as record_feedback_service,
    record_prediction as record_prediction_service,
)
from domain.services.evolution_service import generate_system_prompt
from domain.services.legacy_entry_service import (
    build_dashboard,
    cron_daily_collect as cron_daily_collect_service,
    cron_daily_predict as cron_daily_predict_service,
    cron_daily_reflect as cron_daily_reflect_service,
    cron_monthly_audit as cron_monthly_audit_service,
    cron_sector_scan as cron_sector_scan_service,
    cron_weekly_evolve as cron_weekly_evolve_service,
    init_system,
)
from domain.services.reflection_service import backtest_predictions
from legacy.compat.main_compat import handle_new_command, print_new_command_help


def init():
    """兼容入口：初始化编排已迁入 domain.services.legacy_entry_service。"""
    init_system()


# ──────────────── 分析接口 ────────────────

def analyze(query: str, model: str = "", session_id: str = "") -> Dict:
    """兼容入口：分析能力已迁入 domain.services.assistant_service。"""
    return analyze_service(query=query, model=model, session_id=session_id)


def record_prediction(target: str, direction: str, confidence: float,
                      reasoning: str, strategy: str = "technical",
                      model: str = "", predicted_change: float = None,
                      target_name: str = "") -> int:
    """兼容入口：预测记录能力已迁入 domain.services.assistant_service。"""
    return record_prediction_service(
        target=target,
        direction=direction,
        confidence=confidence,
        reasoning=reasoning,
        strategy=strategy,
        model=model,
        predicted_change=predicted_change,
        target_name=target_name,
    )


def record_feedback(action: str, prediction_id: int = None,
                    reason: str = "", comment: str = ""):
    """兼容入口：反馈记录能力已迁入 domain.services.assistant_service。"""
    record_feedback_service(action=action, prediction_id=prediction_id, reason=reason, comment=comment)


# ──────────────── 定时任务入口 ────────────────

def cron_daily_collect():
    """兼容入口：日采集编排已迁入 domain.services.legacy_entry_service。"""
    return cron_daily_collect_service()


def cron_daily_predict():
    """兼容入口：日预测编排已迁入 domain.services.legacy_entry_service。"""
    return cron_daily_predict_service()


def cron_daily_reflect():
    """兼容入口：日反思编排已迁入 domain.services.legacy_entry_service。"""
    return cron_daily_reflect_service()


def cron_weekly_evolve():
    """兼容入口：周进化编排已迁入 domain.services.legacy_entry_service。"""
    return cron_weekly_evolve_service()


def cron_sector_scan():
    """兼容入口：板块扫描编排已迁入 domain.services.legacy_entry_service。"""
    return cron_sector_scan_service()


def cron_monthly_audit():
    """兼容入口：月审计编排已迁入 domain.services.legacy_entry_service。"""
    return cron_monthly_audit_service()


# ──────────────── 状态看板 ────────────────

def dashboard() -> str:
    """兼容入口：状态看板能力已迁入 domain.services.assistant_service。"""
    return build_dashboard()


# ──────────────── CLI ────────────────


COMMANDS = {
    "init": ("初始化系统", init),
    "collect": ("采集每日数据", cron_daily_collect),
    "predict": ("生成每日预测", cron_daily_predict),
    "reflect": ("每日反思", cron_daily_reflect),
    "evolve": ("进化（调整策略/规则/案例）", cron_weekly_evolve),
    "audit": ("月度审计", cron_monthly_audit),
    "dashboard": ("状态看板", lambda: print(dashboard())),
    "prompt": ("查看当前 system prompt", lambda: print(generate_system_prompt())),
    "backtest": ("回测预测", lambda: backtest_predictions()),
    "sector-scan": ("板块扫描+持仓诊断", cron_sector_scan),
}


def main():
    if len(sys.argv) < 2:
        print("🦞 OpenClaw Investor — 自动学习投资助手")
        print(f"\n用法: python3 {sys.argv[0]} <command>")
        print("\n可用命令（legacy）:")
        for cmd, (desc, _) in COMMANDS.items():
            print(f"  {cmd:12s} — {desc}")
        print_new_command_help()
        return

    cmd = sys.argv[1]

    if handle_new_command(cmd):
        return

    if cmd in COMMANDS:
        _, func = COMMANDS[cmd]
        result = func()
        if isinstance(result, str):
            print(result)
        elif result and isinstance(result, dict):
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str)[:5000])
    else:
        print(f"❌ 未知命令: {cmd}")
        print(f"可用: {', '.join(COMMANDS.keys())}")
        print_new_command_help()


if __name__ == "__main__":
    main()

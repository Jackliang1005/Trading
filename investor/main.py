#!/usr/bin/env python3
"""
OpenClaw Investor — 主入口
统一调度四个闭环：感知 → 记忆 → 反思 → 进化
小龙虾 (XiaoLongXia) — A股投资助理
"""

import json
import contextlib
import io
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

# ── New-style CLI: import dispatch directly from app/cli.py ──
from app.cli import (
    NEW_COMMAND_SPECS,
    is_new_command,
    print_result,
    run_command as run_new_command,
)


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


def _run_sync_logs():
    """同步国金/东莞生产日志到本地。"""
    from workflows.sync_production_logs import sync_date
    from datetime import datetime
    return sync_date(datetime.now().strftime("%Y-%m-%d"))


# ──────────────── 状态看板 ────────────────

def dashboard() -> str:
    """兼容入口：状态看板能力已迁入 domain.services.assistant_service。"""
    return build_dashboard()


# ──────────────── 统合命令注册 ────────────────

LEGACY_COMMANDS = {
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
    "sync-logs": ("同步生产机器日志到本地", lambda: _run_sync_logs()),
}

# Commands handled by app/cli.py (new-style)
NEW_COMMANDS = set(NEW_COMMAND_SPECS.keys())


def _run_legacy_command(cmd: str):
    """Execute a legacy command and return (has_output, result)."""
    _, func = LEGACY_COMMANDS[cmd]
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            result = func()
    except Exception:
        details = captured.getvalue().strip()
        if details:
            print(details, file=sys.stderr)
        raise
    if isinstance(result, str):
        print(result)
    elif result and isinstance(result, dict):
        if isinstance(result.get("text"), str) and result.get("text").strip():
            print(result["text"])
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str)[:5000])


def _print_unified_help():
    """Print unified help showing all commands (legacy + new)."""
    print("🦞 小龙虾 (XiaoLongXia) — OpenClaw Investor 投资助理")
    print(f"\n用法: python3 {sys.argv[0]} <command> [args...]")
    print("\n── 闭环命令 ──")
    for cmd, (desc, _) in LEGACY_COMMANDS.items():
        print(f"  {cmd:18s} {desc}")
    print("\n── 监控与交易视图 ──")
    for cmd, spec in NEW_COMMAND_SPECS.items():
        print(f"  {cmd:18s} {spec['description']}")
    print("\n提示: python3 main.py help <command> 查看命令详情")


def main():
    if len(sys.argv) < 2:
        _print_unified_help()
        return

    cmd = sys.argv[1]

    # "help" uses unified display
    if cmd == "help":
        target = sys.argv[2] if len(sys.argv) > 2 else ""
        if target and is_new_command(target):
            # Show detailed help for a specific new-style command
            from app.cli import get_new_command_help
            print(get_new_command_help(target))
        else:
            _print_unified_help()
        return

    # Unified dispatch: try legacy first, then new-style
    if cmd in LEGACY_COMMANDS:
        _run_legacy_command(cmd)
        return

    if is_new_command(cmd):
        try:
            result = run_new_command(cmd)
            print_result(result)
        except ValueError as exc:
            usage = NEW_COMMAND_SPECS.get(cmd, {}).get("usage", "")
            message = str(exc).strip()
            if message and message != usage:
                print(f"❌ {message}")
            if usage:
                print(f"usage: {usage}")
        return

    print(f"❌ 未知命令: {cmd}")
    _print_unified_help()


if __name__ == "__main__":
    main()

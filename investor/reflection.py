#!/usr/bin/env python3
"""
Loop 3: 反思 — 自动评估与归因
每日回测预测 → 评分 → 归因分析 → 生成反思报告
"""

import sys
import os
from typing import Dict, List

sys.path.insert(0, os.path.dirname(__file__))
import db
from domain.policies.scoring_policy import calculate_prediction_score as score_prediction_by_policy
from domain.services.reflection_analysis_service import (
    analyze_failure_patterns as analyze_failure_patterns_by_service,
    format_weekly_report as format_weekly_report_by_service,
)
from domain.services.reflection_runtime_service import (
    build_trading_summary_report as build_trading_summary_report_runtime,
    daily_reflection as daily_reflection_runtime,
    get_reflection_context_summary as get_reflection_context_summary_runtime,
    load_reflection_context as load_reflection_context_runtime,
)
def load_reflection_context() -> Dict:
    """兼容入口：上下文加载已迁入 domain.services.reflection_runtime_service。"""
    return load_reflection_context_runtime()


def get_reflection_context_summary() -> Dict:
    """兼容入口：上下文摘要已迁入 domain.services.reflection_runtime_service。"""
    return get_reflection_context_summary_runtime()

# ──────────────── 每日自动回测 ────────────────

def backtest_predictions(target_date: str = None) -> Dict:
    """兼容入口：回测编排已迁入 domain.services.reflection_runtime_service。"""
    from domain.services.reflection_runtime_service import backtest_predictions as backtest_predictions_service
    return backtest_predictions_service(target_date=target_date)


def calculate_prediction_score(pred: Dict, actual_change: float, is_correct: bool,
                               is_near_miss: bool = False) -> float:
    """兼容入口：评分策略已下沉到 domain.policies.scoring_policy。"""
    return score_prediction_by_policy(pred, actual_change, is_correct, is_near_miss)


# ──────────────── 周度归因分析 ────────────────

def weekly_attribution(end_date: str = None) -> Dict:
    """兼容入口：周度归因编排已迁入 domain.services.reflection_service。"""
    from domain.services.reflection_service import weekly_attribution as weekly_attribution_service
    return weekly_attribution_service(date=end_date)


def analyze_failure_patterns(failures: List[Dict]) -> List[Dict]:
    """兼容入口：失败模式分析已下沉到 domain.services.reflection_analysis_service。"""
    return analyze_failure_patterns_by_service(failures)


def format_weekly_report(report: Dict) -> str:
    """兼容入口：周报格式化已下沉到 domain.services.reflection_analysis_service。"""
    return format_weekly_report_by_service(report)


# ──────────────── 月度策略审计 ────────────────

def monthly_audit(end_date: str = None) -> Dict:
    """兼容入口：月度审计编排已迁入 domain.services.reflection_service。"""
    from domain.services.reflection_service import monthly_audit as monthly_audit_service
    return monthly_audit_service(date=end_date)


# ──────────────── 每日反思主函数 ────────────────

def daily_reflection():
    """兼容入口：每日反思编排已迁入 domain.services.reflection_runtime_service。"""
    return daily_reflection_runtime()


def build_trading_summary_report(ts: Dict) -> str:
    """兼容入口：交易摘要格式化已迁入 domain.services.reflection_runtime_service。"""
    return build_trading_summary_report_runtime(ts)


if __name__ == "__main__":
    db.init_db()
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "weekly":
            from domain.services.reflection_service import weekly_attribution as weekly_attribution_service
            weekly_attribution_service()
        elif cmd == "monthly":
            from domain.services.reflection_service import monthly_audit as monthly_audit_service
            monthly_audit_service()
        elif cmd == "backtest":
            date = sys.argv[2] if len(sys.argv) > 2 else None
            backtest_predictions(date)
        else:
            print(f"未知命令: {cmd}")
    else:
        daily_reflection()

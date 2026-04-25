#!/usr/bin/env python3
"""Reflection services."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Optional

import db
from domain.repository import get_prediction_evaluation_repository
from domain.services.reflection_analysis_service import (
    analyze_failure_patterns,
    format_weekly_report,
)

EVALUATION_DATA_SOURCE = "prediction_evaluations_preferred_with_prediction_log_fallback"


def _resolve_end_date(date: Optional[str] = None) -> str:
    return date or datetime.now().strftime("%Y-%m-%d")


def daily_reflection() -> Dict:
    from domain.services.reflection_runtime_service import daily_reflection as daily_reflection_runtime
    return daily_reflection_runtime()


def weekly_attribution(date: Optional[str] = None) -> Dict:
    end_date = _resolve_end_date(date)
    start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")

    print(f"📊 周度归因分析 [{start_date} ~ {end_date}]")
    repo = get_prediction_evaluation_repository()

    strategy_perf = repo.get_strategy_performance(start_date, end_date)
    print("\n📈 策略表现:")
    for sp in strategy_perf:
        print(
            f"  - {sp['strategy_used']}: 胜率 {sp['win_rate']}%, "
            f"共 {sp['total']} 次预测, 正确 {sp['correct']} 次"
        )

    predictions = repo.get_checked_predictions_in_range(start_date, end_date)
    failures = [p for p in predictions if not p.get("is_correct")]
    failure_patterns = analyze_failure_patterns(failures)
    overall = repo.get_overall_stats(start_date, end_date)

    report = {
        "period": f"{start_date} ~ {end_date}",
        "total": overall.get("total", 0),
        "correct": overall.get("correct", 0),
        "win_rate": overall.get("win_rate", 0),
        "findings": [],
        "failures": failure_patterns,
        "suggestions": [],
        "strategy_performance": [dict(s) for s in strategy_perf] if strategy_perf else [],
        "evaluation_data_source": EVALUATION_DATA_SOURCE,
    }

    if strategy_perf:
        best = max(strategy_perf, key=lambda x: x.get("win_rate", 0) or 0)
        worst = min(strategy_perf, key=lambda x: x.get("win_rate", 100) or 100)
        report["findings"].append(f"最佳策略: {best['strategy_used']} (胜率 {best['win_rate']}%)")
        report["findings"].append(f"最差策略: {worst['strategy_used']} (胜率 {worst['win_rate']}%)")
    if failure_patterns:
        report["findings"].append(f"识别到 {len(failure_patterns)} 种失败模式")

    if overall.get("win_rate") and overall["win_rate"] < 50:
        report["suggestions"].append("整体胜率低于50%，建议增加分析深度或降低预测频率")
    for fp in failure_patterns:
        report["suggestions"].append(
            f"针对失败模式'{fp['pattern']}'，建议: {fp.get('suggestion', '加强此类场景分析')}"
        )

    report_text = format_weekly_report(report)
    db.add_reflection_report(
        report_type="weekly",
        period_start=start_date,
        period_end=end_date,
        stats=report,
        full_report=report_text,
    )
    print("\n📋 周度报告已生成并存入数据库")
    return report


def monthly_audit(date: Optional[str] = None) -> Dict:
    end_date = _resolve_end_date(date)
    start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")

    print(f"📋 月度策略审计 [{start_date} ~ {end_date}]")
    repo = get_prediction_evaluation_repository()
    strategy_perf = repo.get_strategy_performance(start_date, end_date)

    for sp in strategy_perf:
        name = sp.get("strategy_used", "")
        if name:
            db.update_strategy_stats(
                name=name,
                total=sp.get("total", 0),
                correct=sp.get("correct", 0),
                win_rate=sp.get("win_rate", 0) or 0,
                avg_score=sp.get("avg_score", 0) or 0,
            )

    overall = repo.get_overall_stats(start_date, end_date)
    report = {
        "period": f"{start_date} ~ {end_date}",
        "total": overall.get("total", 0),
        "correct": overall.get("correct", 0),
        "win_rate": overall.get("win_rate", 0),
        "findings": [],
        "failures": [],
        "suggestions": [],
        "actions": [],
        "strategy_adjustments": [],
        "evaluation_data_source": EVALUATION_DATA_SOURCE,
    }

    if strategy_perf and len(strategy_perf) >= 2:
        sorted_strats = sorted(strategy_perf, key=lambda x: x.get("win_rate", 0) or 0, reverse=True)
        report["findings"].append(f"月度最佳: {sorted_strats[0]['strategy_used']}")
        report["findings"].append(f"月度最差: {sorted_strats[-1]['strategy_used']}")

    report_text = f"# 📋 月度策略审计报告\n**时间：** {report['period']}\n\n"
    report_text += f"总预测 {report['total']}，正确 {report['correct']}，胜率 {report['win_rate']}%\n"
    report_text += f"评估数据源: {report.get('evaluation_data_source', 'unknown')}\n"

    db.add_reflection_report(
        report_type="monthly",
        period_start=start_date,
        period_end=end_date,
        stats=report,
        full_report=report_text,
    )
    print("📋 月度审计完成")
    return report


def backtest_predictions() -> Dict:
    from domain.services.reflection_runtime_service import backtest_predictions as backtest_predictions_runtime
    return backtest_predictions_runtime()

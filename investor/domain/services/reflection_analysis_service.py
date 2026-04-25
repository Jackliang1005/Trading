#!/usr/bin/env python3
"""Reflection analysis helpers."""

from __future__ import annotations

from typing import Dict, List


def analyze_failure_patterns(failures: List[Dict]) -> List[Dict]:
    """分析失败预测的模式。"""
    patterns = []

    if not failures:
        return patterns

    chase_count = 0
    for item in failures:
        reasoning = (item.get("reasoning") or "").lower()
        if "上涨" in reasoning and item.get("direction") == "up":
            chase_count += 1
        elif "下跌" in reasoning and item.get("direction") == "down":
            chase_count += 1
    if chase_count > 1:
        patterns.append(
            {
                "pattern": "追涨杀跌",
                "count": chase_count,
                "description": "跟随当前趋势预测延续，但实际发生反转",
                "suggestion": "增加反转信号检测，不盲目跟随趋势",
            }
        )

    overconfident = [item for item in failures if (item.get("confidence") or 0) > 0.7]
    if len(overconfident) > 1:
        patterns.append(
            {
                "pattern": "过度自信",
                "count": len(overconfident),
                "description": "高置信度预测频繁失败",
                "suggestion": "降低整体置信度，对高置信度预测增加额外验证",
            }
        )

    target_stats = {}
    for item in failures:
        target = item.get("target", "unknown")
        target_stats[target] = target_stats.get(target, 0) + 1
    for target, count in target_stats.items():
        if count >= 2:
            patterns.append(
                {
                    "pattern": f"标的{target}预测困难",
                    "count": count,
                    "description": f"对{target}的预测连续失败{count}次",
                    "suggestion": f"暂停对{target}的预测或增加特定分析维度",
                }
            )

    return patterns


def format_weekly_report(report: Dict) -> str:
    """格式化周度报告。"""
    lines = [
        "# 📊 周度反思报告",
        f"**时间范围：** {report['period']}",
        "",
        "## 整体表现",
        f"- 总预测: {report['total']}",
        f"- 正确: {report['correct']}",
        f"- 胜率: {report['win_rate']}%",
        "",
        f"- 评估数据源: {report.get('evaluation_data_source', 'unknown')}",
        "",
    ]

    if report.get("strategy_performance"):
        lines.append("## 策略表现")
        lines.append("| 策略 | 总数 | 正确 | 胜率 | 平均分 |")
        lines.append("|------|------|------|------|--------|")
        for sp in report["strategy_performance"]:
            lines.append(
                f"| {sp.get('strategy_used', '-')} | {sp.get('total', 0)} | "
                f"{sp.get('correct', 0)} | {sp.get('win_rate', 0)}% | {sp.get('avg_score', 0)} |"
            )
        lines.append("")

    if report.get("findings"):
        lines.append("## 关键发现")
        for finding in report["findings"]:
            lines.append(f"- {finding}")
        lines.append("")

    if report.get("failures"):
        lines.append("## 失败模式")
        for pattern in report["failures"]:
            lines.append(f"- **{pattern['pattern']}** ({pattern['count']}次): {pattern['description']}")
        lines.append("")

    if report.get("suggestions"):
        lines.append("## 改进建议")
        for suggestion in report["suggestions"]:
            lines.append(f"- {suggestion}")

    return "\n".join(lines)

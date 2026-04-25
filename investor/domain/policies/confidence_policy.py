#!/usr/bin/env python3
"""Confidence policy for rule maintenance."""

from __future__ import annotations


def calculate_rule_confidence(
    times_applied: int,
    times_helpful: int,
    default_confidence: float = 0.5,
) -> float:
    """
    依据规则使用效果计算置信度。

    当没有有效样本时回退到默认值；结果限制在 [0, 1]。
    """
    applied = max(0, int(times_applied or 0))
    helpful = max(0, int(times_helpful or 0))
    if applied <= 0:
        return max(0.0, min(1.0, float(default_confidence)))
    return max(0.0, min(1.0, helpful / applied))


def should_disable_rule(
    confidence: float,
    times_applied: int,
    min_confidence: float = 0.2,
    min_applied: int = 10,
) -> bool:
    """判断规则是否应被自动禁用。"""
    return float(confidence or 0) < float(min_confidence) and int(times_applied or 0) > int(min_applied)

#!/usr/bin/env python3
"""Prediction scoring policy."""

from __future__ import annotations

from typing import Dict


def calculate_prediction_score(
    prediction: Dict,
    actual_change: float,
    is_correct: bool,
    is_near_miss: bool = False,
) -> float:
    """
    综合评分 (0-100):
    - 方向正确 +50 / near-miss +30 / 错误 +0
    - 置信度校准 +20
    - 幅度预测 +30
    """
    score = 0.0

    if is_correct:
        score += 50
    elif is_near_miss:
        score += 30

    confidence = float(prediction.get("confidence", 0.5) or 0.5)
    if is_correct:
        score += confidence * 20
    elif is_near_miss:
        score += (1 - confidence) * 15
    else:
        score += (1 - confidence) * 20

    predicted_change = prediction.get("predicted_change")
    if predicted_change is not None and actual_change is not None:
        diff = abs(float(predicted_change) - float(actual_change))
        if diff < 0.3:
            score += 30
        elif diff < 0.5:
            score += 25
        elif diff < 1.0:
            score += 20
        elif diff < 2.0:
            score += 10
        elif diff < 5.0:
            score += 5

    return min(100, max(0, score))


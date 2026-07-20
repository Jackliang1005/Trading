#!/usr/bin/env python3
"""Prediction scoring policy — 3-day K-line + buy/sell points scoring."""

from __future__ import annotations

import json
from typing import Dict, List

# Thresholds
TREND_ACCURACY_MAX = 35.0
KLINE_CLOSE_MAX = 25.0
KLINE_RANGE_MAX = 15.0
BUY_SELL_MAX = 15.0
STOP_LOSS_MAX = 10.0

RANGING_THRESHOLD = 0.015  # 1.5% for ranging validation
BUY_SELL_TOUCH_THRESHOLD = 0.01  # ±1% from predicted point


def calculate_prediction_score(
    prediction: Dict,
    actual_change: float = None,
    is_correct: bool = None,
    is_near_miss: bool = False,
    # New params for 3d-kline scoring
    actual_3d_kline: List[Dict] = None,
    actual_3d_return: float = None,
    actual_3d_high: float = None,
    actual_3d_low: float = None,
    actual_3d_close: float = None,
) -> float:
    """
    Score a prediction (0-100).

    If new-style fields are present (trend_3d + kline_day*):
      - 3日趋势正确: 35
      - K线精度(收盘价): 25
      - K线精度(区间): 15
      - 买卖点有效性: 15
      - 止损合理性: 10

    Otherwise falls back to legacy scoring for old prediction rows.
    """
    trend_3d = (prediction.get("trend_3d") or "").strip()
    kline_day1 = prediction.get("kline_day1")

    if trend_3d and kline_day1:
        return _new_score(
            prediction,
            actual_3d_kline=actual_3d_kline,
            actual_3d_return=actual_3d_return,
            actual_3d_high=actual_3d_high,
            actual_3d_low=actual_3d_low,
            actual_3d_close=actual_3d_close,
        )

    # Legacy fallback
    return _legacy_score(prediction, actual_change, is_correct, is_near_miss)


def _new_score(
    prediction: Dict,
    actual_3d_kline: List[Dict] = None,
    actual_3d_return: float = None,
    actual_3d_high: float = None,
    actual_3d_low: float = None,
    actual_3d_close: float = None,
) -> float:
    score = 0.0

    trend = (prediction.get("trend_3d") or "").strip()
    predicted_return = float(prediction.get("predicted_return_3d", 0) or 0)

    # ── 1. 3日趋势正确 (0-35) ──
    if actual_3d_return is not None:
        if trend == "bullish" and actual_3d_return > 0.005:  # +0.5% or more
            score += TREND_ACCURACY_MAX
        elif trend == "bearish" and actual_3d_return < -0.005:
            score += TREND_ACCURACY_MAX
        elif trend == "ranging" and abs(actual_3d_return) < RANGING_THRESHOLD * 100:
            score += TREND_ACCURACY_MAX
        elif trend == "bullish" and actual_3d_return > -0.003:
            score += TREND_ACCURACY_MAX * 0.5  # mostly flat but not wrong direction
        elif trend == "bearish" and actual_3d_return < 0.003:
            score += TREND_ACCURACY_MAX * 0.5

    # ── 2-3. K线精度 ──
    if actual_3d_kline and len(actual_3d_kline) >= 1:
        score += _kline_precision_score(prediction, actual_3d_kline)

    # ── 4. 买卖点有效性 (0-15) ──
    score += _buysell_score(prediction, actual_3d_high, actual_3d_low)

    # ── 5. 止损合理性 (0-10) ──
    score += _stoploss_score(prediction, actual_3d_low)

    return min(100, max(0, score))


def _kline_precision_score(prediction: Dict, actual_3d_kline: List[Dict]) -> float:
    """Score kline precision: close accuracy (25) + range coverage (15)."""
    score = 0.0
    current_price = float(prediction.get("actual_price_at_predict", 0) or 0)

    close_errors = []
    range_scores = []

    for i in range(min(3, len(actual_3d_kline))):
        day_key = f"kline_day{i + 1}"
        day_pred = prediction.get(day_key)
        if not day_pred:
            continue
        if isinstance(day_pred, str):
            try:
                day_pred = json.loads(day_pred)
            except (json.JSONDecodeError, TypeError):
                continue
        if not isinstance(day_pred, dict):
            continue

        actual_day = actual_3d_kline[i]
        actual_close = float(actual_day.get("close", 0) or 0)
        actual_high = float(actual_day.get("high", 0) or 0)
        actual_low = float(actual_day.get("low", 0) or 0)

        pred_close = float(day_pred.get("close", 0) or 0)
        pred_high = float(day_pred.get("high", 0) or 0)
        pred_low = float(day_pred.get("low", 0) or 0)

        if pred_close > 0 and actual_close > 0:
            # Close error as percentage of actual price
            if current_price > 0:
                close_pct_err = abs(pred_close - actual_close) / current_price
                if close_pct_err < 0.005:    # <0.5%
                    close_errors.append(1.0)
                elif close_pct_err < 0.01:   # <1%
                    close_errors.append(0.8)
                elif close_pct_err < 0.02:   # <2%
                    close_errors.append(0.5)
                elif close_pct_err < 0.03:   # <3%
                    close_errors.append(0.3)
                else:
                    close_errors.append(0.1)

        # Range coverage: how well predicted [low, high] covers actual [low, high]
        if pred_high > 0 and pred_low > 0 and actual_high > 0 and actual_low > 0:
            pred_range = pred_high - pred_low
            actual_range = actual_high - actual_low
            if pred_range > 0 and actual_range > 0:
                overlap_low = max(pred_low, actual_low)
                overlap_high = min(pred_high, actual_high)
                overlap = max(0, overlap_high - overlap_low)
                coverage = overlap / actual_range
                range_scores.append(min(1.0, coverage))

    # Close score: avg of day scores * 25
    if close_errors:
        score += (sum(close_errors) / len(close_errors)) * KLINE_CLOSE_MAX

    # Range score: avg of day scores * 15
    if range_scores:
        score += (sum(range_scores) / len(range_scores)) * KLINE_RANGE_MAX

    return score


def _buysell_score(prediction: Dict, actual_high: float = None, actual_low: float = None) -> float:
    """Score buy/sell point effectiveness: did actual price touch these levels (±1%)?"""
    buy_point = float(prediction.get("buy_point", 0) or 0)
    sell_point = float(prediction.get("sell_point", 0) or 0)

    if buy_point <= 0 or sell_point <= 0:
        return 0

    score = 0.0

    if actual_low is not None and actual_low > 0 and buy_point > 0:
        low_dev = abs(actual_low - buy_point) / buy_point
        if low_dev < BUY_SELL_TOUCH_THRESHOLD:
            score += BUY_SELL_MAX * 0.5  # Buy point was touched
        elif low_dev < 0.03:
            score += BUY_SELL_MAX * 0.3  # Was close

    if actual_high is not None and actual_high > 0 and sell_point > 0:
        high_dev = abs(actual_high - sell_point) / sell_point
        if high_dev < BUY_SELL_TOUCH_THRESHOLD:
            score += BUY_SELL_MAX * 0.5  # Sell point was touched
        elif high_dev < 0.03:
            score += BUY_SELL_MAX * 0.3

    return score


def _stoploss_score(prediction: Dict, actual_low: float = None) -> float:
    """Score stop loss reasonableness: should NOT have been triggered, but be close enough."""
    stop_loss = float(prediction.get("stop_loss", 0) or 0)
    buy_point = float(prediction.get("buy_point", 0) or 0)

    if stop_loss <= 0 or buy_point <= 0:
        return 0

    # Check stop is below buy (already validated at parse time)
    if stop_loss >= buy_point:
        return 0

    # Ideal stop distance: 2-6% below buy_point
    stop_distance_pct = (buy_point - stop_loss) / buy_point
    if 0.02 <= stop_distance_pct <= 0.06:
        score = STOP_LOSS_MAX * 0.7
    elif 0.01 <= stop_distance_pct <= 0.10:
        score = STOP_LOSS_MAX * 0.4
    else:
        score = STOP_LOSS_MAX * 0.1

    # If actual low is available, check if stop was triggered
    if actual_low is not None and actual_low > 0:
        if actual_low <= stop_loss:
            # Stop was triggered — still give partial credit if it limited loss
            score = STOP_LOSS_MAX * 0.3
        else:
            # Not triggered — bonus for safety
            score = min(STOP_LOSS_MAX, score + STOP_LOSS_MAX * 0.3)

    return score


def _legacy_score(
    prediction: Dict,
    actual_change: float = None,
    is_correct: bool = None,
    is_near_miss: bool = False,
) -> float:
    """Legacy scoring for old-format predictions (up/down/neutral)."""
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

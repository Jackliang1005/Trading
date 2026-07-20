#!/usr/bin/env python3
"""Prediction response parser — 3-day K-line + buy/sell points format."""

from __future__ import annotations

import json
from typing import Dict, List

VALID_TRENDS = {"bullish", "bearish", "ranging"}
VALID_STRATEGIES = {"technical", "fundamental", "sentiment", "geopolitical"}
KLINE_KEYS = {"open", "high", "low", "close", "pattern"}


def _validate_kline(day: dict, label: str) -> bool:
    """Validate a single kline_day dict has all required keys with sensible values."""
    if not isinstance(day, dict):
        return False
    if not KLINE_KEYS.issubset(day.keys()):
        return False
    try:
        o, h, l, c = float(day["open"]), float(day["high"]), float(day["low"]), float(day["close"])
    except (ValueError, TypeError):
        return False
    if not (l <= o <= h and l <= c <= h):
        # basic OHLC sanity: low <= open/close <= high
        return False
    if not isinstance(day.get("pattern"), str) or not day["pattern"].strip():
        return False
    return True


def _price_in_range(price: float, current: float) -> bool:
    """Price must be within ±15% of current_price."""
    if current <= 0:
        return True
    deviation = abs(price - current) / current
    return deviation <= 0.15


def parse_prediction_output(llm_output: str) -> List[Dict]:
    text = llm_output.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        text = text[start:end + 1]

    predictions = json.loads(text)
    if not isinstance(predictions, list):
        predictions = [predictions]

    valid: List[Dict] = []
    for item in predictions:
        code = item.get("code", "").strip()
        if not code:
            continue

        trend = item.get("trend_3d", "").strip().lower()
        if trend not in VALID_TRENDS:
            continue

        current_price = float(item.get("current_price", 0) or 0)

        # Validate 3 kline days
        kline_ok = True
        for day_key in ("kline_day1", "kline_day2", "kline_day3"):
            day = item.get(day_key)
            if not _validate_kline(day, day_key):
                kline_ok = False
                break
        if not kline_ok:
            continue

        # Validate buy/sell/stop
        try:
            buy_point = float(item.get("buy_point", 0) or 0)
            sell_point = float(item.get("sell_point", 0) or 0)
            stop_loss = float(item.get("stop_loss", 0) or 0)
        except (ValueError, TypeError):
            continue

        if buy_point <= 0 or sell_point <= 0 or stop_loss <= 0:
            continue
        if stop_loss >= buy_point:
            continue  # stop must be below buy for long logic
        if sell_point <= buy_point:
            continue  # sell must be above buy

        # Price reasonableness (±15% of current)
        if current_price > 0:
            if not all(
                _price_in_range(p, current_price)
                for p in (buy_point, sell_point, stop_loss)
            ):
                continue
            # Also check kline day prices
            for day_key in ("kline_day1", "kline_day2", "kline_day3"):
                day = item[day_key]
                for k in ("open", "high", "low", "close"):
                    if not _price_in_range(float(day[k]), current_price):
                        kline_ok = False
                        break
                if not kline_ok:
                    break
            if not kline_ok:
                continue

        confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5) or 0.5)))
        predicted_return = float(item.get("predicted_return_3d", 0) or 0)
        strategy = item.get("strategy_used", "technical").strip().lower()
        if strategy not in VALID_STRATEGIES:
            strategy = "technical"

        valid.append({
            "code": code,
            "name": item.get("name", code),
            "current_price": current_price,
            "trend_3d": trend,
            "predicted_return_3d": predicted_return,
            "kline_day1": item["kline_day1"],
            "kline_day2": item["kline_day2"],
            "kline_day3": item["kline_day3"],
            "buy_point": buy_point,
            "sell_point": sell_point,
            "stop_loss": stop_loss,
            "confidence": confidence,
            "strategy_used": strategy,
            "reasoning": str(item.get("reasoning", ""))[:200],
            # Preserve prediction_type if passed through (index vs position)
            "prediction_type": item.get("prediction_type", "index"),
        })
    return valid

#!/usr/bin/env python3
"""Prediction response parser."""

from __future__ import annotations

import json
from typing import Dict, List


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
        if not item.get("code") or not item.get("direction"):
            continue
        if item["direction"] not in ("up", "down", "neutral"):
            continue
        item["confidence"] = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        item["predicted_change"] = float(item.get("predicted_change", 0.0))
        valid.append(item)
    return valid


#!/usr/bin/env python3
from __future__ import annotations

from typing import Dict


def should_require_human_review(task: Dict) -> bool:
    severity = str(task.get("severity", "P2")).upper()
    summary = str(task.get("summary", ""))
    if severity == "P0":
        return True
    high_risk_keywords = ("order", "cancel", "risk", "execution", "position")
    return any(keyword in summary.lower() for keyword in high_risk_keywords)

#!/usr/bin/env python3
from __future__ import annotations

from typing import Dict, List


CODE_ERROR_HINTS = ("Traceback", "ImportError", "ModuleNotFoundError", "AttributeError", "KeyError", "TypeError")


def route_incidents(incidents: List[Dict]) -> List[Dict]:
    routed = []
    for incident in incidents:
        evidence_text = str(incident.get("evidence", ""))
        needs_codex = any(hint in evidence_text for hint in CODE_ERROR_HINTS) or incident.get("kind") in {"trade_log_error", "strategy_log_error"}
        item = dict(incident)
        item["action"] = "codex_fix_task" if needs_codex else "observe"
        routed.append(item)
    return routed

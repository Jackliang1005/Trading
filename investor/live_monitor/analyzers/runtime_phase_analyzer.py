#!/usr/bin/env python3
from __future__ import annotations

from typing import Dict, List


STALLED_PHASES = {"loading_strategy", "starting_engine"}


def analyze_runtime_phase(runtime_status: Dict) -> List[Dict]:
    incidents = []
    for item in runtime_status.get("statuses", []):
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        phase = str(data.get("phase", ""))
        if phase in STALLED_PHASES and str(data.get("status", "running")).lower() == "running":
            incidents.append(
                {
                    "severity": "P2",
                    "kind": "runtime_phase_watch",
                    "signature": f"runtime_phase_watch::{item.get('path','')}::{phase}",
                    "summary": f"runtime currently in sensitive phase={phase}",
                    "evidence": item,
                }
            )
    return incidents

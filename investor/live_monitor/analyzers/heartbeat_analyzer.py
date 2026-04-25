#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime
from typing import Dict, List


def _parse_iso(value: str):
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def analyze_heartbeat(runtime_status: Dict, stale_after_seconds: int = 600) -> List[Dict]:
    incidents = []
    now = datetime.now()
    for item in runtime_status.get("statuses", []):
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        updated_at = _parse_iso(str(data.get("updated_at", "")))
        status = str(data.get("status", "")).lower()
        phase = str(data.get("phase", ""))
        age_seconds = None
        if updated_at is not None:
            if updated_at.tzinfo is not None:
                updated_at = updated_at.astimezone().replace(tzinfo=None)
            age_seconds = (now - updated_at).total_seconds()
        if status == "error":
            incidents.append(
                {
                    "severity": "P1",
                    "kind": "runtime_status_error",
                    "signature": f"runtime_status_error::{item.get('path','')}",
                    "summary": f"runtime status reports error in phase={phase}",
                    "evidence": item,
                }
            )
        if age_seconds is not None and age_seconds > stale_after_seconds:
            incidents.append(
                {
                    "severity": "P1",
                    "kind": "runtime_heartbeat_stale",
                    "signature": f"runtime_heartbeat_stale::{item.get('path','')}",
                    "summary": f"runtime heartbeat stale for {int(age_seconds)}s in phase={phase}",
                    "evidence": item,
                }
            )
    return incidents

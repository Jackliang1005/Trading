#!/usr/bin/env python3
from __future__ import annotations

import json
from typing import Dict, List

from live_monitor.config import QMTTRADER_RUNTIME_ROOT

RUNTIME_DIR = QMTTRADER_RUNTIME_ROOT


def collect_runtime_status() -> Dict:
    statuses: List[Dict] = []
    if RUNTIME_DIR.exists():
        for path in sorted(RUNTIME_DIR.glob("*_status.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                statuses.append({"path": str(path), "data": data})
            except Exception as exc:
                statuses.append({"path": str(path), "error": str(exc)})
    return {"kind": "runtime_status", "statuses": statuses}

#!/usr/bin/env python3
from __future__ import annotations

import json
from typing import Dict, List

from live_monitor.config import QMTTRADER_RUNTIME_ROOT

RUNTIME_DIR = QMTTRADER_RUNTIME_ROOT


def collect_observability(limit: int = 10) -> Dict:
    entries: List[Dict] = []
    if RUNTIME_DIR.exists():
        files = sorted(RUNTIME_DIR.glob("**/*_observability_*.json"), reverse=True)[:limit]
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                entries.append({"path": str(path), "data": data})
            except Exception as exc:
                entries.append({"path": str(path), "error": str(exc)})
    return {"kind": "observability", "entries": entries}

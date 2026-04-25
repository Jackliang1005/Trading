#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from live_monitor.config import QMTTRADER_LOG_ROOT

LOG_ROOT = QMTTRADER_LOG_ROOT


def _tail_text_file(path: Path, lines: int = 200) -> List[str]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return [f"<<read error: {exc}>>"]
    if lines > 0:
        content = content[-lines:]
    return content


def collect_strategy_logs(limit_files: int = 6, lines: int = 200) -> Dict:
    entries: List[Dict] = []
    if LOG_ROOT.exists():
        files = sorted(LOG_ROOT.glob("**/*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[:limit_files]:
            stat = path.stat()
            entries.append(
                {
                    "path": str(path),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "content": _tail_text_file(path, lines=lines),
                }
            )
    return {"kind": "strategy_logs", "entries": entries}

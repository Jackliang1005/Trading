
#!/usr/bin/env python3
"""Lightweight operational status for OpenClaw Investor assistant."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

WORKSPACE = Path("/root/.openclaw/workspace")
REPORTS_DIR = WORKSPACE / "reports"

CORE_UNITS = [
    "feishu-webhook.service",
    "investor-event-watch.service",
    "trading-intraday.service",
]

TIMERS = [
    "investor-collect.timer",
    "investor-morning-brief.timer",
    "investor-global-event-scan.timer",
    "investor-health-alert.timer",
    "investor-predict.timer",
    "trading-morning.timer",
    "investor-briefing-0945.timer",
    "investor-risk-report.timer",
    "investor-briefing-1320.timer",
    "investor-briefing-1420.timer",
    "trading-evening.timer",
    "investor-closing-brief.timer",
    "qmttrader-v2-concepts.timer",
    "investor-daily-maintain.timer",
    "investor-reflect.timer",
    "investor-capability-audit.timer",
    "investor-weekly-report.timer",
]

REPORT_FILES = {
    "morning": "investor_morning_brief_latest.md",
    "risk": "investor_risk_report_latest.md",
    "closing": "investor_closing_brief_latest.md",
    "weekly": "investor_weekly_report_latest.md",
    "audit": "investor_assistant_capability_audit_latest.json",
    "health": "investor_assistant_health_latest.md",
}


def _run(args: List[str], timeout: int = 10) -> Dict[str, Any]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {"rc": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    except Exception as exc:
        return {"rc": 999, "stdout": "", "stderr": str(exc)}


def _systemctl_active(unit: str) -> str:
    return _run(["systemctl", "is-active", unit], timeout=5).get("stdout") or "unknown"


def _timer_next(unit: str) -> str:
    result = _run(["systemctl", "list-timers", "--all", "--no-pager", unit], timeout=8)
    out = result.get("stdout", "")
    for line in out.splitlines():
        if unit in line and "NEXT" not in line:
            parts = line.split()
            return " ".join(parts[:4]) if len(parts) >= 4 else line.strip()
    return "unknown"


def _file_status(name: str) -> Dict[str, Any]:
    path = REPORTS_DIR / name
    if not path.exists():
        return {"exists": False, "path": str(path)}
    stat = path.stat()
    age_hours = max(0.0, (datetime.now().timestamp() - stat.st_mtime) / 3600)
    return {"exists": True, "path": str(path), "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"), "age_hours": round(age_hours, 2), "size": stat.st_size}


def _audit_summary() -> Dict[str, Any]:
    path = REPORTS_DIR / REPORT_FILES["audit"]
    if not path.exists():
        return {"available": False, "overall": "unknown", "blocked": [], "warning": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "overall": "read_failed", "error": str(exc), "blocked": [], "warning": []}
    return {
        "available": True,
        "overall": payload.get("overall"),
        "blocked_count": payload.get("blocked_count", 0),
        "warning_count": payload.get("warning_count", 0),
        "blocked": [item.get("name") for item in payload.get("items", []) if item.get("status") == "blocked"],
        "warning": [item.get("name") for item in payload.get("items", []) if item.get("status") == "warn"],
        "generated_at": payload.get("generated_at", ""),
    }


def build_assistant_status() -> Dict[str, Any]:
    units = {unit: _systemctl_active(unit) for unit in CORE_UNITS}
    timers = {unit: {"active": _systemctl_active(unit), "next": _timer_next(unit)} for unit in TIMERS}
    reports = {key: _file_status(name) for key, name in REPORT_FILES.items()}
    audit = _audit_summary()
    status = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "units": units,
        "timers": timers,
        "reports": reports,
        "audit": audit,
    }
    status["text"] = format_assistant_status(status)
    return status


def format_assistant_status(status: Dict[str, Any]) -> str:
    audit = status.get("audit") or {}
    lines = [
        "OpenClaw Assistant Status",
        f"generated_at: {status.get('generated_at')}",
        f"audit: {audit.get('overall')} | blocked={audit.get('blocked_count', 0)} warn={audit.get('warning_count', 0)}",
    ]
    if audit.get("blocked"):
        lines.append("blocked_items: " + ", ".join(str(x) for x in audit.get("blocked") or []))
    lines.extend(["", "core services:"])
    for unit, active in (status.get("units") or {}).items():
        lines.append(f"- {unit}: {active}")
    lines.extend(["", "key timers:"])
    for unit in TIMERS:
        item = (status.get("timers") or {}).get(unit, {})
        lines.append(f"- {unit}: {item.get('active')} next={item.get('next')}")
    lines.extend(["", "latest reports:"])
    for key in ("morning", "risk", "closing", "weekly", "audit", "health"):
        item = (status.get("reports") or {}).get(key, {})
        if item.get("exists"):
            lines.append(f"- {key}: age={item.get('age_hours')}h mtime={item.get('mtime')} size={item.get('size')}")
        else:
            lines.append(f"- {key}: missing")
    return "\n".join(lines)

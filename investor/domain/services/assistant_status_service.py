
#!/usr/bin/env python3
"""Lightweight operational status for OpenClaw Investor assistant."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

WORKSPACE = Path("/root/.openclaw/workspace")
REPORTS_DIR = WORKSPACE / "reports"
HEALTH_STATE_PATH = WORKSPACE / "runtime" / "investor_health_alert_state.json"

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
    "investor-decision-0935.timer",
    "investor-briefing-0945.timer",
    "investor-decision-1030.timer",
    "investor-risk-report.timer",
    "investor-briefing-1320.timer",
    "investor-briefing-1420.timer",
    "investor-outlook-1430.timer",
    "trading-evening.timer",
    "investor-closing-brief.timer",
    "qmttrader-v2-concepts.timer",
    "investor-daily-maintain.timer",
    "investor-mootdx-finance-cache.timer",
    "investor-mootdx-momentum-cache.timer",
    "investor-mootdx-industry-cache.timer",
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
    "health": str(HEALTH_STATE_PATH),
}

CORE_UNIT_LABELS = {
    "feishu-webhook.service": "飞书命令服务",
    "investor-event-watch.service": "事件监控",
    "trading-intraday.service": "盘中监控",
}

REPORT_TIMER_LABELS = {
    "investor-morning-brief.timer": "08:30 晨报",
    "investor-predict.timer": "09:30 开盘预测",
    "investor-decision-1030.timer": "10:30 走势修正",
    "investor-outlook-1430.timer": "14:30 预测复盘",
    "investor-closing-brief.timer": "收盘简报",
    "investor-risk-report.timer": "持仓风险报告",
    "investor-reflect.timer": "每日交易复盘",
    "investor-weekly-report.timer": "周报",
}

REPORT_FILE_LABELS = {
    "morning": "晨报",
    "risk": "风险报告",
    "closing": "收盘简报",
    "weekly": "周报",
    "audit": "能力审计",
    "health": "最近自动健康探针",
}

AUDIT_ITEM_LABELS = {
    "holdings_account_monitor": "持仓与账户监控",
    "feishu_push_entry_inventory": "飞书推送入口清单",
    "global_impact_command_center": "全球事件影响指挥台",
    "post_market_closing_brief": "盘后收盘简报",
    "pre_market_morning_brief": "盘前晨报",
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


def _human_timer_next(value: object) -> str:
    text = str(value or "").strip()
    match = re.search(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(20\d{2})-(\d{2})-(\d{2})\s+(\d{2}:\d{2})", text)
    if not match:
        return "等待 systemd 给出下次时间" if text in {"", "unknown", "n/a"} else text
    year, month, day, hm = match.groups()
    try:
        weekday = "一二三四五六日"[datetime(int(year), int(month), int(day)).weekday()]
    except (ValueError, IndexError):
        weekday = "?"
    return f"{int(month)}月{int(day)}日（周{weekday}）{hm}"


def _file_status(name: str) -> Dict[str, Any]:
    candidate = Path(name)
    path = candidate if candidate.is_absolute() else REPORTS_DIR / candidate
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
        "🧭 OpenClaw 投资助理运行状态",
        f"检查时间：{status.get('generated_at') or '未知'}",
        "",
        "**核心服务**",
    ]
    for unit, active in (status.get("units") or {}).items():
        label = CORE_UNIT_LABELS.get(unit, unit)
        lines.append(f"- {label}：{'正常' if active == 'active' else '异常（' + str(active) + '）'}")
    lines.extend(["", "**核心报告时间**"])
    for unit, label in REPORT_TIMER_LABELS.items():
        item = (status.get("timers") or {}).get(unit, {})
        if item.get("active") == "active":
            lines.append(f"- {label}：已启用，下次 {_human_timer_next(item.get('next'))}")
        else:
            lines.append(f"- {label}：未正常启用（{item.get('active') or '未知'}）")
    lines.extend(["", "**最新报告**"])
    for key in ("morning", "risk", "closing", "weekly", "audit", "health"):
        item = (status.get("reports") or {}).get(key, {})
        label = REPORT_FILE_LABELS.get(key, key)
        if item.get("exists"):
            lines.append(f"- {label}：{item.get('mtime')}，距今 {float(item.get('age_hours') or 0):.1f} 小时")
        else:
            lines.append(f"- {label}：尚未生成")
    blocked = int(audit.get("blocked_count", 0) or 0)
    warnings = int(audit.get("warning_count", 0) or 0)
    lines.extend(["", "**能力检查**", f"- 阻断 {blocked} 项，警告 {warnings} 项。"])
    warning_names = [AUDIT_ITEM_LABELS.get(str(name), str(name)) for name in (audit.get("warning") or []) if name]
    blocked_names = [AUDIT_ITEM_LABELS.get(str(name), str(name)) for name in (audit.get("blocked") or []) if name]
    if warning_names:
        lines.append(f"- 警告项：{'、'.join(warning_names)}。")
    if blocked_names:
        lines.append(f"- 阻断项：{'、'.join(blocked_names)}。")
    if audit.get("generated_at"):
        lines.append(f"- 审计时间：{audit.get('generated_at')}。")
    if blocked:
        lines.append("- 存在阻断项时，相关报告应按数据不可用降级，不得补齐结论。")
    lines.extend(["", "**说明**", "- 这是运行状态，不是交易信号；系统不会因状态检查自动下单。"])
    return "\n".join(lines)

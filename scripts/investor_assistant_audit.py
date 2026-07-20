#!/usr/bin/env python3
"""Capability audit for the OpenClaw A-share investment assistant."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

WORKSPACE = Path("/root/.openclaw/workspace")
INVESTOR_DIR = WORKSPACE / "investor"
TRADING_DIR = WORKSPACE / "trading"
REPORT_DIR = WORKSPACE / "reports"
LATEST_JSON = REPORT_DIR / "investor_assistant_capability_audit_latest.json"
LATEST_MD = REPORT_DIR / "investor_assistant_capability_audit_latest.md"


def _run(cmd: List[str], timeout: int = 20, cwd: Path | None = None) -> Dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)
        return {"ok": proc.returncode == 0, "rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "ms": round((time.time() - started) * 1000, 1)}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "rc": 124, "stdout": exc.stdout or "", "stderr": exc.stderr or "timeout", "ms": round((time.time() - started) * 1000, 1)}
    except Exception as exc:
        return {"ok": False, "rc": 1, "stdout": "", "stderr": str(exc), "ms": round((time.time() - started) * 1000, 1)}


def _systemctl(unit: str, mode: str = "is-active") -> str:
    result = _run(["systemctl", mode, unit], timeout=5)
    return (result["stdout"] or result["stderr"]).strip()


def _timer_next(unit: str) -> str:
    result = _run(["systemctl", "list-timers", "--all", "--no-pager", unit], timeout=8)
    lines = [line for line in result["stdout"].splitlines() if unit in line]
    return lines[0].strip() if lines else ""


def _db_scalar(sql: str, default: Any = None) -> Any:
    path = INVESTOR_DIR / "data" / "investor.db"
    if not path.exists():
        return default
    try:
        conn = sqlite3.connect(str(path))
        cur = conn.cursor()
        cur.execute(sql)
        row = cur.fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


def _file_age_days(path: Path) -> float | None:
    if not path.exists():
        return None
    return round((time.time() - path.stat().st_mtime) / 86400, 2)


def _cli_contains(command: List[str], needle: str, timeout: int = 20, cwd: Path = INVESTOR_DIR) -> Dict[str, Any]:
    result = _run(command, timeout=timeout, cwd=cwd)
    result["contains"] = needle in (result.get("stdout") or "")
    return result


def _item(name: str, status: str, evidence: List[str], action: str = "") -> Dict[str, Any]:
    return {"name": name, "status": status, "evidence": evidence, "action": action}


def _push_inventory_item() -> Dict[str, Any]:
    result = _run(["python3", "scripts/report_push_inventory.py", "--json"], timeout=60, cwd=WORKSPACE)
    try:
        payload = json.loads(result.get("stdout") or "{}")
    except json.JSONDecodeError:
        payload = {"ok": False, "issues": ["入口校验器输出无法解析"]}
    timers = payload.get("enabled_timers", []) or []
    sources = payload.get("send_sources", []) or []
    crons = payload.get("enabled_openclaw_crons", []) or []
    issues = [str(item) for item in (payload.get("issues", []) or [])]
    ok = bool(result.get("ok")) and bool(payload.get("ok")) and not issues
    evidence = [
        f"enabled systemd entries={len(timers)}",
        f"enabled OpenClaw crons={len(crons)}",
        f"registered send sources={len(sources)}",
    ]
    evidence.extend(issues[:8])
    return _item(
        "feishu_push_entry_inventory",
        "ok" if ok else "warn",
        evidence,
        "Run scripts/report_push_inventory.py and register or disable every unknown push entry.",
    )


def build_audit() -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    items.append(_push_inventory_item())

    event_today = _run(["python3", "main.py", "event-today"], timeout=20, cwd=INVESTOR_DIR)
    event_service = _systemctl("investor-event-watch.service")
    event_env = Path("/etc/default/investor-event-watch").read_text(encoding="utf-8", errors="ignore") if Path("/etc/default/investor-event-watch").exists() else ""
    event_ok = event_service == "active" and "INVESTOR_FEISHU_TARGET=" in event_env and "count" in event_today["stdout"]
    items.append(_item(
        "financial_news_event_push",
        "ok" if event_ok else "warn",
        [f"investor-event-watch.service={event_service}", f"event-today rc={event_today['rc']} ms={event_today['ms']}", "INVESTOR_FEISHU_TARGET configured" if "INVESTOR_FEISHU_TARGET=" in event_env else "missing INVESTOR_FEISHU_TARGET", (event_today["stdout"] or event_today["stderr"])[:300].replace("\n", " | ")],
        "Check event sources/Feishu target if event-today fails or no new events are stored.",
    ))

    watchlist = _cli_contains(["python3", "main.py", "watchlist-report", "--top", "3"], "事件驱动观察池", timeout=20)
    watchlist_menu = _cli_contains(["python3", "main.py", "assistant-menu"], "/关注", timeout=10)
    items.append(_item(
        "event_driven_watchlist",
        "ok" if watchlist.get("contains") and watchlist_menu.get("contains") else "warn",
        [f"cli_has_watchlist_report={watchlist.get('contains')}", f"menu_has_watchlist_report={watchlist_menu.get('contains')}"],
        "Inspect watchlist-report CLI and menu entry if event-driven watchlist stops.",
    ))

    global_timer = _systemctl("investor-global-event-scan.timer")
    global_help = _cli_contains(["python3", "main.py", "help"], "global-event-scan", timeout=10)
    global_brief = _cli_contains(["python3", "main.py", "global-event-brief", "--limit", "10", "--top", "1", "--json"], "top_events", timeout=35)
    global_menu = _cli_contains(["python3", "main.py", "assistant-menu"], "/影响", timeout=10)
    items.append(_item(
        "global_breaking_news_radar",
        "ok" if global_timer == "active" and global_help.get("contains") and global_brief.get("contains") and global_menu.get("contains") else "warn",
        [f"investor-global-event-scan.timer={global_timer} next={_timer_next('investor-global-event-scan.timer')}", f"cli_has_global_event_scan={global_help.get('contains')}", f"cli_has_global_event_brief={global_brief.get('contains')}", f"menu_uses_global_event_brief={global_menu.get('contains')}", "sources=Yahoo Finance,CNBC,Federal Reserve,ECB,WSJ Markets"],
        "Inspect investor-global-event-scan.timer, global-event-scan, and global-event-brief CLI if global radar stops.",
    ))

    impact_brief = _cli_contains(["python3", "main.py", "global-impact-brief", "--limit", "10", "--top", "2", "--json"], "urgent_events", timeout=45)
    impact_menu = _cli_contains(["python3", "main.py", "assistant-menu"], "/影响", timeout=10)
    items.append(_item(
        "global_impact_command_center",
        "ok" if impact_brief.get("contains") and impact_menu.get("contains") else "warn",
        [f"cli_has_urgent_events={impact_brief.get('contains')}", f"menu_has_global_impact={impact_menu.get('contains')}"],
        "Inspect global-impact-brief and Feishu /impact route if the global command center stops.",
    ))

    dongguan = _run(["python3", "-c", "import json,urllib.request; h={'X-API-Token':'998811','Authorization':'Bearer 998811'}; r=urllib.request.urlopen(urllib.request.Request('http://150.158.31.115:8085/api/stock/positions',headers=h),timeout=8); d=json.loads(r.read().decode()); print(d.get('success'), len(d.get('data') or []))"], timeout=12)
    degraded_positions = _run(["python3", "main.py", "feishu-query", "/\u6301\u4ed3"], timeout=30, cwd=INVESTOR_DIR)
    guojin_probe = _run(["python3", "scripts/qmt2http_remote_recovery.py", "--server", "guojin", "--timeout", "4"], timeout=35, cwd=WORKSPACE)
    guojin_bad = guojin_probe["rc"] == 2
    items.append(_item(
        "holdings_account_monitor",
        "blocked" if guojin_bad else ("ok" if dongguan["ok"] else "warn"),
        [f"dongguan positions rc={dongguan['rc']} output={(dongguan['stdout'] or dongguan['stderr']).strip()}", f"degraded_positions rc={degraded_positions['rc']} has_fallback={'fallback_snapshot' in (degraded_positions['stdout'] or degraded_positions['stderr'])}", f"guojin recovery probe rc={guojin_probe['rc']} ms={guojin_probe['ms']}", (guojin_probe["stdout"] or guojin_probe["stderr"])[:500].replace("\n", " | ")],
        "Guojin qmt2http/miniQMT requires Windows-side recovery; degraded positions fallback uses latest portfolio snapshot while Dongguan realtime remains healthy.",
    ))

    closing_timer = _systemctl("investor-closing-brief.timer")
    closing_brief = _cli_contains(["python3", "main.py", "closing-brief"], "A股收盘简报", timeout=60)
    items.append(_item(
        "post_market_closing_brief",
        "ok" if closing_timer == "active" and closing_brief.get("contains") else "warn",
        [f"investor-closing-brief.timer={closing_timer} next={_timer_next('investor-closing-brief.timer')}", f"cli_has_global_impact_close={closing_brief.get('contains')}"],
        "Inspect investor-closing-brief.timer and closing-brief CLI if post-market brief stops.",
    ))

    morning_timer = _systemctl("investor-morning-brief.timer")
    morning_brief = _cli_contains(["python3", "main.py", "morning-brief"], "A股盘前简报", timeout=60)
    items.append(_item(
        "pre_market_morning_brief",
        "ok" if morning_timer == "active" and morning_brief.get("contains") else "warn",
        [f"investor-morning-brief.timer={morning_timer} next={_timer_next('investor-morning-brief.timer')}", f"cli_has_global_impact_morning={morning_brief.get('contains')}"],
        "Inspect investor-morning-brief.timer and morning-brief CLI if pre-market brief stops.",
    ))

    risk_timer = _systemctl("investor-risk-report.timer")
    risk_report = _cli_contains(["python3", "main.py", "risk-report", "--json"], "risk_flags", timeout=15)
    items.append(_item(
        "portfolio_risk_report",
        "ok" if risk_timer == "active" and risk_report.get("contains") else "warn",
        [f"investor-risk-report.timer={risk_timer} next={_timer_next('investor-risk-report.timer')}", f"cli_has_risk_flags={risk_report.get('contains')}"],
        "Inspect investor-risk-report.timer and risk-report CLI if portfolio risk report stops.",
    ))

    timers = ["investor-health-alert.timer", "investor-morning-brief.timer", "investor-risk-report.timer", "investor-decision-0935.timer", "investor-briefing-0945.timer", "investor-decision-1030.timer", "investor-briefing-1320.timer", "investor-briefing-1420.timer", "trading-morning.timer", "trading-evening.timer", "investor-closing-brief.timer", "investor-weekly-report.timer"]
    timer_evidence = [f"{u}={_systemctl(u)} next={_timer_next(u)}" for u in timers]
    items.append(_item(
        "intraday_timed_alerts",
        "ok" if all("=active" in e for e in timer_evidence) else "warn",
        timer_evidence,
        "Inspect systemctl list-timers if any timer is inactive.",
    ))

    reflect_timer = _systemctl("investor-reflect.timer")
    reflect_reports = sorted((INVESTOR_DIR / "reflection_reports").glob("reflection_*.md"))
    latest_reflect = reflect_reports[-1].name if reflect_reports else "none"
    items.append(_item(
        "post_market_review",
        "ok" if reflect_timer == "active" and reflect_reports else "warn",
        [f"investor-reflect.timer={reflect_timer}", f"latest_reflection_report={latest_reflect}", "investor-reflect.service uses run_investor_command_push.sh" if "run_investor_command_push" in _run(["systemctl", "show", "investor-reflect.service", "-p", "ExecStart"], timeout=5)["stdout"] else "reflect push wrapper missing"],
        "If latest report is stale, inspect investor-reflect.service after next market close.",
    ))

    longterm = _run(["python3", "-m", "trading_core_new.longterm.cli", "summary"], timeout=30, cwd=TRADING_DIR)
    items.append(_item(
        "longterm_portfolio_tracking",
        "ok" if longterm["ok"] and ("NAV" in longterm["stdout"] or "nav:" in longterm["stdout"].lower()) else "warn",
        [f"longterm summary rc={longterm['rc']} ms={longterm['ms']}", (longterm["stdout"] or longterm["stderr"])[:500].replace("\n", " | ")],
        "Inspect trading longterm repository if NAV summary fails.",
    ))

    health_timer = _systemctl("investor-health-alert.timer")
    health_dry = _run(["python3", "scripts/investor_health_alert.py", "--dry-run", "--timeout", "3"], timeout=35, cwd=WORKSPACE)
    health_state = WORKSPACE / "runtime" / "investor_health_alert_state.json"
    items.append(_item(
        "service_health_diagnostics",
        "blocked" if health_dry["rc"] == 2 else ("ok" if health_timer == "active" and health_dry["ok"] else "warn"),
        [f"investor-health-alert.timer={health_timer}", f"health dry-run rc={health_dry['rc']} ms={health_dry['ms']}", f"state_file_age_days={_file_age_days(health_state)}", (health_dry["stdout"] or health_dry["stderr"])[:700].replace("\n", " | ")],
        "Current blocked state is expected while Guojin qmt2http/miniQMT trade endpoint is unhealthy.",
    ))

    assistant_status = _cli_contains(["python3", "main.py", "assistant-status"], "投资助理运行状态", timeout=20)
    status_menu = _cli_contains(["python3", "main.py", "assistant-menu"], "/状态", timeout=10)
    items.append(_item(
        "operator_status_overview",
        "ok" if assistant_status.get("contains") and status_menu.get("contains") else "warn",
        [f"cli_has_assistant_status={assistant_status.get('contains')}", f"menu_has_assistant_status={status_menu.get('contains')}"],
        "Inspect assistant-status CLI and status menu entry if status overview stops.",
    ))

    menu = _cli_contains(["python3", "main.py", "assistant-menu"], "实时数据｜不可达、非当日或回读不一致时明确降级", timeout=10)
    docs = [Path("/root/Agent.md"), WORKSPACE / "docs" / "investor_assistant_command_menu.md", WORKSPACE / "docs" / "guojin_qmt2http_recovery_runbook.md"]
    items.append(_item(
        "runbook_and_operator_menu",
        "ok" if menu.get("contains") and all(path.exists() for path in docs) else "warn",
        [f"assistant-menu has data-safety guidance={menu.get('contains')}"] + [f"{path} exists={path.exists()} age_days={_file_age_days(path)}" for path in docs],
        "Keep Agent.md and docs updated when services or recovery commands change.",
    ))

    blocking = [item for item in items if item["status"] == "blocked"]
    warning = [item for item in items if item["status"] == "warn"]
    overall = "blocked" if blocking else ("warn" if warning else "ok")
    return {"generated_at": datetime.now().strftime("%F %T %Z"), "overall": overall, "items": items, "blocked_count": len(blocking), "warning_count": len(warning)}


def write_reports(audit: Dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_JSON.write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    lines = ["# OpenClaw Investment Assistant Capability Audit", "", f"- generated_at: {audit['generated_at']}", f"- overall: {audit['overall']}", f"- blocked_count: {audit['blocked_count']}", f"- warning_count: {audit['warning_count']}", "", "| capability | status | first evidence | action |", "| --- | --- | --- | --- |"]
    for item in audit["items"]:
        evidence = (item.get("evidence") or [""])[0].replace("|", "/")
        action = item.get("action", "").replace("|", "/")
        lines.append(f"| {item['name']} | {item['status']} | {evidence} | {action} |")
    lines.append("")
    for item in audit["items"]:
        lines.append(f"## {item['name']} [{item['status']}]")
        for ev in item.get("evidence", []):
            lines.append(f"- {ev}")
        if item.get("action"):
            lines.append(f"- action: {item['action']}")
        lines.append("")
    remediation = audit.get("remediation")
    if remediation:
        lines.append("## automatic_remediation")
        lines.append(f"- status: {remediation.get('status', 'unknown')}")
        for action in remediation.get("actions", []):
            lines.append(f"- {action}")
        if remediation.get("note"):
            lines.append(f"- note: {remediation['note']}")
        lines.append("")
    LATEST_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit OpenClaw investment assistant capabilities")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--remediate", action="store_true", help="run the constrained post-audit remediator")
    args = parser.parse_args()
    audit = build_audit()
    write_reports(audit)
    if args.remediate:
        remediation = _run(
            ["python3", "scripts/investor_audit_remediator.py", "--audit-file", str(LATEST_JSON)],
            timeout=190,
            cwd=WORKSPACE,
        )
        try:
            audit["remediation"] = json.loads(remediation["stdout"] or "{}")
        except json.JSONDecodeError:
            audit["remediation"] = {
                "status": "remediator_error",
                "actions": [],
                "note": (remediation["stderr"] or remediation["stdout"] or "invalid remediator output")[:500],
            }
        write_reports(audit)
    if args.json:
        print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(LATEST_MD.read_text(encoding="utf-8"))
    return 0 if audit["overall"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Capability audit for the OpenClaw A-share investment assistant."""
from __future__ import annotations

import argparse
import json
import os
import re
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

CAPABILITY_LABELS = {
    "feishu_push_entry_inventory": "飞书推送入口治理",
    "financial_news_event_push": "财经新闻与事件推送",
    "event_driven_watchlist": "事件驱动观察池",
    "global_breaking_news_radar": "全球突发事件雷达",
    "global_impact_command_center": "全球影响指挥台",
    "holdings_account_monitor": "持仓与账户监控",
    "post_market_closing_brief": "盘后收盘简报",
    "pre_market_morning_brief": "盘前早报",
    "portfolio_risk_report": "组合风险报告",
    "intraday_timed_alerts": "盘中定时提醒",
    "post_market_review": "盘后复盘",
    "longterm_portfolio_tracking": "长期组合跟踪",
    "service_health_diagnostics": "服务健康诊断",
    "operator_status_overview": "运行状态总览",
    "runbook_and_operator_menu": "操作手册与命令菜单",
}

ACTION_LABELS = {
    "feishu_push_entry_inventory": "登记新增入口的发送策略；未知入口不得直接推送。",
    "financial_news_event_push": "检查事件源、事件服务和飞书目标配置。",
    "event_driven_watchlist": "检查观察池命令及菜单入口。",
    "global_breaking_news_radar": "检查全球事件扫描定时器与简报命令。",
    "global_impact_command_center": "检查全球影响简报及飞书命令入口。",
    "holdings_account_monitor": "检查行情/交易端连通性；保持只读降级，不自动操作 QMT。",
    "post_market_closing_brief": "检查收盘简报定时器与生成命令。",
    "pre_market_morning_brief": "检查盘前简报定时器与生成命令。",
    "portfolio_risk_report": "检查风险报告定时器与生成命令。",
    "intraday_timed_alerts": "检查未运行的定时器及其最近日志。",
    "post_market_review": "检查复盘定时器与最新报告时间。",
    "longterm_portfolio_tracking": "检查长期组合仓储与净值汇总。",
    "service_health_diagnostics": "检查健康任务；外部终端异常仅提示人工处理。",
    "operator_status_overview": "检查状态命令及菜单入口。",
    "runbook_and_operator_menu": "同步更新操作手册与命令菜单。",
}

STATUS_LABELS = {"ok": "正常", "warn": "需关注", "blocked": "受阻"}


def _safe_text(value: Any, limit: int = 240) -> str:
    """Keep reports useful without exposing endpoints, identifiers, or tokens."""
    text = str(value or "").replace("\r", " ").replace("\n", " | ")
    text = re.sub(r"https?://[^\s|]+", "[endpoint]", text, flags=re.I)
    text = re.sub(r"(?i)(account_id|token|authorization|fingerprint)\s*[:=]\s*[^,|}\s]+", r"\1=[redacted]", text)
    text = re.sub(r"\b\d{8,}\b", "[id]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _json_payload(output: str) -> Dict[str, Any]:
    """Parse JSON even when a CLI writes a short startup line before it."""
    start = (output or "").find("{")
    if start < 0:
        return {}
    try:
        value = json.loads(output[start:])
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


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
    result = _run(["python3", "scripts/report_push_inventory.py", "--json"], timeout=120, cwd=WORKSPACE)
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
        f"OpenClaw cron source={payload.get('openclaw_cron_source', 'unknown')}",
        f"registered send sources={len(sources)}",
    ]
    if not result.get("ok"):
        evidence.insert(0, f"inventory checker rc={result.get('rc')} ms={result.get('ms')}")
        diagnostic = result.get("stderr") or result.get("stdout")
        if diagnostic:
            evidence.append("inventory checker detail=" + _safe_text(diagnostic))
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
    event_payload = _json_payload(event_today["stdout"])
    event_count = event_payload.get("count")
    event_ok = event_service == "active" and "INVESTOR_FEISHU_TARGET=" in event_env and isinstance(event_count, int)
    items.append(_item(
        "financial_news_event_push",
        "ok" if event_ok else "warn",
        [f"investor-event-watch.service={event_service}", f"event-today rc={event_today['rc']} ms={event_today['ms']} count={event_count if isinstance(event_count, int) else 'unknown'}", "INVESTOR_FEISHU_TARGET configured" if "INVESTOR_FEISHU_TARGET=" in event_env else "missing INVESTOR_FEISHU_TARGET"],
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
        [f"dongguan positions reachable={dongguan['ok']}", f"degraded_positions rc={degraded_positions['rc']} has_fallback={'fallback_snapshot' in (degraded_positions['stdout'] or degraded_positions['stderr'])}", f"guojin recovery probe rc={guojin_probe['rc']} ms={guojin_probe['ms']}"],
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
    timer_states = {u: _systemctl(u) for u in timers}
    inactive_timers = [u for u, state in timer_states.items() if state != "active"]
    timer_evidence = [f"active_timers={len(timers) - len(inactive_timers)}/{len(timers)}"]
    if inactive_timers:
        timer_evidence.append("inactive_timers=" + ",".join(inactive_timers))
    items.append(_item(
        "intraday_timed_alerts",
        "ok" if not inactive_timers else "warn",
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
        [f"longterm summary rc={longterm['rc']} ms={longterm['ms']} nav_present={'NAV' in longterm['stdout'] or 'nav:' in longterm['stdout'].lower()}"],
        "Inspect trading longterm repository if NAV summary fails.",
    ))

    health_timer = _systemctl("investor-health-alert.timer")
    health_dry = _run(["python3", "scripts/investor_health_alert.py", "--dry-run", "--timeout", "3"], timeout=35, cwd=WORKSPACE)
    health_attempts = 1
    recovered_on_retry = False
    if health_dry["rc"] == 2:
        # External QMT reads occasionally cross the short probe deadline. A
        # second, slightly wider read-only probe prevents one sample from
        # turning a healthy capability audit into a user-facing incident.
        health_attempts = 2
        health_retry = _run(["python3", "scripts/investor_health_alert.py", "--dry-run", "--timeout", "5"], timeout=45, cwd=WORKSPACE)
        recovered_on_retry = health_retry["rc"] == 0
        health_dry = health_retry
    health_payload = _json_payload(health_dry.get("stdout") or "")
    health_issues = ((health_payload.get("health") or {}).get("issues") or []) if health_payload else []
    health_state = WORKSPACE / "runtime" / "investor_health_alert_state.json"
    health_evidence = [f"investor-health-alert.timer={health_timer}", f"health dry-run rc={health_dry['rc']} ms={health_dry['ms']} issues={len(health_issues)} attempts={health_attempts}", f"state_file_age_days={_file_age_days(health_state)}"]
    if recovered_on_retry:
        health_evidence.append("transient probe failure recovered on read-only retry")
    health_evidence.extend(f"issue={_safe_text(issue)}" for issue in health_issues[:8])
    items.append(_item(
        "service_health_diagnostics",
        "blocked" if health_dry["rc"] == 2 else ("ok" if health_timer == "active" and health_dry["ok"] else "warn"),
        health_evidence,
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
    return {"generated_at": datetime.now().astimezone().strftime("%F %T %z"), "overall": overall, "items": items, "blocked_count": len(blocking), "warning_count": len(warning)}


def write_reports(audit: Dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_JSON.write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    findings = [item for item in audit["items"] if item.get("status") != "ok"]
    healthy = [item for item in audit["items"] if item.get("status") == "ok"]
    overall_label = {"ok": "全部正常", "warn": "存在需关注项", "blocked": "存在受阻能力"}.get(audit["overall"], audit["overall"])
    lines = [
        "# OpenClaw 投资助理能力审计",
        "",
        f"**结论：{overall_label}**",
        f"- 检查时间：{audit['generated_at']}",
        f"- 能力总数：{len(audit['items'])}｜正常：{len(healthy)}｜需关注：{audit['warning_count']}｜受阻：{audit['blocked_count']}",
        "",
    ]
    if findings:
        lines.extend(["## 需要处理", "", "| 能力 | 状态 | 证据摘要 | 建议动作 |", "| --- | --- | --- | --- |"])
        for item in findings:
            evidence = _safe_text((item.get("evidence") or ["暂无证据"])[0]).replace("|", "/")
            action = ACTION_LABELS.get(item["name"], _safe_text(item.get("action"))).replace("|", "/")
            lines.append(f"| {CAPABILITY_LABELS.get(item['name'], item['name'])} | {STATUS_LABELS.get(item['status'], item['status'])} | {evidence} | {action} |")
        lines.append("")
        for item in findings:
            lines.append(f"### {CAPABILITY_LABELS.get(item['name'], item['name'])}")
            for evidence in item.get("evidence", []):
                lines.append(f"- {_safe_text(evidence)}")
            lines.append(f"- 建议：{ACTION_LABELS.get(item['name'], _safe_text(item.get('action')))}")
            lines.append("")
    else:
        lines.extend(["无需人工处理；正常结果不推送飞书。", ""])
    lines.extend(["## 已通过", "", "、".join(CAPABILITY_LABELS.get(item["name"], item["name"]) for item in healthy), ""])
    remediation = audit.get("remediation")
    if remediation and remediation.get("status") != "not_needed":
        lines.append("## 自动修复")
        lines.append(f"- 状态：{remediation.get('status', 'unknown')}")
        for action in remediation.get("actions", []):
            lines.append(f"- {_safe_text(action)}")
        if remediation.get("note"):
            lines.append(f"- 说明：{_safe_text(remediation['note'])}")
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

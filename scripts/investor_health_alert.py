#!/usr/bin/env python3
"""Push throttled OpenClaw investment assistant health alerts to Feishu."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Set

WORKSPACE = Path("/root/.openclaw/workspace")
INVESTOR_DIR = WORKSPACE / "investor"
STATE_PATH = WORKSPACE / "runtime" / "investor_health_alert_state.json"
DEFAULT_TARGET = "ou_f7d5ef82efd4396dea7a604691c56f75"
WATCH_UNITS = [
    "feishu-webhook.service",
    "investor-event-watch.service",
    "trading-intraday.service",
    "investor-collect.timer",
    "investor-morning-brief.timer",
    "investor-predict.timer",
    "trading-morning.timer",
    "investor-decision-0935.timer",
    "investor-briefing-0945.timer",
    "investor-decision-1030.timer",
    "investor-outlook-1430.timer",
    "investor-briefing-1320.timer",
    "investor-briefing-1420.timer",
    "investor-risk-report.timer",
    "trading-evening.timer",
    "investor-closing-brief.timer",
    "qmttrader-v2-concepts.timer",
    "investor-daily-maintain.timer",
    "investor-reflect.timer",
    "investor-weekly-report.timer",
    "investor-capability-audit.timer",
    "investor-global-event-scan.timer",
]

sys.path.insert(0, str(WORKSPACE / "trading"))
from trading_core_new.longterm.notifier import build_diagnostic_card, push_feishu_rich, record_feishu_delivery


def _run(cmd: List[str], timeout: int = 20, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)


def _systemctl_state(unit: str) -> Dict[str, str]:
    enabled = _run(["systemctl", "is-enabled", unit], timeout=5)
    active = _run(["systemctl", "is-active", unit], timeout=5)
    return {"unit": unit, "enabled": (enabled.stdout or enabled.stderr).strip(), "active": (active.stdout or active.stderr).strip()}


def _load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _headers() -> Dict[str, str]:
    sys.path.insert(0, str(INVESTOR_DIR))
    try:
        from live_monitor.collectors.qmt_auth import build_qmt_auth_headers
        return build_qmt_auth_headers()
    except Exception:
        return {"Accept": "application/json"}


def _candidate_servers() -> List[Dict[str, str]]:
    pairs = [
        ("guojin", os.getenv("QMT2HTTP_MAIN_URL", os.getenv("QMT2HTTP_BASE_URL", "http://39.105.48.176:8085")).strip()),
        ("dongguan", os.getenv("QMT2HTTP_DONGGUAN_BASE_URL", os.getenv("QMT2HTTP_TRADE_URL", "http://150.158.31.115:8085")).strip()),
    ]
    seen = set()
    servers = []
    for name, url in pairs:
        if not url or url in seen:
            continue
        seen.add(url)
        servers.append({"name": name, "base_url": url.rstrip("/")})
    return servers


def _fetch_json(base_url: str, path: str, timeout: float) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    started = time.time()
    payload = None
    error = None
    status_code = None
    try:
        req = urllib.request.Request(url, headers=_headers(), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"raw": raw[:500]}
        error = f"HTTP {exc.code}"
    except Exception as exc:
        error = str(exc)
    return {"ok": bool(payload and payload.get("success")) and not error, "http_status": status_code, "error": error, "latency_ms": round((time.time() - started) * 1000, 1), "url": url, "payload": payload or {}}


def _probe_qmt2http(timeout: float) -> Dict[str, Any]:
    today = date.today().isoformat()
    params = urllib.parse.urlencode({"lines": 20, "include_content": "false", "date": today, "kind": "all", "max_files": 8})
    checks = {
        "health": "/health",
        "positions": "/api/stock/positions",
        "qmttrader_v2_status": "/api/qmttrader_v2/status",
        "qmttrader_v2_logs": f"/api/qmttrader_v2/logs?{params}",
    }
    servers = _candidate_servers()
    rows_by_name: Dict[str, Dict[str, Any]] = {server["name"]: {"server": server["name"]} for server in servers}
    with ThreadPoolExecutor(max_workers=max(1, len(servers) * len(checks))) as pool:
        futures = {}
        for server in servers:
            for key, path in checks.items():
                future = pool.submit(_fetch_json, server["base_url"], path, timeout)
                futures[future] = (server["name"], key)
        for future in as_completed(futures):
            server_name, key = futures[future]
            try:
                rows_by_name[server_name][key] = future.result()
            except Exception as exc:
                rows_by_name[server_name][key] = {"ok": False, "http_status": None, "error": str(exc), "latency_ms": 0, "payload": {}}
    return {"servers": [rows_by_name[server["name"]] for server in servers]}


def _concept_db_status() -> Dict[str, Any]:
    path = Path("/root/qmttrader_v2/concept_db/concepts.db")
    if not path.exists():
        return {"ok": False, "issue": "concept_db_missing"}
    try:
        conn = sqlite3.connect(str(path))
        cur = conn.cursor()
        cur.execute("select max(date), count(*) from hot_concepts")
        hot_date, hot_count = cur.fetchone()
        cur.execute("select max(date), count(*) from concept_stocks")
        stock_date, stock_count = cur.fetchone()
        conn.close()
        ok = bool(hot_date and stock_date and int(hot_count or 0) > 0 and int(stock_count or 0) > 0)
        return {"ok": ok, "hot_date": hot_date, "hot_count": hot_count, "stock_date": stock_date, "stock_count": stock_count}
    except Exception as exc:
        return {"ok": False, "issue": f"concept_db_error:{exc}"}


def _intraday_process_count() -> int:
    result = _run(["pgrep", "-f", "python3 -m trading_core_new.longterm.cli intraday-monitor --interval 300"], timeout=5)
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _collect_trade_risk_alerts() -> Dict[str, Any]:
    """Use the consolidated price/technical risk Skill during live A-share sessions."""
    sys.path.insert(0, str(INVESTOR_DIR))
    try:
        from skill_api import _dispatch
        result = _dispatch({"action": "risk_alert"})
    except Exception as exc:
        return {"checked": False, "error": type(exc).__name__, "alerts": []}
    if not result.get("trading_session"):
        return {"checked": True, "live": False, "alerts": []}
    alerts = result.get("alerts") or []
    return {"checked": True, "live": True, "alerts": alerts, "source": result.get("source")}


def collect_health(timeout: float = 3.0) -> Dict[str, Any]:
    unit_states = [_systemctl_state(unit) for unit in WATCH_UNITS]
    qmt = _probe_qmt2http(timeout=timeout)
    concept_db = _concept_db_status()
    intraday_count = _intraday_process_count()
    trade_risk = _collect_trade_risk_alerts()
    issues: List[str] = []
    for item in unit_states:
        if item["enabled"] not in ("enabled", "static", "generated"):
            issues.append(f"unit_not_enabled:{item['unit']}={item['enabled']}")
        if item["active"] != "active":
            issues.append(f"unit_not_active:{item['unit']}={item['active']}")
    if intraday_count != 1:
        issues.append(f"intraday_process_count={intraday_count}")
    for server in qmt.get("servers", []):
        name = server.get("server", "unknown")
        for key in ("health", "positions", "qmttrader_v2_status", "qmttrader_v2_logs"):
            result = server.get(key, {})
            if not result.get("ok"):
                issues.append(f"{name}:{key}_failed(http={result.get('http_status')}, error={result.get('error')})")
                continue
            if key == "qmttrader_v2_status":
                data = (result.get("payload") or {}).get("data") or {}
                if data and data.get("running") is False:
                    issues.append(f"{name}:qmttrader_v2_not_running")
            if key == "qmttrader_v2_logs":
                data = (result.get("payload") or {}).get("data") or {}
                if isinstance(data, dict) and int(data.get("matched_count") or 0) < 1:
                    issues.append(f"{name}:qmttrader_v2_logs_empty_for_today")
    if not concept_db.get("ok"):
        issues.append(f"concept_db:{concept_db.get('issue', 'invalid_counts')}")
    for alert in trade_risk.get("alerts", []):
        qty = int(alert.get("suggested_qty") or 0)
        change_pct = float(alert.get("change_pct") or 0)
        relative_change_pct = float(alert.get("relative_change_pct") or 0)
        # A zero-sized recommendation with no observable move is not an
        # actionable risk signal.  Treating it as an incident caused the same
        # holdings to generate a fresh red card every health-check cycle.
        if qty <= 0 and abs(change_pct) < 0.01 and abs(relative_change_pct) < 0.01:
            continue
        triggers = ",".join(alert.get("triggers") or []) or "decision_reduce_priority"
        issues.append(
            f"trade_risk_reduce_priority:{alert.get('code')} change={change_pct:+.2f}% "
            f"relative={relative_change_pct:+.2f}pct suggested_qty={qty} triggers={triggers}"
        )

    incident_keys = sorted({_issue_identity(issue) for issue in issues})
    fingerprint_payload = {"incident_keys": incident_keys}
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "ok": not issues,
        "generated_at": datetime.now().strftime("%F %T %Z"),
        "issues": sorted(set(issues)),
        "incident_keys": incident_keys,
        "fingerprint": fingerprint,
        "qmt": qmt,
        "concept_db": concept_db,
        "intraday_process_count": intraday_count,
        "trade_risk": trade_risk,
    }


def _issue_identity(issue: str) -> str:
    """Return a stable incident identity while preserving detailed diagnostics.

    Network error text, HTTP status, price changes and quantities can vary on
    every poll.  Those details belong in the card body, but must not make a
    continuing incident look new and bypass the cooldown.
    """
    text = str(issue or "").strip()
    if "_failed(" in text:
        return text.split("(", 1)[0]
    if text.startswith("trade_risk_reduce_priority:"):
        head = text.split(" ", 1)[0]
        triggers = text.split(" triggers=", 1)[1] if " triggers=" in text else "decision_reduce_priority"
        return f"{head} triggers={triggers}"
    return text


def _health_transition(
    health: Dict[str, Any],
    state: Dict[str, Any],
    *,
    now: int,
    cooldown: int,
    recovery_confirmations: int,
    force: bool = False,
) -> Dict[str, Any]:
    """Apply incident hysteresis and return the next notification state."""
    incident_open = bool(state.get("incident_open", not bool(state.get("last_ok", True))))
    known_keys: Set[str] = {str(item) for item in (state.get("incident_keys") or []) if item}
    current_keys: Set[str] = {
        str(item)
        for item in (health.get("incident_keys") or [_issue_identity(issue) for issue in health.get("issues", [])])
        if item
    }
    last_sent_at = int(state.get("last_sent_at", 0) or 0)

    if health.get("ok"):
        consecutive_ok = int(state.get("consecutive_ok", 0) or 0) + 1
        recovered = bool(incident_open and consecutive_ok >= max(1, recovery_confirmations))
        return {
            "should_send": recovered,
            "recovered": recovered,
            "incident_open": False if recovered else incident_open,
            "incident_keys": [] if recovered else sorted(known_keys),
            "consecutive_ok": consecutive_ok,
            "new_incident_keys": [],
        }

    new_keys = current_keys - known_keys
    should_send = bool(
        force
        or not incident_open
        or new_keys
        or (last_sent_at and now - last_sent_at >= max(1, cooldown))
    )
    return {
        "should_send": should_send,
        "recovered": False,
        "incident_open": True,
        # Retain every identity seen during the incident.  A temporarily
        # disappearing endpoint must not become a "new" incident when it
        # flaps back on the next poll.
        "incident_keys": sorted(known_keys | current_keys),
        "consecutive_ok": 0,
        "new_incident_keys": sorted(new_keys),
    }


def _refresh_recovered_portfolio() -> Dict[str, Any]:
    sys.path.insert(0, str(INVESTOR_DIR))
    from domain.services.portfolio_refresh_service import refresh_verified_portfolio_snapshot

    return refresh_verified_portfolio_snapshot(require_complete_sources=True)


def _reconcile_recovery_transition(
    transition: Dict[str, Any],
    previous_state: Dict[str, Any],
    *,
    recovery_confirmations: int,
    dry_run: bool,
    refresh_fn=None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Do not close an incident until a fresh complete portfolio snapshot is saved."""
    if not transition.get("recovered") or dry_run:
        return transition, {"attempted": False, "verified": False, "reason": "not_required"}
    refresh = refresh_fn or _refresh_recovered_portfolio
    try:
        result = dict(refresh() or {})
    except Exception as exc:
        result = {"verified": False, "saved": False, "reason": f"refresh_failed:{type(exc).__name__}"}
    reconciliation = {"attempted": True, **result}
    if result.get("verified") and result.get("saved"):
        return transition, reconciliation

    held = dict(transition)
    held.update(
        {
            "should_send": False,
            "recovered": False,
            "incident_open": True,
            "incident_keys": list(previous_state.get("incident_keys") or []),
            "consecutive_ok": max(0, int(recovery_confirmations) - 1),
        }
    )
    return held, reconciliation


def _format_message(
    health: Dict[str, Any],
    recovered: bool = False,
    reconciliation: Dict[str, Any] | None = None,
) -> str:
    if recovered:
        verified_sources = len((reconciliation or {}).get("verified_sources") or [])
        return (
            "✅ OpenClaw 投资助手已恢复\n"
            "此前异常的服务与数据链路已通过连续健康检查。\n"
            f"双账户实时快照已重新采集并校验（来源 {verified_sources} 个）；后续报告不再使用该故障期历史回退。"
        )
    lines = [
        "🚨 OpenClaw 投资助手异常",
        f"检查时间：{health.get('generated_at', '')}",
        f"共发现 {len(health.get('issues', []))} 项异常；交易数据不可用时不会推断账户状态。",
        "",
        "**异常明细**",
    ]
    for issue in (health.get("issues", []) or [])[:12]:
        text = str(issue)
        if text.startswith("unit_not_active:"):
            text = "服务未运行｜" + text.removeprefix("unit_not_active:")
        elif text.startswith("unit_not_enabled:"):
            text = "服务未启用｜" + text.removeprefix("unit_not_enabled:")
        elif text.startswith("intraday_process_count="):
            text = "盘中监控进程数量异常｜" + text
        elif text.startswith("concept_db:"):
            text = "概念板块数据库异常｜" + text.removeprefix("concept_db:")
        elif text.startswith("trade_risk_reduce_priority:"):
            text = "持仓触发降风险条件｜" + text.removeprefix("trade_risk_reduce_priority:")
        elif ":" in text and "_failed(" in text:
            text = "QMT数据链路失败｜" + text
        lines.append(f"- {text}")
    if len(health.get("issues", []) or []) > 12:
        lines.append(f"- 另有 {len(health['issues']) - 12} 项，详见本机健康检查。")
    lines.extend([
        "",
        "**处理说明**",
        "- 本告警只做诊断，不会启动QMT客户端或自动执行交易。",
        "- 本机检查：`cd /root/.openclaw/workspace && scripts/investor_assistant_healthcheck.sh`",
    ])
    return "\n".join(lines)


def _send_feishu(message: str, target: str) -> Dict[str, Any]:
    clean = (target or os.environ.get("INVESTOR_FEISHU_TARGET") or DEFAULT_TARGET).strip()
    if not clean:
        return {"pushed": False, "reason": "missing_target"}
    if not clean.startswith(("user:", "chat:")):
        clean = f"user:{clean}"
    recovered = "已恢复" in message or "恢复正常" in message
    title = "OpenClaw 投资助理已恢复" if recovered else "OpenClaw 投资助理异常"
    card = build_diagnostic_card(title, message, recovered=recovered)
    if push_feishu_rich(message, card=card, diagnostic=True, target=clean):
        return {"pushed": True, "target": clean, "transport": "rich_card"}
    cmd = ["openclaw", "message", "send", "--channel", "feishu", "--target", clean, "-m", message]
    try:
        result = _run(cmd, timeout=30)
        record_feishu_delivery(
            text=message,
            card=card,
            diagnostic=True,
            target=clean,
            transport="health_raw_fallback",
            sent=result.returncode == 0,
        )
        return {"pushed": result.returncode == 0, "target": clean, "returncode": result.returncode, "stderr": result.stderr[-500:]}
    except Exception as exc:
        record_feishu_delivery(
            text=message,
            card=card,
            diagnostic=True,
            target=clean,
            transport="health_raw_fallback",
            sent=False,
        )
        return {"pushed": False, "target": clean, "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenClaw investment assistant health alert")
    parser.add_argument("--target", default="", help="Feishu target, user:<id> or chat:<id>")
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("INVESTOR_HEALTH_ALERT_TIMEOUT", "3")))
    parser.add_argument("--cooldown", type=int, default=int(os.environ.get("INVESTOR_HEALTH_ALERT_COOLDOWN", "10800")))
    parser.add_argument(
        "--recovery-confirmations",
        type=int,
        default=int(os.environ.get("INVESTOR_HEALTH_RECOVERY_CONFIRMATIONS", "2")),
        help="consecutive healthy checks required before sending a recovery card",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    now = int(time.time())
    health = collect_health(timeout=args.timeout)
    state = _load_state()
    transition = _health_transition(
        health,
        state,
        now=now,
        cooldown=args.cooldown,
        recovery_confirmations=args.recovery_confirmations,
        force=args.force,
    )
    transition, reconciliation = _reconcile_recovery_transition(
        transition,
        state,
        recovery_confirmations=args.recovery_confirmations,
        dry_run=args.dry_run,
    )
    recovered = bool(transition["recovered"])
    should_send = bool(transition["should_send"])

    push_result = {"pushed": False, "reason": "not_needed"}
    if should_send and not args.dry_run:
        push_result = _send_feishu(
            _format_message(health, recovered=recovered, reconciliation=reconciliation),
            args.target,
        )

    state.update(
        {
            "last_checked_at": now,
            "last_ok": bool(health["ok"]),
            "last_fingerprint": str(health.get("fingerprint") or ""),
            "last_issue_count": len(health.get("issues", [])),
            "last_push_result": push_result,
            "incident_open": transition["incident_open"],
            "incident_keys": transition["incident_keys"],
            "consecutive_ok": transition["consecutive_ok"],
        }
    )
    if should_send and not args.dry_run:
        state["last_sent_at"] = now
    # Audits and previews must be observational.  Persisting a dry-run would
    # consume a new incident or a recovery confirmation and could suppress the
    # next real Feishu notification.
    if not args.dry_run:
        _save_state(state)

    result = {
        "ok": health["ok"],
        "should_send": should_send,
        "recovered": recovered,
        "push_result": push_result,
        "recovery_reconciliation": reconciliation,
        "health": health,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if health["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

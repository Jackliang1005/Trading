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
from typing import Any, Dict, List

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
        triggers = ",".join(alert.get("triggers") or []) or "decision_reduce_priority"
        issues.append(
            f"trade_risk_reduce_priority:{alert.get('code')} change={float(alert.get('change_pct') or 0):+.2f}% "
            f"relative={float(alert.get('relative_change_pct') or 0):+.2f}pct suggested_qty={qty} triggers={triggers}"
        )

    fingerprint_payload = {"issues": sorted(set(issues))}
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return {"ok": not issues, "generated_at": datetime.now().strftime("%F %T %Z"), "issues": sorted(set(issues)), "fingerprint": fingerprint, "qmt": qmt, "concept_db": concept_db, "intraday_process_count": intraday_count, "trade_risk": trade_risk}


def _format_message(health: Dict[str, Any], recovered: bool = False) -> str:
    if recovered:
        return "✅ OpenClaw 投资助手已恢复\n此前异常的服务与数据链路已通过健康检查。"
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
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    now = int(time.time())
    health = collect_health(timeout=args.timeout)
    state = _load_state()
    previous_ok = bool(state.get("last_ok", True))
    last_fingerprint = str(state.get("last_fingerprint", ""))
    last_sent_at = int(state.get("last_sent_at", 0) or 0)

    recovered = bool(health["ok"] and not previous_ok)
    if health["ok"]:
        should_send = recovered
    else:
        should_send = args.force or health["fingerprint"] != last_fingerprint or (now - last_sent_at) >= args.cooldown

    push_result = {"pushed": False, "reason": "not_needed"}
    if should_send and not args.dry_run:
        push_result = _send_feishu(_format_message(health, recovered=recovered), args.target)

    state.update({"last_checked_at": now, "last_ok": bool(health["ok"]), "last_fingerprint": health["fingerprint"], "last_issue_count": len(health.get("issues", [])), "last_push_result": push_result})
    if should_send and not args.dry_run:
        state["last_sent_at"] = now
    _save_state(state)

    result = {"ok": health["ok"], "should_send": should_send, "recovered": recovered, "push_result": push_result, "health": health}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if health["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

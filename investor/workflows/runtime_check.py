#!/usr/bin/env python3
"""Runtime diagnostic workflow for qmt2http monitor stack."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from live_monitor.collectors.qmt_health_collector import collect_qmt_health
from live_monitor.collectors.qmt_trade_log_collector import collect_qmt_trade_logs
from live_monitor.collectors.qmt_trade_state_collector import collect_qmt_trade_state


def _endpoint_status(summary_entry: Dict) -> str:
    if bool((summary_entry or {}).get("skipped_expected")):
        return "skipped_expected"
    ok = bool((summary_entry or {}).get("ok"))
    if ok:
        return "ok"
    return "failed"


def _diagnose_server(health_entry: Dict, state_entry: Dict, log_entry: Dict) -> Dict:
    server = str(health_entry.get("server", "") or state_entry.get("server", "") or log_entry.get("server", "") or "unknown")
    health_ok = bool(health_entry.get("ok"))
    log_ok = bool(log_entry.get("ok"))
    state_summary = (state_entry.get("summary", {}) or {})

    endpoint_status = {
        name: _endpoint_status(state_summary.get(name, {}))
        for name in ("asset", "positions", "orders", "trades", "records_trades")
    }
    endpoint_ok_count = len([name for name, status in endpoint_status.items() if status == "ok"])
    endpoint_expected_total = len([name for name, status in endpoint_status.items() if status != "skipped_expected"])

    issues: List[str] = []
    actions: List[str] = []

    if not health_ok:
        http_status = health_entry.get("http_status")
        error = str(health_entry.get("error", "") or "")
        issues.append(f"health_failed(http={http_status}, error={error or 'none'})")
        if http_status in (401, 403):
            actions.append("check_qmt_token_and_auth")
        elif http_status in (500, 502, 503, 504):
            actions.append("restart_qmt2http_service")
        elif "Operation not permitted" in error:
            actions.append("check_runtime_network_policy")

    if not log_ok:
        http_status = log_entry.get("http_status")
        error = str(log_entry.get("error", "") or "")
        issues.append(f"trade_log_failed(http={http_status}, error={error or 'none'})")
        if http_status in (401, 403):
            actions.append("check_trade_log_permission")

    if endpoint_expected_total > 0 and endpoint_ok_count == 0:
        failed_http_codes = []
        for name in ("asset", "positions", "orders", "trades", "records_trades"):
            endpoint_result = (state_entry.get("endpoints", {}) or {}).get(name, {}) or {}
            if endpoint_result.get("skipped_expected"):
                continue
            if endpoint_result.get("http_status") is not None:
                failed_http_codes.append(int(endpoint_result.get("http_status")))
        issues.append("trade_state_all_failed")
        if any(code in (401, 403) for code in failed_http_codes):
            actions.append("check_trading_endpoint_auth_or_acl")
        else:
            actions.append("check_qmt_trade_backend_connection")

    if not issues:
        actions.append("no_action_needed")

    unique_actions = []
    for item in actions:
        if item not in unique_actions:
            unique_actions.append(item)

    return {
        "server": server,
        "health_ok": health_ok,
        "trade_log_ok": log_ok,
        "endpoint_ok_count": endpoint_ok_count,
        "endpoint_total": endpoint_expected_total,
        "endpoint_status": endpoint_status,
        "issues": issues,
        "actions": unique_actions,
        "mode": (state_entry.get("mode", {}) or {}),
    }


def run_runtime_check(date: Optional[str] = None) -> Dict:
    requested_date = str(date or "").strip() or datetime.now().strftime("%Y-%m-%d")
    health = collect_qmt_health()
    trade_logs = collect_qmt_trade_logs(date=requested_date)
    trade_state = collect_qmt_trade_state()

    health_map = {str(item.get("server", "")): item for item in (health.get("servers", []) or [])}
    logs_map = {str(item.get("server", "")): item for item in (trade_logs.get("servers", []) or [])}
    state_map = {str(item.get("server", "")): item for item in (trade_state.get("servers", []) or [])}

    server_names = []
    for name in list(health_map.keys()) + list(logs_map.keys()) + list(state_map.keys()):
        if name and name not in server_names:
            server_names.append(name)

    diagnostics = []
    for server in server_names:
        diagnostics.append(
            _diagnose_server(
                health_map.get(server, {}),
                state_map.get(server, {}),
                logs_map.get(server, {}),
            )
        )

    issue_servers = [item for item in diagnostics if item.get("issues")]
    return {
        "requested_date": requested_date,
        "server_count": len(diagnostics),
        "issue_server_count": len(issue_servers),
        "ok": len(issue_servers) == 0,
        "diagnostics": diagnostics,
    }

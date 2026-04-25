#!/usr/bin/env python3
from __future__ import annotations

from typing import Dict, List


ERROR_MARKERS = ("Traceback", "ERROR", "CRITICAL", "系统运行错误", "Exception")


def _extract_lines(response: Dict) -> List[str]:
    data = (response or {}).get("data") or {}
    if data.get("is_dir"):
        lines = []
        for entry in data.get("entries", []):
            lines.extend(entry.get("content", []) or [])
        return lines
    return data.get("content", []) or []


def analyze_trade_logs(trade_logs: Dict) -> List[Dict]:
    incidents = []
    for server in trade_logs.get("servers", []):
        if not server.get("ok"):
            incidents.append(
                {
                    "severity": "P1",
                    "kind": "trade_log_unavailable",
                    "signature": f"trade_log_unavailable::{server.get('server','')}::{server.get('base_url','')}",
                    "summary": f"trade log unavailable on {server.get('server')}: {server.get('error') or server.get('http_status')}",
                    "evidence": server,
                }
            )
            continue
        lines = _extract_lines((server.get("response") or {}).get("response", {}))
        matches = [line for line in lines if any(marker in line for marker in ERROR_MARKERS)]
        if matches:
            incidents.append(
                {
                    "severity": "P1",
                    "kind": "trade_log_error",
                    "signature": f"trade_log_error::{server.get('server','')}::{len(matches)}",
                    "summary": f"detected {len(matches)} error lines in qmt2http trade log on {server.get('server')}",
                    "evidence": {"server": server.get("server"), "matches": matches[-20:]},
                }
            )
    return incidents


def analyze_strategy_logs(strategy_logs: Dict) -> List[Dict]:
    incidents = []
    for entry in strategy_logs.get("entries", []):
        lines = entry.get("content", []) or []
        matches = [line for line in lines if any(marker in line for marker in ERROR_MARKERS)]
        if not matches:
            continue
        severity = "P1" if any("Traceback" in line or "CRITICAL" in line for line in matches) else "P2"
        incidents.append(
            {
                "severity": severity,
                "kind": "strategy_log_error",
                "signature": f"strategy_log_error::{entry.get('path','')}",
                "summary": f"detected {len(matches)} error lines in local strategy log {entry.get('path')}",
                "evidence": {"path": entry.get("path"), "matches": matches[-20:]},
            }
        )
    return incidents

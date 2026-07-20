#!/usr/bin/env python3
from __future__ import annotations

from typing import Dict, List


ERROR_MARKERS = ("Traceback", "ERROR", "CRITICAL", "系统运行错误", "Exception")


def _extract_lines(response: Dict) -> List[str]:
    data = (response or {}).get("data") or {}
    lines: List[str] = []
    raw_lines = data.get("lines") if isinstance(data, dict) else None
    if isinstance(raw_lines, list):
        lines.extend(str(line) for line in raw_lines if line is not None)
    entries = data.get("entries", []) if isinstance(data, dict) else []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content")
            if isinstance(content, list):
                lines.extend(str(line) for line in content if line is not None)
    content = data.get("content") if isinstance(data, dict) else None
    if isinstance(content, list):
        lines.extend(str(line) for line in content if line is not None)
    return lines


def analyze_trade_logs(trade_logs: Dict) -> List[Dict]:
    incidents = []
    source_kind = str(trade_logs.get("kind") or "qmt_trade_log")
    is_v2 = source_kind == "qmttrader_v2_logs"
    unavailable_kind = "qmttrader_v2_log_unavailable" if is_v2 else "trade_log_unavailable"
    error_kind = "qmttrader_v2_log_error" if is_v2 else "trade_log_error"
    source_label = "qmttrader_v2 log" if is_v2 else "qmt2http trade log"
    for server in trade_logs.get("servers", []):
        if not server.get("ok"):
            incidents.append(
                {
                    "severity": "P1",
                    "kind": unavailable_kind,
                    "signature": f"{unavailable_kind}::{server.get('server','')}::{server.get('base_url','')}",
                    "summary": f"{source_label} unavailable on {server.get('server')}: {server.get('error') or server.get('http_status')}",
                    "evidence": server,
                }
            )
            continue
        lines = _extract_lines(server.get("response") or {})
        matches = [line for line in lines if any(marker in line for marker in ERROR_MARKERS)]
        if matches:
            incidents.append(
                {
                    "severity": "P1",
                    "kind": error_kind,
                    "signature": f"{error_kind}::{server.get('server','')}::{len(matches)}",
                    "summary": f"detected {len(matches)} error lines in {source_label} on {server.get('server')}",
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

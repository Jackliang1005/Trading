#!/usr/bin/env python3
from __future__ import annotations

from typing import Dict, List


def analyze_qmt_trade_state(qmt_trade_state: Dict) -> List[Dict]:
    incidents: List[Dict] = []
    for server in qmt_trade_state.get("servers", []):
        server_name = server.get("server", "")
        base_url = server.get("base_url", "")
        endpoints = server.get("endpoints", {}) or {}
        failed = []
        for name, payload in endpoints.items():
            if not isinstance(payload, dict):
                continue
            if payload.get("skipped_expected"):
                continue
            if payload.get("ok"):
                continue
            failed.append(
                {
                    "name": name,
                    "url": payload.get("url", ""),
                    "error": payload.get("error", ""),
                    "http_status": payload.get("http_status"),
                }
            )
        if failed:
            incidents.append(
                {
                    "severity": "P2",
                    "kind": "qmt_trade_state_unavailable",
                    "signature": f"qmt_trade_state_unavailable::{server_name}::{base_url}",
                    "summary": f"qmt trade state unavailable on {server_name}: {len(failed)} endpoints failed",
                    "evidence": {
                        "server": server_name,
                        "base_url": base_url,
                        "failed_endpoints": failed,
                    },
                }
            )
    return incidents

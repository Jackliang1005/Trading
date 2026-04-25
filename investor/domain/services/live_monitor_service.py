#!/usr/bin/env python3
"""Live monitor orchestration service."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Sequence

import db
from domain.repository import get_live_monitor_repository
from live_monitor.analyzers.heartbeat_analyzer import analyze_heartbeat
from live_monitor.analyzers.log_error_analyzer import analyze_strategy_logs, analyze_trade_logs
from live_monitor.analyzers.qmt_trade_state_analyzer import analyze_qmt_trade_state
from live_monitor.analyzers.risk_analyzer import analyze_risk
from live_monitor.analyzers.root_cause_router import route_incidents
from live_monitor.analyzers.runtime_phase_analyzer import analyze_runtime_phase
from live_monitor.analyzers.trade_decision_analyzer import analyze_trade_decisions
from live_monitor.collectors.observability_collector import collect_observability
from live_monitor.collectors.qmt_health_collector import collect_qmt_health
from live_monitor.collectors.qmt_trade_log_collector import collect_qmt_trade_logs
from live_monitor.collectors.qmt_trade_state_collector import collect_qmt_trade_state
from live_monitor.collectors.runtime_status_collector import collect_runtime_status
from live_monitor.collectors.strategy_log_collector import collect_strategy_logs
from live_monitor.collectors.trade_decision_collector import collect_trade_decisions


def _normalize_monitor_date(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError("date must be in YYYY-MM-DD or YYYYMMDD format")


def _extract_codes_from_endpoint_result(result: Dict) -> List[str]:
    payload = ((result or {}).get("response") or {}).get("data")
    if not isinstance(payload, list):
        return []
    codes: List[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        code = item.get("stock_code") or item.get("code") or item.get("证券代码")
        if not code:
            continue
        code_text = str(code)
        if code_text not in codes:
            codes.append(code_text)
    return codes


def _dedupe_codes(items: Sequence[str]) -> List[str]:
    ordered: List[str] = []
    for raw in items or []:
        code = str(raw or "").strip()
        if not code or code in ordered:
            continue
        ordered.append(code)
    return ordered


def _build_account_candidates(
    codes: Sequence[str],
    server_matches: Sequence[Dict],
    reachable_servers: Sequence[str],
) -> List[Dict]:
    entries: List[Dict] = []
    for code in _dedupe_codes(codes):
        matched_servers = []
        matched_sources = []
        for item in server_matches or []:
            if not item.get("reachable"):
                continue
            source_hits = []
            if code in (item.get("position_codes", []) or []):
                source_hits.append("positions")
            if code in (item.get("order_codes", []) or []):
                source_hits.append("orders")
            if code in (item.get("trade_codes", []) or []):
                source_hits.append("trades")
            if code in (item.get("record_trade_codes", []) or []):
                source_hits.append("records_trades")
            if source_hits:
                matched_servers.append(str(item.get("server", "")))
                matched_sources.extend(source_hits)
        if len(matched_servers) == 1:
            attribution = "unique"
        elif len(matched_servers) > 1:
            attribution = "ambiguous"
        elif reachable_servers:
            attribution = "missing"
        else:
            attribution = "unreachable"
        entries.append(
            {
                "code": code,
                "candidate_servers": matched_servers,
                "attribution": attribution,
                "matched_sources": _dedupe_codes(matched_sources),
            }
        )
    return entries


def _summarize_skipped_reasons(
    skipped_buys: Sequence[Dict],
    skipped_account_candidates: Sequence[Dict],
) -> Dict:
    reason_counts: Dict[str, int] = {}
    reason_by_server: Dict[str, Dict[str, int]] = {}
    account_by_code = {
        str(item.get("code", "")).strip(): item
        for item in (skipped_account_candidates or [])
        if str(item.get("code", "")).strip()
    }
    for item in skipped_buys or []:
        code = str(item.get("code", "")).strip()
        reason = str(item.get("reason", "")).strip() or "unknown"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        account = account_by_code.get(code, {})
        attribution = str(account.get("attribution", "") or "")
        servers = account.get("candidate_servers", []) or []
        if attribution == "unique" and len(servers) == 1:
            server = str(servers[0] or "")
        elif attribution in {"ambiguous", "missing", "unreachable"}:
            server = attribution
        else:
            server = "unknown"
        if server not in reason_by_server:
            reason_by_server[server] = {}
        reason_by_server[server][reason] = reason_by_server[server].get(reason, 0) + 1
    sorted_reasons = sorted(
        [{"reason": key, "count": value} for key, value in reason_counts.items()],
        key=lambda row: (-int(row.get("count", 0)), str(row.get("reason", ""))),
    )
    sorted_servers = []
    for server, reason_map in reason_by_server.items():
        sorted_servers.append(
            {
                "server": server,
                "reasons": sorted(
                    [{"reason": key, "count": value} for key, value in reason_map.items()],
                    key=lambda row: (-int(row.get("count", 0)), str(row.get("reason", ""))),
                ),
            }
        )
    sorted_servers.sort(key=lambda row: str(row.get("server", "")))
    return {"overall": sorted_reasons, "by_server": sorted_servers}


def _trade_reconciliation_from_snapshot(snapshot: Dict) -> Dict:
    decision_log = (snapshot.get("trade_decisions", {}) or {}).get("system_log", {}) or {}
    qmt_servers = (snapshot.get("qmt_trade_state", {}) or {}).get("servers", []) or []

    final_candidate_codes = _dedupe_codes(decision_log.get("final_candidates", []) or [])
    submitted_codes = [
        str(item.get("code", ""))
        for item in (decision_log.get("submitted_buys", []) or [])
        if item.get("code")
    ]
    submitted_codes = _dedupe_codes(submitted_codes)
    filled_codes = _dedupe_codes([str(code) for code in (decision_log.get("filled_buys", []) or []) if code])
    skipped_buys = [
        {
            "code": str(item.get("code", "")).strip(),
            "reason": str(item.get("reason", "")).strip() or "unknown",
        }
        for item in (decision_log.get("skipped_buys", []) or [])
        if str(item.get("code", "")).strip()
    ]
    skipped_codes = _dedupe_codes([item.get("code", "") for item in skipped_buys if item.get("code")])

    reachable_servers = []
    unavailable_servers = []
    qmt_position_codes: List[str] = []
    qmt_order_codes: List[str] = []
    qmt_trade_codes: List[str] = []
    qmt_record_trade_codes: List[str] = []
    server_matches: List[Dict] = []
    for item in qmt_servers:
        server_name = str(item.get("server", ""))
        summary = item.get("summary", {}) or {}
        endpoints = item.get("endpoints", {}) or {}
        positions = summary.get("positions", {}) or {}
        orders = summary.get("orders", {}) or {}
        trades = summary.get("trades", {}) or {}
        records_trades = summary.get("records_trades", {}) or {}
        position_codes = _extract_codes_from_endpoint_result(endpoints.get("positions", {}) or {}) or [
            str(code) for code in positions.get("codes", []) or [] if code
        ]
        order_codes = _extract_codes_from_endpoint_result(endpoints.get("orders", {}) or {}) or [
            str(code) for code in orders.get("codes", []) or [] if code
        ]
        trade_codes = _extract_codes_from_endpoint_result(endpoints.get("trades", {}) or {}) or [
            str(code) for code in trades.get("codes", []) or [] if code
        ]
        record_trade_codes = _extract_codes_from_endpoint_result(endpoints.get("records_trades", {}) or {}) or [
            str(code) for code in records_trades.get("codes", []) or [] if code
        ]
        any_ok = any(bool((entry or {}).get("ok")) for entry in summary.values() if isinstance(entry, dict))
        if any_ok:
            reachable_servers.append(server_name)
            qmt_position_codes.extend(position_codes)
            qmt_order_codes.extend(order_codes)
            qmt_trade_codes.extend(trade_codes)
            qmt_record_trade_codes.extend(record_trade_codes)
        else:
            unavailable_servers.append(server_name)
        server_matches.append(
            {
                "server": server_name,
                "reachable": any_ok,
                "position_codes": position_codes,
                "order_codes": order_codes,
                "trade_codes": trade_codes,
                "record_trade_codes": record_trade_codes,
                "matched_filled_codes": [
                    code
                    for code in filled_codes
                    if code in position_codes or code in order_codes or code in trade_codes or code in record_trade_codes
                ],
            }
        )

    qmt_position_codes = _dedupe_codes(qmt_position_codes)
    qmt_order_codes = _dedupe_codes(qmt_order_codes)
    qmt_trade_codes = _dedupe_codes(qmt_trade_codes)
    qmt_record_trade_codes = _dedupe_codes(qmt_record_trade_codes)

    matched_filled_to_positions = [code for code in filled_codes if code in qmt_position_codes]
    matched_filled_to_trades = [
        code for code in filled_codes if code in qmt_order_codes or code in qmt_trade_codes or code in qmt_record_trade_codes
    ]
    missing_filled_in_qmt = [
        code
        for code in filled_codes
        if code not in qmt_position_codes and code not in qmt_order_codes and code not in qmt_trade_codes and code not in qmt_record_trade_codes
    ]

    status = "consistent"
    if not reachable_servers:
        status = "qmt_unreachable"
    elif missing_filled_in_qmt:
        status = "mismatch"

    final_account_candidates = _build_account_candidates(final_candidate_codes, server_matches, reachable_servers)
    submitted_account_candidates = _build_account_candidates(submitted_codes, server_matches, reachable_servers)
    filled_account_candidates = _build_account_candidates(filled_codes, server_matches, reachable_servers)
    skipped_account_candidates = _build_account_candidates(skipped_codes, server_matches, reachable_servers)
    skipped_reason_summary = _summarize_skipped_reasons(skipped_buys, skipped_account_candidates)

    account_trade_matrix = []
    for item in server_matches:
        server = str(item.get("server", "") or "")
        if not server:
            continue
        if item.get("reachable"):
            final_codes = [
                code
                for code in final_candidate_codes
                if code in (item.get("position_codes", []) or [])
                or code in (item.get("order_codes", []) or [])
                or code in (item.get("trade_codes", []) or [])
                or code in (item.get("record_trade_codes", []) or [])
            ]
            submitted_codes_hit = [
                code
                for code in submitted_codes
                if code in (item.get("position_codes", []) or [])
                or code in (item.get("order_codes", []) or [])
                or code in (item.get("trade_codes", []) or [])
                or code in (item.get("record_trade_codes", []) or [])
            ]
            filled_codes_hit = [
                code
                for code in filled_codes
                if code in (item.get("position_codes", []) or [])
                or code in (item.get("order_codes", []) or [])
                or code in (item.get("trade_codes", []) or [])
                or code in (item.get("record_trade_codes", []) or [])
            ]
        else:
            final_codes = []
            submitted_codes_hit = []
            filled_codes_hit = []
        account_trade_matrix.append(
            {
                "server": server,
                "reachable": bool(item.get("reachable")),
                "final_candidate_codes": final_codes,
                "submitted_codes": submitted_codes_hit,
                "filled_codes": filled_codes_hit,
                "final_candidate_count": len(final_codes),
                "submitted_count": len(submitted_codes_hit),
                "filled_count": len(filled_codes_hit),
            }
        )

    server_matches = [
        {
            **item,
            "position_count": len(item.get("position_codes", [])),
            "order_count": len(item.get("order_codes", [])),
            "trade_count": len(item.get("trade_codes", [])),
            "record_trade_count": len(item.get("record_trade_codes", [])),
        }
        for item in server_matches
    ]

    return {
        "status": status,
        "reachable_servers": reachable_servers,
        "unavailable_servers": unavailable_servers,
        "final_candidate_codes": final_candidate_codes,
        "submitted_codes": submitted_codes,
        "filled_codes": filled_codes,
        "skipped_codes": skipped_codes,
        "skipped_buys": skipped_buys,
        "qmt_position_codes": qmt_position_codes,
        "qmt_order_codes": qmt_order_codes,
        "qmt_trade_codes": qmt_trade_codes,
        "qmt_record_trade_codes": qmt_record_trade_codes,
        "matched_filled_to_positions": matched_filled_to_positions,
        "matched_filled_to_trades": matched_filled_to_trades,
        "missing_filled_in_qmt": missing_filled_in_qmt,
        "server_matches": server_matches,
        "final_account_candidates": final_account_candidates,
        "submitted_account_candidates": submitted_account_candidates,
        "filled_account_candidates": filled_account_candidates,
        "skipped_account_candidates": skipped_account_candidates,
        "skipped_reason_summary": skipped_reason_summary,
        "account_trade_matrix": account_trade_matrix,
        "coverage_summary": {
            "final_candidate_count": len(final_candidate_codes),
            "submitted_count": len(submitted_codes),
            "filled_count": len(filled_codes),
            "skipped_count": len(skipped_codes),
            "final_unique_attributed_count": len(
                [item for item in final_account_candidates if item.get("attribution") == "unique"]
            ),
            "submitted_unique_attributed_count": len(
                [item for item in submitted_account_candidates if item.get("attribution") == "unique"]
            ),
            "filled_unique_attributed_count": len(
                [item for item in filled_account_candidates if item.get("attribution") == "unique"]
            ),
            "skipped_unique_attributed_count": len(
                [item for item in skipped_account_candidates if item.get("attribution") == "unique"]
            ),
        },
    }


def run_live_monitor(date: Optional[str] = None) -> Dict:
    db.init_db()
    monitor_repo = get_live_monitor_repository()
    monitor_repo.ensure_tables()
    requested_date = _normalize_monitor_date(date)

    snapshot = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "qmt_health": collect_qmt_health(),
        "qmt_trade_log": collect_qmt_trade_logs(date=requested_date or None),
        "qmt_trade_state": collect_qmt_trade_state(),
        "runtime_status": collect_runtime_status(),
        "observability": collect_observability(),
        "strategy_logs": collect_strategy_logs(),
        "trade_decisions": collect_trade_decisions(date=requested_date or None),
    }
    snapshot_id = monitor_repo.save_snapshot(snapshot)

    incidents = []
    incidents.extend(analyze_heartbeat(snapshot["runtime_status"]))
    incidents.extend(analyze_runtime_phase(snapshot["runtime_status"]))
    incidents.extend(analyze_trade_logs(snapshot["qmt_trade_log"]))
    incidents.extend(analyze_qmt_trade_state(snapshot["qmt_trade_state"]))
    incidents.extend(analyze_strategy_logs(snapshot["strategy_logs"]))
    incidents.extend(analyze_trade_decisions(snapshot["trade_decisions"]))
    incidents.extend(
        analyze_risk(
            qmt_trade_state=snapshot.get("qmt_trade_state", {}),
            trade_decisions=snapshot.get("trade_decisions", {}),
        )
    )
    trade_reconciliation = _trade_reconciliation_from_snapshot(snapshot)
    if trade_reconciliation.get("status") == "mismatch":
        incidents.append(
            {
                "severity": "P1",
                "kind": "trade_reconciliation_mismatch",
                "signature": "trade_reconciliation_mismatch::"
                + ",".join(trade_reconciliation.get("missing_filled_in_qmt", []) or ["none"]),
                "summary": "filled buys from local strategy logs are missing in reachable qmt accounts",
                "evidence": trade_reconciliation,
            }
        )
    incidents = route_incidents(incidents)
    incident_ids = monitor_repo.save_incidents(incidents) if incidents else []
    codex_fix_ids = monitor_repo.save_codex_fix_tasks(incidents) if incidents else []

    return {
        "snapshot_id": snapshot_id,
        "incident_count": len(incidents),
        "incident_ids": incident_ids,
        "codex_fix_task_count": len(codex_fix_ids),
        "codex_fix_task_ids": codex_fix_ids,
        "incidents": incidents,
        "summary": {
            "requested_date": requested_date,
            "qmt_servers": len(snapshot["qmt_health"].get("servers", [])),
            "qmt_trade_state_servers": len(snapshot["qmt_trade_state"].get("servers", [])),
            "runtime_status_files": len(snapshot["runtime_status"].get("statuses", [])),
            "observability_files": len(snapshot["observability"].get("entries", [])),
            "strategy_log_files": len(snapshot["strategy_logs"].get("entries", [])),
            "decision_log_date": snapshot["trade_decisions"].get("summary", {}).get("latest_log_date", ""),
            "final_candidate_count": snapshot["trade_decisions"].get("summary", {}).get("final_candidate_count", 0),
            "submitted_buy_count": snapshot["trade_decisions"].get("summary", {}).get("submitted_buy_count", 0),
            "filled_buy_count": snapshot["trade_decisions"].get("summary", {}).get("filled_buy_count", 0),
            "risk_incident_count": sum(
                1 for inc in incidents
                if str(inc.get("kind", "")).startswith(
                    ("position_concentration", "sector_concentration", "intraday_drawdown",
                     "stock_adverse_move", "low_fill_rate", "dominant_skip_reason")
                )
            ),
            "risk_incidents": [
                {"kind": inc["kind"], "severity": inc["severity"], "summary": inc["summary"]}
                for inc in incidents
                if str(inc.get("kind", "")).startswith(
                    ("position_concentration", "sector_concentration", "intraday_drawdown",
                     "stock_adverse_move", "low_fill_rate", "dominant_skip_reason")
                )
            ],
        },
        "trade_decision_summary": snapshot["trade_decisions"].get("summary", {}),
        "trade_decision_focus": {
            "strategy": snapshot["trade_decisions"].get("system_log", {}).get("strategy", ""),
            "log_date": snapshot["trade_decisions"].get("system_log", {}).get("log_date", ""),
            "final_candidates": snapshot["trade_decisions"].get("system_log", {}).get("final_candidates", [])[:10],
            "priority_buy_candidates": snapshot["trade_decisions"].get("system_log", {}).get("priority_buy_candidates", [])[:10],
            "submitted_buys": snapshot["trade_decisions"].get("system_log", {}).get("submitted_buys", [])[:10],
            "filled_buys": snapshot["trade_decisions"].get("system_log", {}).get("filled_buys", [])[:10],
            "skipped_buys": snapshot["trade_decisions"].get("system_log", {}).get("skipped_buys", [])[:50],
            "timeline_events": snapshot["trade_decisions"].get("system_log", {}).get("timeline_events", [])[:120],
            "watchlists": (snapshot["trade_decisions"].get("watchlists", {}) or {}).get("entries", [])[:20],
            "watchlist_dir_warnings": (snapshot["trade_decisions"].get("watchlists", {}) or {}).get("dir_warnings", [])[:10],
        },
        "trade_reconciliation": trade_reconciliation,
        "qmt_trade_state_focus": [
            {
                "server": item.get("server", ""),
                "asset": item.get("summary", {}).get("asset", {}),
                "positions": item.get("summary", {}).get("positions", {}),
                "orders": item.get("summary", {}).get("orders", {}),
                "trades": item.get("summary", {}).get("trades", {}),
                "records_trades": item.get("summary", {}).get("records_trades", {}),
            }
            for item in snapshot["qmt_trade_state"].get("servers", [])
        ],
    }

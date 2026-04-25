#!/usr/bin/env python3
"""Live monitor view service."""

from __future__ import annotations

from typing import Dict, Optional

from domain.repository import get_analysis_context_repository
from domain.services.analysis_context_service import normalize_analysis_context_summary
from domain.services.live_monitor_service import run_live_monitor

def _analysis_context_summary(requested_date: str = "") -> Dict:
    repo = get_analysis_context_repository()
    summary = repo.summarize_bundle(as_of_date=requested_date or None)
    return normalize_analysis_context_summary(summary, as_of_date=requested_date)


def _load_live_view(date: Optional[str] = None) -> Dict:
    result = run_live_monitor(date=date)
    return {
        "result": result,
        "requested_date": result.get("summary", {}).get("requested_date", ""),
        "focus": result.get("trade_decision_focus", {}) or {},
    }


def _build_accounts_overview(
    accounts: list,
    reconciliation: Dict,
    final_candidates: list,
    submitted_buys: list,
    filled_buys: list,
) -> Dict:
    server_matches = (reconciliation.get("server_matches", []) or [])
    reachable_servers = [str(item.get("server", "")) for item in server_matches if item.get("reachable")]
    unavailable_servers = [str(item.get("server", "")) for item in server_matches if not item.get("reachable")]

    server_stats = []
    total_positions = 0
    total_orders = 0
    total_trades = 0
    for item in accounts or []:
        server = str(item.get("server", "") or "")
        positions = int((item.get("positions", {}) or {}).get("count", 0) or 0)
        orders = int((item.get("orders", {}) or {}).get("count", 0) or 0)
        trades = int((item.get("trades", {}) or {}).get("count", 0) or 0)
        total_positions += positions
        total_orders += orders
        total_trades += trades
        ok = any(bool((item.get(name, {}) or {}).get("ok")) for name in ("asset", "positions", "orders", "trades", "records_trades"))
        server_stats.append(
            {
                "server": server,
                "reachable": server in reachable_servers,
                "ok": ok,
                "positions_count": positions,
                "orders_count": orders,
                "trades_count": trades,
            }
        )

    submitted_codes = [str(item.get("code", "")) for item in (submitted_buys or []) if item.get("code")]
    filled_codes = [str(code) for code in (filled_buys or []) if code]
    matched_filled_codes = set()
    for server_match in server_matches:
        for code in server_match.get("matched_filled_codes", []) or []:
            matched_filled_codes.add(str(code))
    account_trade_matrix = (reconciliation.get("account_trade_matrix", []) or [])
    coverage = (reconciliation.get("coverage_summary", {}) or {})

    return {
        "server_count": len(accounts or []),
        "reachable_server_count": len(reachable_servers),
        "reachable_servers": reachable_servers,
        "unavailable_servers": unavailable_servers,
        "server_stats": server_stats,
        "total_positions_count": total_positions,
        "total_orders_count": total_orders,
        "total_trades_count": total_trades,
        "final_candidate_count": len(final_candidates or []),
        "submitted_buy_count": len(submitted_codes),
        "filled_buy_count": len(filled_codes),
        "matched_filled_count": len(matched_filled_codes),
        "unmatched_filled_count": max(0, len(filled_codes) - len(matched_filled_codes)),
        "account_trade_matrix": account_trade_matrix,
        "final_unique_attributed_count": int(coverage.get("final_unique_attributed_count", 0) or 0),
        "submitted_unique_attributed_count": int(coverage.get("submitted_unique_attributed_count", 0) or 0),
        "filled_unique_attributed_count": int(coverage.get("filled_unique_attributed_count", 0) or 0),
        "skipped_unique_attributed_count": int(coverage.get("skipped_unique_attributed_count", 0) or 0),
    }


def run_trading_monitor(date: Optional[str] = None) -> Dict:
    result = run_live_monitor(date=date)
    requested_date = result.get("summary", {}).get("requested_date", "")
    reconciliation = result.get("trade_reconciliation", {}) or {}
    focus = result.get("trade_decision_focus", {}) or {}
    accounts = result.get("qmt_trade_state_focus", []) or []
    return {
        "snapshot_id": result.get("snapshot_id"),
        "requested_date": requested_date,
        "incident_count": result.get("incident_count", 0),
        "trade_incidents": [
            item
            for item in result.get("incidents", [])
            if str(item.get("kind", "")).startswith(("trade_", "qmt_trade_"))
        ],
        "trade_decision_summary": result.get("trade_decision_summary", {}),
        "trade_decision_focus": focus,
        "qmt_trade_state_focus": accounts,
        "trade_reconciliation": reconciliation,
        "accounts_overview": _build_accounts_overview(
            accounts=accounts,
            reconciliation=reconciliation,
            final_candidates=focus.get("final_candidates", []),
            submitted_buys=focus.get("submitted_buys", []),
            filled_buys=focus.get("filled_buys", []),
        ),
        "analysis_context_summary": _analysis_context_summary(requested_date),
    }


def get_today_candidates(date: Optional[str] = None) -> Dict:
    view = _load_live_view(date=date)
    result = view["result"]
    requested_date = view["requested_date"]
    focus = view["focus"]
    reconciliation = result.get("trade_reconciliation", {}) or {}
    return {
        "snapshot_id": result.get("snapshot_id"),
        "requested_date": requested_date,
        "strategy": focus.get("strategy", ""),
        "log_date": focus.get("log_date", ""),
        "final_candidates": focus.get("final_candidates", []),
        "priority_buy_candidates": focus.get("priority_buy_candidates", []),
        "trade_reconciliation": reconciliation,
        "final_account_candidates": reconciliation.get("final_account_candidates", []),
        "submitted_account_candidates": reconciliation.get("submitted_account_candidates", []),
        "filled_account_candidates": reconciliation.get("filled_account_candidates", []),
        "watchlists": focus.get("watchlists", []),
        "watchlist_dir_warnings": focus.get("watchlist_dir_warnings", []),
        "analysis_context_summary": _analysis_context_summary(requested_date),
        "summary": result.get("trade_decision_summary", {}),
    }


def get_today_buys(date: Optional[str] = None) -> Dict:
    view = _load_live_view(date=date)
    result = view["result"]
    focus = view["focus"]
    return {
        "snapshot_id": result.get("snapshot_id"),
        "requested_date": result.get("summary", {}).get("requested_date", ""),
        "strategy": focus.get("strategy", ""),
        "log_date": focus.get("log_date", ""),
        "submitted_buys": focus.get("submitted_buys", []),
        "filled_buys": focus.get("filled_buys", []),
        "skipped_buys": focus.get("skipped_buys", []),
        "timeline_events": focus.get("timeline_events", []),
        "qmt_trade_state_focus": result.get("qmt_trade_state_focus", []),
        "trade_reconciliation": result.get("trade_reconciliation", {}),
        "final_account_candidates": (result.get("trade_reconciliation", {}) or {}).get("final_account_candidates", []),
        "submitted_account_candidates": (result.get("trade_reconciliation", {}) or {}).get("submitted_account_candidates", []),
        "filled_account_candidates": (result.get("trade_reconciliation", {}) or {}).get("filled_account_candidates", []),
        "skipped_account_candidates": (result.get("trade_reconciliation", {}) or {}).get("skipped_account_candidates", []),
        "skipped_reason_summary": (result.get("trade_reconciliation", {}) or {}).get("skipped_reason_summary", {}),
        "summary": result.get("trade_decision_summary", {}),
    }


def get_today_account(date: Optional[str] = None) -> Dict:
    view = _load_live_view(date=date)
    result = view["result"]
    requested_date = view["requested_date"]
    accounts = result.get("qmt_trade_state_focus", [])
    reconciliation = result.get("trade_reconciliation", {})
    focus = result.get("trade_decision_focus", {}) or {}
    return {
        "snapshot_id": result.get("snapshot_id"),
        "requested_date": requested_date,
        "log_date": focus.get("log_date", ""),
        "accounts": accounts,
        "trade_reconciliation": reconciliation,
        "server_matches": (reconciliation or {}).get("server_matches", []),
        "final_account_candidates": (reconciliation or {}).get("final_account_candidates", []),
        "submitted_account_candidates": (reconciliation or {}).get("submitted_account_candidates", []),
        "filled_account_candidates": (reconciliation or {}).get("filled_account_candidates", []),
        "skipped_account_candidates": (reconciliation or {}).get("skipped_account_candidates", []),
        "skipped_reason_summary": (reconciliation or {}).get("skipped_reason_summary", {}),
        "accounts_overview": _build_accounts_overview(
            accounts=accounts,
            reconciliation=reconciliation or {},
            final_candidates=focus.get("final_candidates", []),
            submitted_buys=focus.get("submitted_buys", []),
            filled_buys=focus.get("filled_buys", []),
        ),
        "analysis_context_summary": _analysis_context_summary(requested_date),
        "trade_incidents": [
            item
            for item in result.get("incidents", [])
            if str(item.get("kind", "")).startswith(("trade_", "qmt_trade_"))
        ],
    }


def get_today_summary(date: Optional[str] = None) -> Dict:
    view = _load_live_view(date=date)
    result = view["result"]
    focus = view["focus"]
    requested_date = view["requested_date"]
    accounts = result.get("qmt_trade_state_focus", [])
    reconciliation = result.get("trade_reconciliation", {})
    return {
        "snapshot_id": result.get("snapshot_id"),
        "requested_date": requested_date,
        "strategy": focus.get("strategy", ""),
        "log_date": focus.get("log_date", ""),
        "final_candidates": focus.get("final_candidates", []),
        "submitted_buys": focus.get("submitted_buys", []),
        "filled_buys": focus.get("filled_buys", []),
        "skipped_buys": focus.get("skipped_buys", []),
        "timeline_events": focus.get("timeline_events", []),
        "watchlists": focus.get("watchlists", []),
        "watchlist_dir_warnings": focus.get("watchlist_dir_warnings", []),
        "accounts": accounts,
        "trade_reconciliation": reconciliation,
        "final_account_candidates": (reconciliation or {}).get("final_account_candidates", []),
        "submitted_account_candidates": (reconciliation or {}).get("submitted_account_candidates", []),
        "filled_account_candidates": (reconciliation or {}).get("filled_account_candidates", []),
        "skipped_account_candidates": (reconciliation or {}).get("skipped_account_candidates", []),
        "skipped_reason_summary": (reconciliation or {}).get("skipped_reason_summary", {}),
        "accounts_overview": _build_accounts_overview(
            accounts=accounts,
            reconciliation=reconciliation or {},
            final_candidates=focus.get("final_candidates", []),
            submitted_buys=focus.get("submitted_buys", []),
            filled_buys=focus.get("filled_buys", []),
        ),
        "analysis_context_summary": _analysis_context_summary(requested_date),
        "trade_incidents": [
            {
                "kind": item.get("kind", ""),
                "severity": item.get("severity", ""),
                "summary": item.get("summary", ""),
            }
            for item in result.get("incidents", [])
            if str(item.get("kind", "")).startswith(("trade_", "qmt_trade_"))
        ],
    }


def format_today_summary_text(date: Optional[str] = None) -> str:
    payload = get_today_summary(date=date)
    requested_date = payload.get("requested_date", "") or "latest"
    log_date = payload.get("log_date", "") or "N/A"
    strategy = payload.get("strategy", "") or "N/A"
    final_candidates = payload.get("final_candidates", []) or []
    submitted_buys = payload.get("submitted_buys", []) or []
    filled_buys = payload.get("filled_buys", []) or []
    skipped_buys = payload.get("skipped_buys", []) or []
    accounts = payload.get("accounts", []) or []
    reconciliation = payload.get("trade_reconciliation", {}) or {}
    filled_account_candidates = payload.get("filled_account_candidates", []) or []
    final_account_candidates = payload.get("final_account_candidates", []) or []
    submitted_account_candidates = payload.get("submitted_account_candidates", []) or []
    skipped_account_candidates = payload.get("skipped_account_candidates", []) or []
    skipped_reason_summary = payload.get("skipped_reason_summary", {}) or {}
    timeline_events = payload.get("timeline_events", []) or []
    watchlists = payload.get("watchlists", []) or []
    watchlist_dir_warnings = payload.get("watchlist_dir_warnings", []) or []
    analysis_context_summary = payload.get("analysis_context_summary", {}) or {}
    incidents = payload.get("trade_incidents", []) or []
    accounts_overview = payload.get("accounts_overview", {}) or {}
    account_parts = []
    for item in accounts:
        server = str(item.get("server", "") or "?")
        positions = (item.get("positions", {}) or {}).get("count", 0)
        orders = (item.get("orders", {}) or {}).get("count", 0)
        trades = (item.get("trades", {}) or {}).get("count", 0)
        ok = any(bool((item.get(name, {}) or {}).get("ok")) for name in ("asset", "positions", "orders", "trades", "records_trades"))
        account_parts.append(
            f"{server}={'ok' if ok else 'unreachable'} pos={positions} ord={orders} trd={trades}"
        )

    submitted_codes = [str(item.get("code", "")) for item in submitted_buys if item.get("code")]
    incident_lines = [f"{item.get('severity', '')}/{item.get('kind', '')}: {item.get('summary', '')}" for item in incidents[:4]]
    attribution_parts = []
    for item in filled_account_candidates:
        code = str(item.get("code", "") or "")
        servers = item.get("candidate_servers", []) or []
        attribution = str(item.get("attribution", "") or "")
        if servers:
            attribution_parts.append(f"{code}->{','.join(servers)} ({attribution})")
        elif code:
            attribution_parts.append(f"{code}->{attribution}")

    attribution_summary = (
        f"final={len([x for x in final_account_candidates if x.get('attribution') == 'unique'])}/{len(final_account_candidates)} "
        f"submitted={len([x for x in submitted_account_candidates if x.get('attribution') == 'unique'])}/{len(submitted_account_candidates)} "
        f"filled={len([x for x in filled_account_candidates if x.get('attribution') == 'unique'])}/{len(filled_account_candidates)} "
        f"skipped={len([x for x in skipped_account_candidates if x.get('attribution') == 'unique'])}/{len(skipped_account_candidates)}"
    )

    watchlist_parts = []
    for item in watchlists[:5]:
        file_name = str(item.get("file", "") or "")
        status = str(item.get("status", "") or "")
        watchlist_parts.append(f"{file_name}({status})" if file_name else status)

    lines = [
        f"交易简报 {requested_date} | log={log_date} | strategy={strategy}",
        f"候选 {len(final_candidates)}: {', '.join(final_candidates) if final_candidates else '无'}",
        f"提交 {len(submitted_codes)}: {', '.join(submitted_codes) if submitted_codes else '无'}",
        f"成交 {len(filled_buys)}: {', '.join(str(code) for code in filled_buys) if filled_buys else '无'}",
        (
            "过滤 "
            f"{len(skipped_buys)}: "
            + (
                ", ".join(
                    f"{str(item.get('code', ''))}:{str(item.get('reason', 'unknown'))}"
                    for item in skipped_buys[:6]
                    if item.get("code")
                )
                if skipped_buys
                else "无"
            )
        ),
        f"归属: {' | '.join(attribution_parts) if attribution_parts else '无'}",
        f"归属覆盖: {attribution_summary}",
        f"watchlists {len(watchlists)}: {' | '.join(watchlist_parts) if watchlist_parts else '无'}",
        f"对账: {reconciliation.get('status', '') or 'unknown'}",
        f"账户: {' | '.join(account_parts) if account_parts else '无'}",
        (
            "账户汇总: "
            f"reachable={accounts_overview.get('reachable_server_count', 0)}/{accounts_overview.get('server_count', 0)} "
            f"pos={accounts_overview.get('total_positions_count', 0)} "
            f"ord={accounts_overview.get('total_orders_count', 0)} "
            f"trd={accounts_overview.get('total_trades_count', 0)} "
            f"filled_match={accounts_overview.get('matched_filled_count', 0)}/{accounts_overview.get('filled_buy_count', 0)}"
        ),
        (
            "分析上下文: "
            f"packets={analysis_context_summary.get('packet_hits', 0)} "
            f"quotes={analysis_context_summary.get('quote_count', 0)} "
            f"positions={analysis_context_summary.get('positions_count', 0)} "
            f"trades={analysis_context_summary.get('today_trade_count', 0)}"
        ),
    ]
    skipped_overall = (skipped_reason_summary.get("overall", []) if isinstance(skipped_reason_summary, dict) else []) or []
    if skipped_overall:
        lines.append(
            "过滤原因汇总: "
            + " | ".join(
                f"{str(item.get('reason', 'unknown'))}:{int(item.get('count', 0) or 0)}"
                for item in skipped_overall[:6]
            )
        )
    skipped_by_server = (skipped_reason_summary.get("by_server", []) if isinstance(skipped_reason_summary, dict) else []) or []
    if skipped_by_server:
        lines.append(
            "过滤原因分账户: "
            + " | ".join(
                f"{str(item.get('server', '?'))}="
                + ",".join(
                    f"{str(reason_row.get('reason', 'unknown'))}:{int(reason_row.get('count', 0) or 0)}"
                    for reason_row in (item.get("reasons", []) or [])[:4]
                )
                for item in skipped_by_server[:4]
            )
        )
    account_matrix = accounts_overview.get("account_trade_matrix", []) or []
    if account_matrix:
        lines.append(
            "账户分摊: "
            + " | ".join(
                (
                    f"{str(item.get('server', '?'))}"
                    f"[cand={int(item.get('final_candidate_count', 0) or 0)},"
                    f"sub={int(item.get('submitted_count', 0) or 0)},"
                    f"fill={int(item.get('filled_count', 0) or 0)}]"
                )
                for item in account_matrix
            )
        )
    if timeline_events:
        lines.append(
            "盘中时序: "
            + " | ".join(
                (
                    f"{str(event.get('ts', '') or '--')}:{str(event.get('event', 'event'))}"
                    + (f"({event.get('code')})" if event.get("code") else "")
                )
                for event in timeline_events[-6:]
            )
        )
    if incident_lines:
        lines.append("告警:")
        lines.extend(incident_lines)
    if watchlist_dir_warnings:
        lines.append(f"watchlists目录告警: {' | '.join(watchlist_dir_warnings[:3])}")
    return "\n".join(lines)

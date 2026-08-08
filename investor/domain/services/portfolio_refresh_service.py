#!/usr/bin/env python3
"""Verified dual-account portfolio refresh used when a QMT incident recovers."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

import db
from data_collector import fetch_qmt_trading_summary


def refresh_verified_portfolio_snapshot(
    *,
    as_of: datetime | None = None,
    require_complete_sources: bool = True,
) -> Dict[str, Any]:
    """Refresh the combined snapshot only after every configured source is verified."""
    current = as_of or datetime.now()
    summary = fetch_qmt_trading_summary() or {}
    expected_sources = [str(item) for item in summary.get("expected_sources", []) or [] if item]
    source_errors = dict(summary.get("source_errors", {}) or {})
    accounts = dict(summary.get("accounts", {}) or {})
    missing_accounts = [
        source
        for source in expected_sources
        if not isinstance(accounts.get(source), dict) or not accounts.get(source)
    ]
    complete = bool(expected_sources) and not source_errors and not missing_accounts
    if require_complete_sources and not complete:
        return {
            "verified": False,
            "saved": False,
            "reason": "account_sources_incomplete",
            "expected_sources": expected_sources,
            "missing_accounts": missing_accounts,
            "source_error_keys": sorted(source_errors),
        }

    positions = list(summary.get("positions", []) or [])
    orders = list(summary.get("today_orders", []) or [])
    trades = list(summary.get("today_trades", []) or [])
    payload = {
        "timestamp": current.isoformat(),
        "data_status": "live_recovery_verified" if complete else "partial_refresh",
        "qmt_account": accounts.get("main", {}),
        "qmt_positions": positions,
        "qmt_orders": orders,
        "qmt_trades": trades,
        "qmt_trading_summary": summary,
    }
    snapshot_id = db.save_portfolio_snapshot(
        "combined",
        payload,
        as_of_date=current.date().isoformat(),
        source_snapshot_type="portfolio_recovery",
        metadata={
            "reason": "qmt_health_recovered",
            "expected_sources": expected_sources,
            "verified_sources": sorted(accounts),
            "complete": complete,
        },
    )
    return {
        "verified": complete,
        "saved": True,
        "snapshot_id": snapshot_id,
        "as_of": current.date().isoformat(),
        "expected_sources": expected_sources,
        "verified_sources": sorted(accounts),
        "positions_count": len(positions),
    }

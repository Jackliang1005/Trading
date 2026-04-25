#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional


def _parse_date_token(value: str) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def analyze_trade_decisions(trade_decisions: Dict, freshness_days: int = 2) -> List[Dict]:
    incidents: List[Dict] = []
    summary = trade_decisions.get("summary", {}) or {}
    system_log = trade_decisions.get("system_log", {}) or {}
    sources = (trade_decisions.get("sources", {}) or {}).get("entries", []) or []

    latest_log_date = str(summary.get("latest_log_date", "") or "")
    latest_dt = _parse_date_token(latest_log_date)
    now = datetime.now()
    is_fresh = False
    age_days = None
    if latest_dt is not None:
        age_days = (now.date() - latest_dt.date()).days
        is_fresh = age_days <= freshness_days

    if not system_log and not sources:
        incidents.append(
            {
                "severity": "P2",
                "kind": "trade_decision_source_missing",
                "signature": "trade_decision_source_missing",
                "summary": "no trade decision sources found in live system logs",
                "evidence": {"summary": summary},
            }
        )
        return incidents

    if latest_dt is None:
        incidents.append(
            {
                "severity": "P2",
                "kind": "trade_decision_date_missing",
                "signature": "trade_decision_date_missing",
                "summary": "trade decision data exists but latest log date is missing or invalid",
                "evidence": {"summary": summary, "system_log": system_log},
            }
        )
        return incidents

    if not is_fresh:
        incidents.append(
            {
                "severity": "P2",
                "kind": "trade_decision_stale",
                "signature": f"trade_decision_stale::{latest_log_date}",
                "summary": f"latest trade decision log is stale: {latest_log_date} ({age_days} days old)",
                "evidence": {"summary": summary, "system_log": system_log},
            }
        )
        return incidents

    final_candidate_count = int(summary.get("final_candidate_count", 0) or 0)
    submitted_buy_count = int(summary.get("submitted_buy_count", 0) or 0)
    filled_buy_count = int(summary.get("filled_buy_count", 0) or 0)
    signal_count = int(summary.get("signal_count", 0) or 0)
    skipped_buys = system_log.get("skipped_buys", []) or []

    if signal_count > 0 and final_candidate_count == 0:
        incidents.append(
            {
                "severity": "P2",
                "kind": "trade_final_candidates_empty",
                "signature": f"trade_final_candidates_empty::{latest_log_date}",
                "summary": f"trade decision generated {signal_count} signals but final candidates are empty",
                "evidence": {"summary": summary, "system_log": system_log},
            }
        )

    if submitted_buy_count > 0 and filled_buy_count == 0:
        incidents.append(
            {
                "severity": "P2",
                "kind": "trade_buy_submitted_unfilled",
                "signature": f"trade_buy_submitted_unfilled::{latest_log_date}",
                "summary": f"{submitted_buy_count} buy submissions detected but no filled buys were recorded",
                "evidence": {"summary": summary, "system_log": system_log},
            }
        )

    if signal_count > 0 and submitted_buy_count == 0 and skipped_buys:
        incidents.append(
            {
                "severity": "P2",
                "kind": "trade_all_buys_skipped",
                "signature": f"trade_all_buys_skipped::{latest_log_date}",
                "summary": f"trade decision produced {signal_count} signals but all buy attempts were skipped",
                "evidence": {"summary": summary, "system_log": system_log},
            }
        )

    return incidents

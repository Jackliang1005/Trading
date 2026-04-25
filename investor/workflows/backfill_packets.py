#!/usr/bin/env python3
"""Backfill packetized analysis context from legacy market snapshots."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import db


SUPPORTED_SNAPSHOT_TYPES = {"daily_close", "intraday"}


def _normalize_as_of_date(raw) -> str:
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    value = str(raw or "").strip()
    if not value:
        return datetime.now().date().isoformat()
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return value[:10]


def _packet_exists(conn, as_of_date: str, source_snapshot_type: str) -> bool:
    row = conn.execute(
        """SELECT COUNT(*) AS cnt
           FROM research_packets
           WHERE as_of_date=? AND source_snapshot_type=? AND packet_type='market'""",
        (as_of_date, source_snapshot_type),
    ).fetchone()
    return int(row["cnt"] or 0) > 0


def _portfolio_exists(conn, as_of_date: str, source_snapshot_type: str) -> bool:
    row = conn.execute(
        """SELECT COUNT(*) AS cnt
           FROM portfolio_snapshots
           WHERE as_of_date=? AND source_snapshot_type=? AND account_scope='combined'""",
        (as_of_date, source_snapshot_type),
    ).fetchone()
    return int(row["cnt"] or 0) > 0


def _load_candidate_snapshots(limit: int = 0, snapshot_type: str = "") -> List[Dict]:
    conn = db.get_conn()
    sql = "SELECT id, captured_at, snapshot_type, data FROM market_snapshots"
    params: List = []
    if snapshot_type:
        sql += " WHERE snapshot_type=?"
        params.append(snapshot_type)
    sql += " ORDER BY id ASC"
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _collect_coverage_stats() -> Dict:
    conn = db.get_conn()
    try:
        packet_total = conn.execute("SELECT COUNT(*) AS cnt FROM research_packets").fetchone()["cnt"]
        portfolio_total = conn.execute("SELECT COUNT(*) AS cnt FROM portfolio_snapshots").fetchone()["cnt"]
        packet_dates = conn.execute("SELECT COUNT(DISTINCT as_of_date) AS cnt FROM research_packets").fetchone()["cnt"]
        portfolio_dates = conn.execute("SELECT COUNT(DISTINCT as_of_date) AS cnt FROM portfolio_snapshots").fetchone()["cnt"]
        return {
            "research_packets_total": int(packet_total or 0),
            "portfolio_snapshots_total": int(portfolio_total or 0),
            "research_packet_dates": int(packet_dates or 0),
            "portfolio_snapshot_dates": int(portfolio_dates or 0),
        }
    finally:
        conn.close()


def backfill_packets(limit: int = 0, snapshot_type: str = "", dry_run: bool = True, force: bool = False) -> Dict:
    db.init_db()
    candidates = _load_candidate_snapshots(limit=limit, snapshot_type=snapshot_type)
    before_stats = _collect_coverage_stats()

    summary = {
        "total_candidates": len(candidates),
        "processed": 0,
        "skipped_unsupported_type": 0,
        "skipped_already_backfilled": 0,
        "success": 0,
        "failed": 0,
        "dry_run": dry_run,
        "coverage_before": before_stats,
    }

    conn = db.get_conn()
    try:
        for row in candidates:
            summary["processed"] += 1
            sid = int(row["id"])
            stype = str(row.get("snapshot_type", "") or "")
            if stype not in SUPPORTED_SNAPSHOT_TYPES:
                summary["skipped_unsupported_type"] += 1
                print(f"[skip] snapshot#{sid} type={stype} unsupported")
                continue

            try:
                payload = json.loads(row.get("data", "{}") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("snapshot data is not a JSON object")
            except Exception as exc:
                summary["failed"] += 1
                print(f"[fail] snapshot#{sid} parse error: {exc}")
                continue

            as_of_date = _normalize_as_of_date(
                payload.get("date")
                or payload.get("trade_date")
                or payload.get("timestamp")
                or row.get("captured_at")
            )

            already_backfilled = _packet_exists(conn, as_of_date, stype) and _portfolio_exists(conn, as_of_date, stype)
            if already_backfilled and not force:
                summary["skipped_already_backfilled"] += 1
                print(f"[skip] snapshot#{sid} as_of={as_of_date} type={stype} already backfilled")
                continue

            if dry_run:
                print(f"[dry-run] snapshot#{sid} as_of={as_of_date} type={stype} would backfill packets")
                summary["success"] += 1
                continue

            ids = db.save_daily_close_packets(payload, snapshot_type=stype)
            summary["success"] += 1
            print(
                f"[ok] snapshot#{sid} as_of={as_of_date} type={stype} "
                f"market={ids.get('market')} macro={ids.get('macro')} "
                f"sector={ids.get('sector_rotation')} pred_ctx={ids.get('prediction_context')} "
                f"portfolio={ids.get('portfolio')}"
            )
    finally:
        conn.close()

    summary["coverage_after"] = _collect_coverage_stats()
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill research/portfolio packets from legacy market snapshots.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N snapshots (0 means all).")
    parser.add_argument(
        "--type",
        dest="snapshot_type",
        default="",
        help="Only process one snapshot_type (e.g. daily_close/intraday).",
    )
    parser.add_argument("--apply", action="store_true", help="Execute backfill writes. Default is dry-run.")
    parser.add_argument("--force", action="store_true", help="Backfill even if packets already exist.")
    return parser.parse_args()


def main():
    args = parse_args()
    result = backfill_packets(
        limit=max(0, int(args.limit or 0)),
        snapshot_type=str(args.snapshot_type or ""),
        dry_run=not bool(args.apply),
        force=bool(args.force),
    )
    print("\nsummary:")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

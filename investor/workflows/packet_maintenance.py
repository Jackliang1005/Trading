#!/usr/bin/env python3
"""Daily packet maintenance workflow."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict

from workflows.backfill_packets import backfill_packets


DEFAULT_SNAPSHOT_PATH = "/root/.openclaw/workspace/investor/docs/packet_maintenance_latest.json"


def _merge_metrics(daily_result: Dict, intraday_result: Dict) -> Dict:
    keys = [
        "total_candidates",
        "processed",
        "skipped_unsupported_type",
        "skipped_already_backfilled",
        "success",
        "failed",
    ]
    merged: Dict = {}
    for key in keys:
        merged[key] = int(daily_result.get(key, 0) or 0) + int(intraday_result.get(key, 0) or 0)
    return merged


def _write_snapshot(payload: Dict, output_path: str) -> Dict:
    path = Path(str(output_path or "").strip() or DEFAULT_SNAPSHOT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(path), "bytes": int(path.stat().st_size)}


def run_packet_maintenance(
    daily_limit: int = 200,
    intraday_limit: int = 200,
    dry_run: bool = False,
    force: bool = False,
    write_snapshot: bool = True,
    output_path: str = DEFAULT_SNAPSHOT_PATH,
) -> Dict:
    daily_result = backfill_packets(
        limit=max(0, int(daily_limit or 0)),
        snapshot_type="daily_close",
        dry_run=bool(dry_run),
        force=bool(force),
    )
    intraday_result = backfill_packets(
        limit=max(0, int(intraday_limit or 0)),
        snapshot_type="intraday",
        dry_run=bool(dry_run),
        force=bool(force),
    )
    merged = _merge_metrics(daily_result, intraday_result)
    result = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": bool(dry_run),
        "force": bool(force),
        "limits": {
            "daily_close": max(0, int(daily_limit or 0)),
            "intraday": max(0, int(intraday_limit or 0)),
        },
        "daily_close": daily_result,
        "intraday": intraday_result,
        "merged": merged,
        "coverage_before": daily_result.get("coverage_before", {}),
        "coverage_after": intraday_result.get("coverage_after", {}),
    }
    if write_snapshot:
        result["snapshot_file"] = _write_snapshot(result, output_path=output_path)
    return result

#!/usr/bin/env python3
"""Daily maintenance workflow for investor mainline."""

from __future__ import annotations

from typing import Dict

from workflows.packet_maintenance import DEFAULT_SNAPSHOT_PATH, run_packet_maintenance
from workflows.runtime_check import run_runtime_check
from workflows.sync_handoff_snapshot import DEFAULT_HANDOFF_PATH, sync_handoff_snapshot


def run_daily_maintenance(
    limit: int = 200,
    dry_run: bool = False,
    force: bool = False,
    no_write: bool = False,
    skip_runtime_check: bool = False,
    snapshot_path: str = DEFAULT_SNAPSHOT_PATH,
    handoff_path: str = DEFAULT_HANDOFF_PATH,
) -> Dict:
    packet_result = run_packet_maintenance(
        daily_limit=max(0, int(limit or 0)),
        intraday_limit=max(0, int(limit or 0)),
        dry_run=bool(dry_run),
        force=bool(force),
        write_snapshot=not bool(no_write),
        output_path=snapshot_path,
    )
    handoff_result = None
    if not no_write:
        handoff_result = sync_handoff_snapshot(
            snapshot_path=snapshot_path,
            handoff_path=handoff_path,
        )
    runtime_result = None if skip_runtime_check else run_runtime_check()
    return {
        "ok": True,
        "limit": max(0, int(limit or 0)),
        "dry_run": bool(dry_run),
        "force": bool(force),
        "no_write": bool(no_write),
        "skip_runtime_check": bool(skip_runtime_check),
        "packet_maintenance": packet_result,
        "handoff_sync": handoff_result,
        "runtime_check": runtime_result,
    }

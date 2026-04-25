#!/usr/bin/env python3
"""Sync packet maintenance snapshot into HANDOFF.md."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict


DEFAULT_SNAPSHOT_PATH = "/root/.openclaw/workspace/investor/docs/packet_maintenance_latest.json"
DEFAULT_HANDOFF_PATH = "/root/.openclaw/workspace/investor/HANDOFF.md"
START_MARKER = "<!-- packet-maintenance:start -->"
END_MARKER = "<!-- packet-maintenance:end -->"


def _load_snapshot(path: str) -> Dict:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"snapshot not found: {file_path}")
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("snapshot payload must be a JSON object")
    return payload


def _build_snapshot_markdown(snapshot: Dict) -> str:
    run_at = str(snapshot.get("run_at", "") or "")
    dry_run = bool(snapshot.get("dry_run", False))
    force = bool(snapshot.get("force", False))
    limits = snapshot.get("limits", {}) or {}
    merged = snapshot.get("merged", {}) or {}
    before = snapshot.get("coverage_before", {}) or {}
    after = snapshot.get("coverage_after", {}) or {}
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        START_MARKER,
        "",
        "## Packet 日常维护快照（自动生成）",
        "",
        f"- 同步时间：{updated_at}",
        f"- run_at：{run_at}",
        f"- dry_run：{dry_run}",
        f"- force：{force}",
        f"- limits：daily_close={limits.get('daily_close', 0)} intraday={limits.get('intraday', 0)}",
        (
            "- merged："
            f"processed={merged.get('processed', 0)} "
            f"success={merged.get('success', 0)} "
            f"skipped_already_backfilled={merged.get('skipped_already_backfilled', 0)} "
            f"failed={merged.get('failed', 0)}"
        ),
        (
            "- coverage_before："
            f"research_packets={before.get('research_packets_total', 0)} "
            f"portfolio_snapshots={before.get('portfolio_snapshots_total', 0)} "
            f"packet_dates={before.get('research_packet_dates', 0)} "
            f"portfolio_dates={before.get('portfolio_snapshot_dates', 0)}"
        ),
        (
            "- coverage_after："
            f"research_packets={after.get('research_packets_total', 0)} "
            f"portfolio_snapshots={after.get('portfolio_snapshots_total', 0)} "
            f"packet_dates={after.get('research_packet_dates', 0)} "
            f"portfolio_dates={after.get('portfolio_snapshot_dates', 0)}"
        ),
        "",
        END_MARKER,
    ]
    return "\n".join(lines)


def sync_handoff_snapshot(
    snapshot_path: str = DEFAULT_SNAPSHOT_PATH,
    handoff_path: str = DEFAULT_HANDOFF_PATH,
) -> Dict:
    snapshot = _load_snapshot(snapshot_path)
    handoff_file = Path(handoff_path)
    if not handoff_file.exists():
        raise FileNotFoundError(f"handoff not found: {handoff_file}")
    original = handoff_file.read_text(encoding="utf-8")
    block = _build_snapshot_markdown(snapshot)
    if START_MARKER in original and END_MARKER in original:
        start = original.index(START_MARKER)
        end = original.index(END_MARKER) + len(END_MARKER)
        updated = original[:start].rstrip() + "\n\n" + block + "\n" + original[end:].lstrip()
    else:
        updated = original.rstrip() + "\n\n---\n\n" + block + "\n"
    handoff_file.write_text(updated, encoding="utf-8")
    return {
        "updated": True,
        "handoff_path": str(handoff_file),
        "snapshot_path": str(snapshot_path),
        "run_at": snapshot.get("run_at", ""),
    }

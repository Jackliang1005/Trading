#!/usr/bin/env python3
"""Shared, auditable risk policy for human-facing investment advice."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = BASE_DIR / "data" / "advisor_policy.json"

DEFAULT_POLICY: Dict[str, Any] = {
    "version": 1,
    "profile_status": "system_default",
    "single_position_alert_ratio": 0.30,
    "single_position_prepare_ratio": 0.28,
    "single_position_reduce_target_ratio": 0.25,
    "loss_position_review_ratio": 0.18,
    "loss_position_reduce_target_ratio": 0.15,
    "top3_position_alert_ratio": 0.70,
    "minimum_cash_ratio": 0.03,
}


def _ratio(value: Any, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(numeric, 0.0), 1.0)


def load_advisor_policy(path: Path | None = None) -> Dict[str, Any]:
    configured = Path(os.getenv("INVESTOR_ADVISOR_POLICY_PATH", "").strip()) if os.getenv("INVESTOR_ADVISOR_POLICY_PATH", "").strip() else None
    target = path or configured or DEFAULT_POLICY_PATH
    payload: Dict[str, Any] = {}
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except Exception:
            payload = {}
    policy = dict(DEFAULT_POLICY)
    policy.update(payload)
    for key in (
        "single_position_alert_ratio",
        "single_position_prepare_ratio",
        "single_position_reduce_target_ratio",
        "loss_position_review_ratio",
        "loss_position_reduce_target_ratio",
        "top3_position_alert_ratio",
        "minimum_cash_ratio",
    ):
        policy[key] = _ratio(policy.get(key), float(DEFAULT_POLICY[key]))
    # A reduction target must be no higher than the trigger that invokes it.
    policy["single_position_reduce_target_ratio"] = min(
        policy["single_position_reduce_target_ratio"],
        policy["single_position_prepare_ratio"],
    )
    policy["loss_position_reduce_target_ratio"] = min(
        policy["loss_position_reduce_target_ratio"],
        policy["loss_position_review_ratio"],
    )
    policy["path"] = str(target)
    policy["loaded"] = bool(payload)
    return policy

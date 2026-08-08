#!/usr/bin/env python3
"""Shared, auditable risk policy for human-facing investment advice."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = BASE_DIR / "data" / "advisor_policy.json"
USER_POLICY_PATH = BASE_DIR / "runtime" / "advisor_policy_user.json"

DEFAULT_POLICY: Dict[str, Any] = {
    "version": 1,
    "profile_status": "system_default",
    "single_position_alert_ratio": 0.30,
    "single_position_prepare_ratio": 0.28,
    "single_position_reduce_target_ratio": 0.25,
    "loss_position_review_ratio": 0.18,
    "loss_position_reduce_target_ratio": 0.15,
    "loss_review_drawdown_ratio": 0.05,
    "severe_loss_drawdown_ratio": 0.20,
    "top3_position_alert_ratio": 0.70,
    "minimum_cash_ratio": 0.03,
}

ADVISOR_PROFILES: Dict[str, Dict[str, Any]] = {
    "稳健": {
        "description": "更早控制集中度和亏损，保留较高现金缓冲",
        "single_position_alert_ratio": 0.25,
        "single_position_prepare_ratio": 0.23,
        "single_position_reduce_target_ratio": 0.20,
        "loss_position_review_ratio": 0.12,
        "loss_position_reduce_target_ratio": 0.10,
        "loss_review_drawdown_ratio": 0.03,
        "severe_loss_drawdown_ratio": 0.15,
        "top3_position_alert_ratio": 0.60,
        "minimum_cash_ratio": 0.10,
    },
    "均衡": {
        "description": "兼顾回撤控制与持仓弹性",
        "single_position_alert_ratio": 0.30,
        "single_position_prepare_ratio": 0.28,
        "single_position_reduce_target_ratio": 0.25,
        "loss_position_review_ratio": 0.18,
        "loss_position_reduce_target_ratio": 0.15,
        "loss_review_drawdown_ratio": 0.05,
        "severe_loss_drawdown_ratio": 0.20,
        "top3_position_alert_ratio": 0.70,
        "minimum_cash_ratio": 0.05,
    },
    "进取": {
        "description": "容忍更高集中度和波动，但仍保留硬性风险边界",
        "single_position_alert_ratio": 0.40,
        "single_position_prepare_ratio": 0.38,
        "single_position_reduce_target_ratio": 0.35,
        "loss_position_review_ratio": 0.25,
        "loss_position_reduce_target_ratio": 0.20,
        "loss_review_drawdown_ratio": 0.08,
        "severe_loss_drawdown_ratio": 0.25,
        "top3_position_alert_ratio": 0.80,
        "minimum_cash_ratio": 0.02,
    },
}


def _ratio(value: Any, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(numeric, 0.0), 1.0)


def load_advisor_policy(path: Path | None = None) -> Dict[str, Any]:
    configured = Path(os.getenv("INVESTOR_ADVISOR_POLICY_PATH", "").strip()) if os.getenv("INVESTOR_ADVISOR_POLICY_PATH", "").strip() else None
    target = path or configured or (USER_POLICY_PATH if USER_POLICY_PATH.exists() else DEFAULT_POLICY_PATH)
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
        "loss_review_drawdown_ratio",
        "severe_loss_drawdown_ratio",
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
    policy["severe_loss_drawdown_ratio"] = max(
        policy["severe_loss_drawdown_ratio"],
        policy["loss_review_drawdown_ratio"],
    )
    if policy.get("profile_status") not in {"system_default", "user_confirmed"}:
        policy["profile_status"] = "system_default"
    if policy.get("profile_name") not in ADVISOR_PROFILES:
        policy["profile_name"] = ""
    policy["path"] = str(target)
    policy["loaded"] = bool(payload)
    return policy


def loss_review_evidence(position: Dict[str, Any], policy: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Require both meaningful drawdown evidence and portfolio-risk relevance."""
    active = policy or load_advisor_policy()
    try:
        weight = max(0.0, float(position.get("weight") or 0))
        pnl = float(position.get("pnl") or 0)
    except (TypeError, ValueError):
        weight, pnl = 0.0, 0.0
    raw_ratio = position.get("pnl_ratio")
    try:
        pnl_ratio = float(raw_ratio) if raw_ratio is not None else None
    except (TypeError, ValueError):
        pnl_ratio = None
    if pnl_ratio is None:
        try:
            market_value = float(position.get("market_value") or 0)
        except (TypeError, ValueError):
            market_value = 0.0
        inferred_cost = market_value - pnl
        pnl_ratio = pnl / inferred_cost if market_value > 0 and inferred_cost > 0 else None

    review_drawdown = float(active["loss_review_drawdown_ratio"])
    severe_drawdown = float(active["severe_loss_drawdown_ratio"])
    drawdown = abs(min(0.0, pnl_ratio)) if pnl_ratio is not None else None
    severe = bool(drawdown is not None and drawdown >= severe_drawdown)
    material_weighted = bool(
        drawdown is not None
        and drawdown >= review_drawdown
        and weight >= float(active["loss_position_review_ratio"])
    )
    required = bool(pnl < 0 and (severe or material_weighted))
    return {
        "required": required,
        "severity": "severe" if severe else "material" if material_weighted else "noise" if drawdown is not None else "unavailable",
        "pnl_ratio": pnl_ratio,
        "drawdown_ratio": drawdown,
        "review_drawdown_ratio": review_drawdown,
        "severe_drawdown_ratio": severe_drawdown,
        "weight_review_ratio": float(active["loss_position_review_ratio"]),
    }


def confirm_advisor_profile(
    profile_name: str,
    *,
    path: Path | None = None,
    confirmed_via: str = "explicit_command",
) -> Dict[str, Any]:
    """Persist a preset only after an explicit user-confirmation command."""
    name = str(profile_name or "").strip()
    if name not in ADVISOR_PROFILES:
        raise ValueError(f"unknown advisor profile: {name}")
    configured = Path(os.getenv("INVESTOR_ADVISOR_POLICY_PATH", "").strip()) if os.getenv("INVESTOR_ADVISOR_POLICY_PATH", "").strip() else None
    target = path or configured or USER_POLICY_PATH
    current = load_advisor_policy(target if target.exists() else DEFAULT_POLICY_PATH)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    history = list(current.get("history") or [])[-19:]
    if current.get("profile_name") or current.get("profile_status") == "user_confirmed":
        history.append(
            {
                "profile_name": current.get("profile_name") or "自定义",
                "profile_status": current.get("profile_status"),
                "replaced_at": now,
            }
        )
    payload = {
        "version": 2,
        "profile_status": "user_confirmed",
        "profile_name": name,
        **{key: value for key, value in ADVISOR_PROFILES[name].items() if key != "description"},
        "confirmed_at": now,
        "confirmed_via": str(confirmed_via or "explicit_command"),
        "history": history,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    reloaded = load_advisor_policy(target)
    if reloaded.get("profile_status") != "user_confirmed" or reloaded.get("profile_name") != name:
        raise RuntimeError("advisor profile read-back verification failed")
    return reloaded

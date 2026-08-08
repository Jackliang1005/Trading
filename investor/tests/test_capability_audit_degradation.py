from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path("/root/.openclaw/workspace/scripts/investor_assistant_audit.py")
SPEC = importlib.util.spec_from_file_location("investor_assistant_audit_degradation", SCRIPT)
assert SPEC and SPEC.loader
capability_audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capability_audit)


def test_single_account_outage_with_dated_fallback_is_degraded_not_blocked() -> None:
    status, fallback = capability_audit._holdings_capability_status(
        dongguan_reachable=True,
        guojin_probe_rc=2,
        risk_payload={
            "available": True,
            "positions_count": 5,
            "effective_total_asset": 400000,
            "stale_account_sources": {"main": "2026-08-08"},
        },
    )

    assert status == "warn"
    assert fallback is True


def test_total_holdings_loss_remains_blocking() -> None:
    status, fallback = capability_audit._holdings_capability_status(
        dongguan_reachable=False,
        guojin_probe_rc=2,
        risk_payload={"available": False, "positions_count": 0},
    )

    assert status == "blocked"
    assert fallback is False


def test_diagnostics_is_healthy_when_it_detects_external_outage() -> None:
    assert capability_audit._diagnostics_capability_status(
        timer_state="active",
        probe_rc=2,
        payload_available=True,
    ) == "ok"

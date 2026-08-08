import importlib.util
from pathlib import Path


SCRIPT = Path("/root/.openclaw/workspace/scripts/investor_health_alert.py")
SPEC = importlib.util.spec_from_file_location("investor_health_alert_state", SCRIPT)
health_alert = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health_alert)


def _health(ok, issue=""):
    issues = [issue] if issue else []
    return {
        "ok": ok,
        "issues": issues,
        "incident_keys": [health_alert._issue_identity(item) for item in issues],
    }


def test_issue_identity_ignores_transient_network_details():
    first = "guojin:positions_failed(http=None, error=timed out)"
    second = "guojin:positions_failed(http=502, error=Remote end closed connection)"
    assert health_alert._issue_identity(first) == health_alert._issue_identity(second)


def test_continuing_incident_is_suppressed_and_recovery_requires_confirmation():
    first = health_alert._health_transition(
        _health(False, "guojin:positions_failed(http=None, error=timed out)"),
        {},
        now=100,
        cooldown=3600,
        recovery_confirmations=2,
    )
    assert first["should_send"] is True
    assert first["incident_open"] is True

    state = {**first, "last_sent_at": 100, "last_ok": False}
    continuing = health_alert._health_transition(
        _health(False, "guojin:positions_failed(http=502, error=closed)"),
        state,
        now=200,
        cooldown=3600,
        recovery_confirmations=2,
    )
    assert continuing["should_send"] is False

    first_healthy = health_alert._health_transition(
        _health(True),
        {**state, **continuing},
        now=300,
        cooldown=3600,
        recovery_confirmations=2,
    )
    assert first_healthy["recovered"] is False
    assert first_healthy["incident_open"] is True

    second_healthy = health_alert._health_transition(
        _health(True),
        {**state, **first_healthy},
        now=400,
        cooldown=3600,
        recovery_confirmations=2,
    )
    assert second_healthy["recovered"] is True
    assert second_healthy["should_send"] is True
    assert second_healthy["incident_open"] is False


def test_recovery_is_held_open_when_portfolio_refresh_is_incomplete():
    transition = {
        "recovered": True,
        "should_send": True,
        "incident_open": False,
        "incident_keys": [],
        "consecutive_ok": 2,
    }
    previous = {"incident_keys": ["guojin:positions_failed"]}

    reconciled, detail = health_alert._reconcile_recovery_transition(
        transition,
        previous,
        recovery_confirmations=2,
        dry_run=False,
        refresh_fn=lambda: {"verified": False, "saved": False, "reason": "account_sources_incomplete"},
    )

    assert reconciled["recovered"] is False
    assert reconciled["should_send"] is False
    assert reconciled["incident_open"] is True
    assert reconciled["incident_keys"] == ["guojin:positions_failed"]
    assert detail["attempted"] is True


def test_recovery_closes_only_after_verified_snapshot_is_saved():
    transition = {
        "recovered": True,
        "should_send": True,
        "incident_open": False,
        "incident_keys": [],
        "consecutive_ok": 2,
    }

    reconciled, detail = health_alert._reconcile_recovery_transition(
        transition,
        {"incident_keys": ["guojin:positions_failed"]},
        recovery_confirmations=2,
        dry_run=False,
        refresh_fn=lambda: {
            "verified": True,
            "saved": True,
            "verified_sources": ["main", "trade"],
            "snapshot_id": 42,
        },
    )

    assert reconciled["recovered"] is True
    assert reconciled["should_send"] is True
    assert detail["snapshot_id"] == 42


def test_dry_run_does_not_consume_incident_state(monkeypatch):
    monkeypatch.setattr(health_alert, "collect_health", lambda timeout=3: _health(False, "guojin:health_failed(http=None, error=timeout)"))
    monkeypatch.setattr(health_alert, "_load_state", lambda: {})
    saved = []
    monkeypatch.setattr(health_alert, "_save_state", lambda state: saved.append(state))
    monkeypatch.setattr(health_alert.sys, "argv", ["investor_health_alert.py", "--dry-run"])

    assert health_alert.main() == 2
    assert saved == []

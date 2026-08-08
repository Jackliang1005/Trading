import subprocess
import sys

from domain.services import reflection_runtime_service
from domain.services import risk_report_service
from position_pnl import resolve_position_pnl
from qmt_client import QMTManager


def test_data_collector_cold_import_has_no_qmt_circular_dependency():
    completed = subprocess.run(
        [sys.executable, "-c", "import data_collector; import qmt_client"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def _qmt_position():
    return {
        "_source": "main",
        "stock_code": "603986.SH",
        "stock_name": "兆易创新",
        "volume": 500,
        "available_volume": 500,
        "avg_price": 596.13706,
        "last_price": 417.05,
        "market_value": 208525.0,
        "position_profit": -89543.53,
        "float_profit": 16025.0,
        "profit_rate": -0.3004,
    }


def test_cumulative_position_profit_wins_over_daily_float_and_matches_cost_basis():
    evidence = resolve_position_pnl(_qmt_position())

    assert evidence["pnl"] == -89543.53
    assert evidence["basis"] == "position_profit"
    assert evidence["pnl_pct"] == -30.0413
    assert evidence["daily_float_profit"] == 16025.0
    assert evidence["cumulative_cost_conflict"] is False


def test_float_profit_remains_a_legacy_fallback_when_no_cost_or_cumulative_field_exists():
    evidence = resolve_position_pnl({"market_value": 10000, "float_profit": -500})

    assert evidence["pnl"] == -500
    assert evidence["basis"] == "float_profit_fallback"


def test_reflection_does_not_turn_a_cumulative_loss_into_a_profit():
    report = reflection_runtime_service.build_trading_summary_report(
        {"positions": [_qmt_position()], "total_market_value": 208525, "total_unrealized_pnl": 16025}
    )

    assert "总未实现盈亏: -89,543.53" in report
    assert "-30.04%" in report
    assert "总未实现盈亏: +16,025.00" not in report


def test_qmt_combined_summary_uses_cumulative_position_profit(monkeypatch):
    manager = QMTManager.__new__(QMTManager)
    monkeypatch.setattr(manager, "get_all_accounts", lambda: {})
    monkeypatch.setattr(manager, "get_all_positions", lambda: [_qmt_position()])
    monkeypatch.setattr(manager, "get_all_trades", lambda: [])
    monkeypatch.setattr(manager, "get_all_orders", lambda: [])
    monkeypatch.setattr(manager, "_sources", lambda: [])
    manager.last_errors = {}

    summary = manager.get_trading_summary()

    assert summary["total_unrealized_pnl"] == -89543.53


def test_risk_report_uses_cumulative_pnl_and_exposes_its_basis(monkeypatch):
    snapshot = {
        "created_at": "2026-08-08 10:00:00",
        "as_of_date": "2026-08-08",
        "data": {
            "qmt_account": {"account_id": "test", "cash": 1000, "total_asset": 209525},
            "qmt_positions": [_qmt_position()],
        },
    }
    monkeypatch.setattr(risk_report_service, "_init_db_quietly", lambda: None)
    monkeypatch.setattr(risk_report_service, "_select_risk_snapshot", lambda: (snapshot, False))
    monkeypatch.setattr(risk_report_service, "_snapshot_age_days", lambda value: 0)

    report = risk_report_service.build_risk_report()

    assert report["total_unrealized_pnl"] == -89543.53
    assert report["top_positions"][0]["pnl"] == -89543.53
    assert report["top_positions"][0]["pnl_ratio"] == -0.3004
    assert report["top_positions"][0]["pnl_bases"] == ["position_profit"]
    assert "portfolio_unrealized_loss" in report["risk_flags"]


def test_inconsistent_cumulative_and_cost_values_are_flagged(monkeypatch):
    row = _qmt_position()
    row["position_profit"] = -1000
    snapshot = {
        "created_at": "2026-08-08 10:00:00",
        "as_of_date": "2026-08-08",
        "data": {
            "qmt_account": {"account_id": "test", "cash": 1000, "total_asset": 209525},
            "qmt_positions": [row],
        },
    }
    monkeypatch.setattr(risk_report_service, "_init_db_quietly", lambda: None)
    monkeypatch.setattr(risk_report_service, "_select_risk_snapshot", lambda: (snapshot, False))
    monkeypatch.setattr(risk_report_service, "_snapshot_age_days", lambda value: 0)

    report = risk_report_service.build_risk_report()

    assert report["top_positions"][0]["pnl_conflict"] is True
    assert report["top_positions"][0]["pnl"] == -89543.53
    assert report["top_positions"][0]["pnl_bases"] == ["conservative_conflict_min"]
    assert "position_pnl_conflict" in report["risk_flags"]
    assert "持仓盈亏字段与成本口径冲突" in report["text"]

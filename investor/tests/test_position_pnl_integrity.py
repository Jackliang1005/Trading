import subprocess
import sys

from domain.services import reflection_runtime_service
from domain.services import risk_report_service
from domain.services.report_style_service import position_pnl_pct
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


def test_position_pnl_percentage_preserves_cost_noise_precision():
    assert position_pnl_pct(-0.0001) == "-0.01%"
    assert position_pnl_pct(-0.30) == "-30.0%"


def test_reflection_accepts_actual_qmt_traded_field_names():
    trade = {
        "stock_code": "513050.SH",
        "traded_volume": 32700,
        "traded_price": 1.161,
        "order_type": 23,
    }

    assert reflection_runtime_service._valid_trades({"today_trades": [trade]}) == [trade]


def test_normalized_portfolio_position_preserves_cumulative_pnl_and_cost_ratio():
    evidence = resolve_position_pnl(
        {
            "market_value": 250230,
            "cost_value": 367376.14,
            "pnl": -117146.14,
            "volume": 600,
        }
    )

    assert evidence["pnl"] == -117146.14
    assert evidence["basis"] == "market_value_minus_cost"
    assert evidence["pnl_pct"] == -31.8872


def test_reflection_does_not_turn_a_cumulative_loss_into_a_profit():
    report = reflection_runtime_service.build_trading_summary_report(
        {"positions": [_qmt_position()], "total_market_value": 208525, "total_unrealized_pnl": 16025}
    )

    assert "总未实现盈亏: -89,543.53" in report
    assert "-30.04%" in report
    assert "总未实现盈亏: +16,025.00" not in report


def test_reflection_prefers_cross_account_risk_inventory_over_stale_packet_totals(monkeypatch):
    risk = {
        "available": True,
        "as_of": "2026-08-08",
        "data_status": "current_snapshot",
        "position_coverage_complete": True,
        "account_source_coverage_complete": False,
        "stale_account_sources": ["guojin"],
        "missing_account_sources": [],
        "positions": [
            {
                "code": "603986.SH",
                "name": "兆易创新",
                "volume": 600,
                "market_value": 250230,
                "cost_value": 367376.14,
                "pnl": -117146.14,
                "pnl_ratio": -0.3189,
            },
            {
                "code": "513050.SH",
                "name": "中概互联网ETF易",
                "volume": 66100,
                "market_value": 76676,
                "cost_value": 76685.3,
                "pnl": -9.3,
                "pnl_ratio": -0.0001,
            },
        ],
    }
    monkeypatch.setattr(risk_report_service, "build_risk_report", lambda: risk)
    context = {
        "as_of_date": "2026-08-08",
        "trading_summary": {
            "positions": [_qmt_position()],
            "positions_count": 1,
            "total_unrealized_pnl": 16025,
        },
    }

    summary = reflection_runtime_service._build_reflection_trading_summary(context)
    _, positions = reflection_runtime_service._build_positions_table(summary["positions"])

    assert summary["positions_count"] == 2
    assert summary["total_unrealized_pnl"] == -117155.44
    assert sum(item["pnl"] for item in positions) == -117155.44
    assert summary["reflection_position_source"] == "portfolio_risk"
    assert summary["stale_account_sources"] == ["guojin"]
    text = reflection_runtime_service.format_reflection_push_text(
        summary,
        "",
        {},
        "2026-08-08 20:30:00",
        "2026-08-08",
    )
    assert "组合累计持仓盈亏 -11.72万" in text
    assert "账户 国金 使用最近可验证历史快照" in text
    assert "不是全账户实时状态" in text
    report = reflection_runtime_service.build_trading_summary_report(summary)
    assert "持仓与累计盈亏采用组合风险模块的跨账户聚合口径" in report
    assert "历史降级账户: 国金" in report


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
    assert "累计持仓盈亏 -8.95万" in report["text"]
    assert "浮动盈亏" not in report["text"]


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

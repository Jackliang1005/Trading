from datetime import datetime
from unittest.mock import patch

from domain.services import decision_monitor_service as service
from domain.services import risk_report_service


def _position(index: int):
    code = f"{600000 + index:06d}.SH"
    return {
        "code": code,
        "name": f"持仓{index}",
        "source": "trade",
        "sources": ["trade"],
        "weight": 0.05,
        "volume": 1000,
        "available_volume": 1000,
        "available_volume_complete": True,
        "market_value": 10000,
        "pnl": 100,
    }


def _patch_runtime(monkeypatch, closing, risk, change_pct=0.2):
    today = datetime.now().strftime("%Y%m%d")

    def quotes(codes):
        rows = {
            service._code_key(code): {
                "available": True,
                "change_available": True,
                "price": 10,
                "change_pct": change_pct,
                "as_of": today + "103000",
            }
            for code in codes
        }
        return rows, ""

    monkeypatch.setattr(service, "_load_latest_closing_payload", lambda: closing)
    monkeypatch.setattr(service, "build_risk_report", lambda: risk)
    monkeypatch.setattr(service, "_is_trading_session", lambda now: True)
    monkeypatch.setattr(service, "_fetch_live_cash", lambda: (None, False, "test"))
    monkeypatch.setattr(service, "_fetch_realtime_quotes", quotes)
    monkeypatch.setattr(service, "is_cn_trading_day", lambda value: (True, "test"))


def test_current_inventory_drives_monitor_even_when_plan_is_missing_and_has_more_than_eight_positions(monkeypatch):
    positions = [_position(index) for index in range(9)]
    risk = {
        "available": True,
        "as_of": "2026-08-10",
        "cash": 0,
        "cash_complete": False,
        "total_market_value": 90000,
        "top1_ratio": 0.05,
        "top3_ratio": 0.15,
        "position_coverage_complete": True,
        "positions": positions,
        "top_positions": positions[:8],
    }
    closing = {"available": False, "error": "closing plan missing"}
    _patch_runtime(monkeypatch, closing, risk)

    monitor = service.build_decision_monitor(slot="1030")

    assert monitor["available"] is True
    assert monitor["plan_available"] is False
    assert len(monitor["tracked_positions"]) == 9
    assert all(item["status"] == "current" for item in monitor["tracked_positions"])
    with patch.object(service, "build_decision_monitor", return_value=monitor):
        text = service.format_decision_monitor_text(slot="1030")
    assert "仅按可验证持仓检查风险" in text


def test_complete_current_inventory_drops_a_plan_only_position(monkeypatch):
    current = _position(1)
    ghost = _position(2)
    risk = {
        "available": True,
        "cash_complete": False,
        "position_coverage_complete": True,
        "positions": [current],
        "top_positions": [current],
    }
    closing = {"available": True, "decision_plan": {"positions": [current, ghost]}}
    _patch_runtime(monkeypatch, closing, risk)

    monitor = service.build_decision_monitor(slot="1030")

    assert [item["code"] for item in monitor["tracked_positions"]] == [current["code"]]


def test_incomplete_inventory_keeps_plan_only_position_but_never_makes_it_executable(monkeypatch):
    current = _position(1)
    unconfirmed = _position(2)
    unconfirmed["weight"] = 0.6
    risk = {
        "available": True,
        "cash_complete": False,
        "position_coverage_complete": False,
        "positions": [current],
        "top_positions": [current],
    }
    closing = {"available": True, "decision_plan": {"positions": [current, unconfirmed]}}
    _patch_runtime(monkeypatch, closing, risk, change_pct=-4.0)

    monitor = service.build_decision_monitor(slot="1030")
    planned = next(item for item in monitor["tracked_positions"] if item["code"] == unconfirmed["code"])

    assert planned["status"] == "from_plan_unconfirmed"
    assert planned["decision_state"] == "position_unconfirmed"
    assert planned["action_level"] == "verify"
    assert planned["execution_hint"]["actionable"] is False
    assert "不能生成交易数量" in planned["execution_hint"]["note"]
    with patch.object(service, "build_decision_monitor", return_value=monitor):
        text = service.format_decision_monitor_text(slot="1030")
    assert "仅来自上一交易日计划的证券已标为待核验" in text


def test_risk_report_exposes_complete_inventory_separately_from_top_eight(monkeypatch):
    rows = [
        {
            "_source": "trade",
            "stock_code": f"{600000 + index:06d}.SH",
            "stock_name": f"持仓{index}",
            "volume": 1000,
            "available_volume": 1000,
            "market_value": (10 - index) * 1000,
        }
        for index in range(9)
    ]
    snapshot = {
        "created_at": "2026-08-10 10:00:00",
        "as_of_date": "2026-08-10",
        "data": {
            "qmt_account": {"account_id": "test", "cash": 1000, "total_asset": 55000},
            "qmt_positions": rows,
        },
    }
    monkeypatch.setattr(risk_report_service, "_init_db_quietly", lambda: None)
    monkeypatch.setattr(risk_report_service, "_select_risk_snapshot", lambda: (snapshot, False))
    monkeypatch.setattr(risk_report_service, "_snapshot_age_days", lambda value: 0)

    report = risk_report_service.build_risk_report()

    assert report["positions_count"] == 9
    assert len(report["positions"]) == 9
    assert len(report["top_positions"]) == 8
    assert report["positions"][:8] == report["top_positions"]

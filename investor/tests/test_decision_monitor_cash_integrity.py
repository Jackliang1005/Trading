from unittest.mock import patch

from domain.services import decision_monitor_service as service


def test_partial_account_cash_never_becomes_a_zero_percent_cash_claim(monkeypatch):
    closing = {"available": True, "decision_plan": {"positions": []}}
    risk = {
        "available": True,
        "cash": 116.23,
        "cash_ratio": 0.0003,
        "cash_complete": False,
        "total_market_value": 359920.0,
        "top1_ratio": 0.5792,
        "top3_ratio": 0.8899,
        "risk_flags": ["top1_concentration_high"],
    }
    monkeypatch.setattr(service, "_load_latest_closing_payload", lambda: closing)
    monkeypatch.setattr(service, "build_risk_report", lambda: risk)
    monkeypatch.setattr(service, "_is_trading_session", lambda now: False)
    monkeypatch.setattr(service, "is_cn_trading_day", None)

    monitor = service.build_decision_monitor(slot="0935")

    assert monitor["cash"] == 116.23
    assert monitor["cash_complete"] is False
    assert monitor["cash_ratio"] is None

    with patch.object(service, "build_decision_monitor", return_value=monitor):
        text = service.format_decision_monitor_text(slot="0935")

    assert "可验证现金 116.23元" in text
    assert "不计算完整现金占比" in text
    assert "现金 116.23元（0.0%）" not in text
    assert "不据此判断资金是否充足" in text


def test_unknown_cash_ratio_does_not_trigger_cash_shortage_advice():
    state, action = service._action_for_position(
        position={"weight": 0.1, "pnl": 100, "source": "main"},
        cash_ratio=None,
        quote={"available": True, "change_available": True, "change_pct": 0.2},
        benchmark={"change_pct": 0.1},
        trading_session=True,
        quote_fresh=True,
    )

    assert state == "observe"
    assert "现金不足" not in action

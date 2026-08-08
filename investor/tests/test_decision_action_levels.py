from unittest.mock import patch

from domain.services import decision_monitor_service as service


def test_action_level_requires_live_evidence_and_actionable_quantity():
    hint = {"actionable": True, "suggested_qty": 100}

    assert service._action_level("reduce_priority", True, True, hint) == "prepare"
    assert service._action_level("reduce_priority", True, False, hint) == "verify"
    assert service._action_level("reduce_priority", False, True, hint) == "observe"
    assert service._action_level("reduce_priority", True, True, {"actionable": False}) == "verify"
    assert service._action_level("observe", True, True, {}) == "observe"


def test_human_report_explains_prepare_level_without_claiming_execution():
    monitor = {
        "available": True,
        "generated_at": "2026-08-10 10:30:00",
        "trading_session": True,
        "cash": 50000,
        "cash_ratio": 0.2,
        "cash_complete": True,
        "top1_ratio": 0.4,
        "top3_ratio": 0.7,
        "risk_flags": ["top1_concentration_high"],
        "quote_available_count": 5,
        "tracked_positions": [
            {
                "name": "示例股份",
                "code": "600000.SH",
                "source": "main",
                "weight": 0.4,
                "action_level": "prepare",
                "suggestion": "交易建议: 减仓优先。",
                "quote": {"available": True, "change_available": True, "price": 10, "change_pct": -3.2},
                "execution_hint": {"actionable": True, "note": "建议减仓 100 股。"},
            }
        ],
        "opportunities": [],
    }

    with patch.object(service, "build_decision_monitor", return_value=monitor):
        text = service.format_decision_monitor_text(slot="1030")

    assert "进入“准备”层级" in text
    assert "行动 准备" in text
    assert "数量参考：建议减仓 100 股" in text
    assert "仍须人工确认" in text
    assert "不会自动下单" in text


def test_verify_level_does_not_expose_an_execution_quantity():
    monitor = {
        "available": True,
        "generated_at": "2026-08-10 10:30:00",
        "trading_session": True,
        "cash": 0,
        "cash_ratio": None,
        "cash_complete": False,
        "top1_ratio": 0.3,
        "top3_ratio": 0.6,
        "risk_flags": [],
        "quote_available_count": 1,
        "tracked_positions": [
            {
                "name": "示例股份",
                "code": "600000.SH",
                "source": "main",
                "weight": 0.3,
                "action_level": "verify",
                "suggestion": "缺少日内涨跌证据，先核验。",
                "quote": {"available": True, "change_available": False, "price": 10},
                "execution_hint": {"actionable": True, "note": "建议减仓 100 股。"},
            }
        ],
        "opportunities": [],
    }

    with patch.object(service, "build_decision_monitor", return_value=monitor):
        text = service.format_decision_monitor_text(slot="1030")

    assert "进入“核验”层级" in text
    assert "行动 核验" in text
    assert "数量参考：建议减仓 100 股" not in text

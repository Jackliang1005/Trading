from unittest.mock import patch

from domain.services import decision_monitor_service as service


def test_action_level_requires_live_evidence_and_actionable_quantity():
    hint = {"actionable": True, "suggested_qty": 100}

    assert service._action_level("reduce_priority", True, True, hint) == "prepare"
    assert service._action_level("reduce_priority", True, False, hint) == "verify"
    assert service._action_level("reduce_priority", False, True, hint) == "observe"
    assert service._action_level("reduce_priority", True, True, {"actionable": False}) == "verify"
    assert service._action_level("observe", True, True, {}) == "observe"


def test_cross_account_position_never_generates_one_combined_sell_quantity():
    hint = service._reduce_execution_hint(
        {
            "weight": 0.62,
            "volume": 600,
            "source": "combined",
            "sources": ["main", "trade"],
        },
        "reduce_priority",
    )

    assert hint["actionable"] is False
    assert hint["suggested_qty"] == 0
    assert "分账户核对可用数量" in hint["note"]


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


def test_market_closed_keeps_severe_drawdown_review_visible_without_quantity():
    position = {
        "weight": 0.05,
        "market_value": 70000,
        "cost_value": 100000,
        "pnl": -30000,
        "pnl_ratio": -0.30,
        "source": "trade",
    }

    state, suggestion = service._action_for_position(
        position=position,
        cash_ratio=None,
        quote={},
        benchmark={},
        trading_session=False,
        quote_fresh=False,
    )

    assert state == "market_closed_loss_review"
    assert "累计回撤 30.0% 已达到复核门槛" in suggestion
    assert "不生成卖出数量" in suggestion


def test_market_closed_report_prioritizes_loss_review_and_shows_cost_risk():
    monitor = {
        "available": True,
        "generated_at": "2026-08-08 18:20:00",
        "trading_session": False,
        "cash": 100,
        "cash_ratio": None,
        "cash_complete": False,
        "top1_ratio": 0.62,
        "top3_ratio": 0.90,
        "risk_flags": [],
        "tracked_positions": [
            {
                "name": "示例股份",
                "code": "600000.SH",
                "source": "trade",
                "weight": 0.05,
                "action_level": "observe",
                "suggestion": "非交易时段：累计回撤 30.0% 已达到复核门槛；当前不生成卖出数量。",
                "quote": {"available": False},
                "loss_review": {"required": True, "pnl_ratio": -0.30},
                "execution_hint": {"actionable": False, "note": ""},
            },
            {
                "name": "示例ETF",
                "code": "510000.SH",
                "source": "main",
                "weight": 0.20,
                "action_level": "observe",
                "suggestion": "非交易时段：保留复盘计划。",
                "quote": {"available": False},
                "loss_review": {"required": False, "pnl_ratio": -0.0001, "severity": "noise"},
                "execution_hint": {"actionable": False, "note": ""},
            },
        ],
        "opportunities": [],
    }

    with patch.object(service, "build_decision_monitor", return_value=monitor):
        text = service.format_decision_monitor_text(slot="Feishu 查询")

    assert "1 只持仓的累计回撤达到复核门槛" in text
    assert "累计盈亏 -30.0%｜下次复核 优先" in text
    assert "累计盈亏 -0.01%（成本噪声）｜下次复核 常规" in text
    assert "盘外不生成价格触发或卖出数量" in text
    assert "数量参考：" not in text


def test_position_display_priority_puts_loss_review_before_regular_observation():
    regular = {
        "action_level": "observe",
        "weight": 0.20,
        "loss_review": {"required": False, "pnl_ratio": -0.0001},
    }
    severe = {
        "action_level": "observe",
        "weight": 0.02,
        "loss_review": {"required": True, "pnl_ratio": -0.40},
    }

    ordered = sorted([regular, severe], key=service._position_display_priority)

    assert ordered == [severe, regular]


def test_position_display_priority_uses_portfolio_loss_impact_within_review_tier():
    concentrated_loss = {
        "action_level": "observe",
        "weight": 0.62,
        "pnl": -117000,
        "loss_review": {"required": True, "pnl_ratio": -0.32},
    }
    small_severe_loss = {
        "action_level": "observe",
        "weight": 0.02,
        "pnl": -5300,
        "loss_review": {"required": True, "pnl_ratio": -0.41},
    }

    ordered = sorted([small_severe_loss, concentrated_loss], key=service._position_display_priority)

    assert ordered == [concentrated_loss, small_severe_loss]

from domain.services import closing_brief_service as closing
from domain.services import decision_monitor_service as monitor
from domain.services import risk_report_service as risk_service


def _confirmed_policy():
    policy = monitor.load_advisor_policy()
    policy["profile_status"] = "user_confirmed"
    return policy


def test_risk_exposure_preserves_fresh_sellable_quantity_by_account():
    rows = [
        {
            "stock_code": "603986.SH",
            "stock_name": "兆易创新",
            "volume": 400,
            "can_use_volume": 300,
            "market_value": 160000,
            "_source": "trade",
        },
        {
            "stock_code": "603986.SH",
            "stock_name": "兆易创新",
            "volume": 200,
            "can_use_volume": 200,
            "market_value": 80000,
            "_source": "main",
        },
    ]

    exposure = risk_service._aggregate_security_exposures(rows, {"main"})[0]

    assert exposure["volume"] == 600
    assert exposure["available_volume"] == 300
    assert exposure["available_volume_complete"] is False
    assert exposure["stale_sources"] == ["main"]
    assert exposure["account_positions"][1]["available_volume"] is None


def test_intraday_quantity_requires_broker_available_volume_evidence():
    hint = monitor._reduce_execution_hint(
        {"weight": 0.50, "volume": 1000, "source": "trade", "sources": ["trade"]},
        "reduce_priority",
        policy=_confirmed_policy(),
    )

    assert hint["actionable"] is False
    assert hint["suggested_qty"] == 0
    assert "不能把总持仓当作可卖数量" in hint["note"]


def test_intraday_quantity_is_capped_by_sellable_shares_without_claiming_target():
    hint = monitor._reduce_execution_hint(
        {
            "weight": 0.50,
            "volume": 1000,
            "available_volume": 250,
            "available_volume_complete": True,
            "market_value": 10000,
            "source": "trade",
            "sources": ["trade"],
        },
        "reduce_priority",
        policy=_confirmed_policy(),
    )

    assert hint["actionable"] is True
    assert hint["required_qty"] == 500
    assert hint["suggested_qty"] == 200
    assert hint["target_reachable"] is False
    assert hint["estimated_notional"] == 2000
    assert hint["transaction_cost_estimated"] is False
    assert "剩余数量待冻结解除" in hint["note"]
    assert "未计佣金、印花税和滑点" in hint["note"]


def test_intraday_quantity_rounds_up_to_reach_target_instead_of_under_selling():
    hint = monitor._reduce_execution_hint(
        {
            "weight": 0.304,
            "volume": 76100,
            "available_volume": 76100,
            "available_volume_complete": True,
            "source": "trade",
            "sources": ["trade"],
        },
        "reduce_candidate",
        policy=_confirmed_policy(),
    )

    assert hint["suggested_qty"] == 13600
    assert hint["target_reachable"] is True


def test_intraday_stale_or_sub_lot_availability_stays_non_actionable():
    stale = monitor._reduce_execution_hint(
        {
            "weight": 0.50,
            "volume": 1000,
            "available_volume": 1000,
            "available_volume_complete": True,
            "stale_sources": ["main"],
            "source": "main",
            "sources": ["main"],
        },
        "reduce_priority",
        policy=_confirmed_policy(),
    )
    sub_lot = monitor._reduce_execution_hint(
        {
            "weight": 0.50,
            "volume": 1000,
            "available_volume": 80,
            "available_volume_complete": True,
            "source": "trade",
            "sources": ["trade"],
        },
        "reduce_priority",
        policy=_confirmed_policy(),
    )

    assert stale["actionable"] is False
    assert "过期账户回退" in stale["note"]
    assert sub_lot["actionable"] is False
    assert "不足 100 股整手" in sub_lot["note"]


def _closing_payload(position, effective_total=1000):
    return {
        "date": "2026-08-08",
        "events": {},
        "global_impact": {},
        "market_review": {"sentiment": "震荡", "indices": []},
        "risk": {
            "available": True,
            "effective_total_asset": effective_total,
            "cash": 0,
            "cash_ratio": 0,
            "cash_complete": True,
            "top1_ratio": float(position["weight"]),
            "top3_ratio": float(position["weight"]),
            "risk_flags": ["top1_concentration_high"],
            "top_positions": [position],
        },
    }


def test_closing_etf_quantity_uses_board_lot_and_discloses_unpriced_costs():
    payload = _closing_payload(
        {
            "code": "513290.SH",
            "name": "纳指生物科技ETF",
            "weight": 0.40,
            "pnl": 10,
            "source": "trade",
            "sources": ["trade"],
            "volume": 500,
            "available_volume": 500,
            "available_volume_complete": True,
            "market_value": 400,
        }
    )

    decision = closing.build_decision_plan(payload)["position_decisions"][0]

    assert decision["quantity_actionable"] is True
    assert decision["suggested_qty"] == 200
    assert decision["target_reachable"] is True
    assert decision["estimated_notional"] == 160
    assert decision["transaction_cost_estimated"] is False
    assert "未计佣金、印花税和滑点" in decision["advice"]


def test_closing_small_adjustment_does_not_overtrade_one_lot():
    payload = _closing_payload(
        {
            "code": "600000.SH",
            "name": "示例股份",
            "weight": 0.28,
            "pnl": 10,
            "source": "trade",
            "sources": ["trade"],
            "volume": 200,
            "available_volume": 200,
            "available_volume_complete": True,
            "market_value": 280,
        }
    )

    decision = closing.build_decision_plan(payload)["position_decisions"][0]

    assert decision["quantity_actionable"] is False
    assert decision["suggested_qty"] == 0
    assert "避免过度调整" in decision["advice"]


def test_closing_missing_sellable_quantity_keeps_direction_but_no_order_size():
    payload = _closing_payload(
        {
            "code": "600000.SH",
            "name": "示例股份",
            "weight": 0.50,
            "pnl": -10,
            "source": "trade",
            "sources": ["trade"],
            "volume": 1000,
            "market_value": 500,
        }
    )

    decision = closing.build_decision_plan(payload)["position_decisions"][0]

    assert decision["action_level"] == "verify"
    assert decision["quantity_actionable"] is False
    assert decision["suggested_qty"] == 0
    assert "未返回完整可用股数" in decision["advice"]

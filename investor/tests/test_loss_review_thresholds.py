from domain.policies.advisor_policy import load_advisor_policy, loss_review_evidence
from domain.services import closing_brief_service, decision_monitor_service, feishu_query_service


def _position(weight, pnl_ratio, market_value=100000):
    cost_value = market_value / (1 + pnl_ratio)
    pnl = market_value - cost_value
    return {
        "code": "600001.SH",
        "name": "test holding",
        "source": "trade",
        "sources": ["trade"],
        "weight": weight,
        "market_value": market_value,
        "cost_value": cost_value,
        "pnl": pnl,
        "pnl_ratio": pnl_ratio,
        "volume": 1000,
        "available_volume": 1000,
        "available_volume_complete": True,
    }


def test_tiny_loss_does_not_trigger_review_just_because_position_weight_is_large():
    policy = load_advisor_policy()
    evidence = loss_review_evidence(_position(0.19, -0.0001), policy)

    assert evidence["required"] is False
    assert evidence["severity"] == "noise"


def test_material_loss_requires_weight_and_drawdown_thresholds_together():
    policy = load_advisor_policy()

    assert loss_review_evidence(_position(0.19, -0.06), policy)["required"] is True
    assert loss_review_evidence(_position(0.10, -0.06), policy)["required"] is False


def test_severe_drawdown_requires_review_even_when_position_weight_is_small():
    policy = load_advisor_policy()
    evidence = loss_review_evidence(_position(0.05, -0.30), policy)

    assert evidence["required"] is True
    assert evidence["severity"] == "severe"


def test_missing_cost_evidence_does_not_turn_any_negative_number_into_a_loss_signal():
    policy = load_advisor_policy()
    evidence = loss_review_evidence({"weight": 0.25, "pnl": -10}, policy)

    assert evidence["required"] is False
    assert evidence["severity"] == "unavailable"


def test_intraday_decision_uses_drawdown_evidence_instead_of_pnl_sign():
    policy = load_advisor_policy()
    common = {
        "cash_ratio": None,
        "quote": {"available": True, "change_available": True, "change_pct": 0.2},
        "benchmark": {"change_pct": 0.1},
        "trading_session": True,
        "quote_fresh": True,
        "policy": policy,
    }

    tiny_state, tiny_text = decision_monitor_service._action_for_position(
        position=_position(0.19, -0.0001),
        **common,
    )
    severe_state, severe_text = decision_monitor_service._action_for_position(
        position=_position(0.05, -0.30),
        **common,
    )

    assert tiny_state == "trade_position_watch"
    assert "\u4e0d\u8865\u4ed3" not in tiny_text
    assert severe_state == "hold_or_reduce"
    assert "\u7d2f\u8ba1\u56de\u64a4 30.0%" in severe_text


def test_closing_plan_marks_severe_small_position_for_verification_but_not_fake_quantity():
    policy = load_advisor_policy()
    position = _position(0.05, -0.30)
    payload = {"events": {}, "global_impact": {}, "market_review": {}}
    risk = {
        "available": True,
        "positions": [position],
        "top_positions": [position],
        "effective_total_asset": 2000000,
    }

    decisions = closing_brief_service._position_decisions(risk, payload, policy)

    assert decisions[0]["action_level"] == "verify"
    assert decisions[0]["loss_review"]["severity"] == "severe"
    assert decisions[0]["quantity_actionable"] is False
    assert "\u7d2f\u8ba1\u56de\u64a4 30.0%" in decisions[0]["advice"]


def test_profile_view_explains_weight_and_drawdown_are_different_thresholds():
    text = feishu_query_service._query_advisor_profile(
        "/\u98ce\u9669\u504f\u597d \u5747\u8861"
    )

    assert "\u4e8f\u635f\u4ed3\u6743\u91cd\u590d\u6838 / \u964d\u98ce\u9669\u76ee\u6807\uff1a18% / 15%" in text
    assert "\u7d2f\u8ba1\u56de\u64a4\u89e6\u53d1\uff1a\u666e\u901a 5% / \u4e25\u91cd 20%" in text

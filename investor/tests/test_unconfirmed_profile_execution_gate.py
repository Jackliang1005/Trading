from domain.services import decision_monitor_service as service


def _policy(status: str):
    return {
        "profile_status": status,
        "single_position_prepare_ratio": 0.28,
        "single_position_reduce_target_ratio": 0.25,
        "loss_position_review_ratio": 0.18,
        "loss_position_reduce_target_ratio": 0.15,
        "loss_review_drawdown_ratio": 0.05,
        "severe_loss_drawdown_ratio": 0.20,
        "minimum_cash_ratio": 0.03,
    }


def _position():
    return {
        "weight": 0.40,
        "volume": 1000,
        "available_volume": 1000,
        "available_volume_complete": True,
        "market_value": 10000,
        "source": "main",
        "sources": ["main"],
        "pnl_ratio": -0.10,
    }


def test_system_default_profile_keeps_risk_signal_but_blocks_target_and_quantity():
    policy = _policy("system_default")
    state, suggestion = service._action_for_position(
        position=_position(),
        cash_ratio=0.10,
        quote={"available": True, "change_available": True, "change_pct": -2.0},
        benchmark={"change_pct": 0.0},
        trading_session=True,
        quote_fresh=True,
        policy=policy,
    )
    hint = service._reduce_execution_hint(_position(), state, policy=policy)

    assert state == "reduce_priority"
    assert "\u51cf\u4ed3\u4f18\u5148" in suggestion
    assert "\u4e2a\u4eba\u98ce\u9669\u753b\u50cf\u5c1a\u672a\u786e\u8ba4" in suggestion
    assert hint["actionable"] is False
    assert hint["target_weight"] is None
    assert hint["suggested_qty"] == 0
    assert service._action_level(state, True, True, hint) == "verify"


def test_user_confirmed_profile_allows_broker_backed_quantity_reference():
    policy = _policy("user_confirmed")
    hint = service._reduce_execution_hint(_position(), "reduce_priority", policy=policy)

    assert hint["actionable"] is True
    assert hint["target_weight"] == 0.25
    assert hint["suggested_qty"] == 400
    assert service._action_level("reduce_priority", True, True, hint) == "prepare"


def test_unconfirmed_candidate_does_not_disclose_default_target_ratio():
    policy = _policy("system_default")
    state, suggestion = service._action_for_position(
        position=_position(),
        cash_ratio=0.10,
        quote={"available": True, "change_available": True, "change_pct": -1.5},
        benchmark={"change_pct": -0.2},
        trading_session=True,
        quote_fresh=True,
        policy=policy,
    )

    assert state == "reduce_candidate"
    assert "25%" not in suggestion
    assert "\u964d\u4f4e\u96c6\u4e2d\u5ea6" in suggestion


def test_position_loss_is_not_mislabeled_as_path_drawdown():
    state, suggestion = service._action_for_position(
        position={
            **_position(),
            "market_value": 7000,
            "cost_value": 10000,
            "pnl": -3000,
            "pnl_ratio": -0.30,
        },
        cash_ratio=None,
        quote={},
        benchmark={},
        trading_session=False,
        quote_fresh=False,
        policy=_policy("system_default"),
    )

    assert state == "market_closed_loss_review"
    assert "\u7d2f\u8ba1\u6301\u4ed3\u4e8f\u635f 30.0%" in suggestion
    assert "\u7d2f\u8ba1\u56de\u64a4" not in suggestion

import json

from domain.policies.advisor_policy import ADVISOR_PROFILES, confirm_advisor_profile, load_advisor_policy
from domain.services import decision_monitor_service


def test_policy_loader_validates_ratios_and_target_order(tmp_path):
    path = tmp_path / "advisor_policy.json"
    path.write_text(
        json.dumps(
            {
                "profile_status": "user_confirmed",
                "single_position_prepare_ratio": 0.2,
                "single_position_reduce_target_ratio": 0.4,
                "top3_position_alert_ratio": 2,
                "minimum_cash_ratio": -1,
            }
        ),
        encoding="utf-8",
    )

    policy = load_advisor_policy(path)

    assert policy["profile_status"] == "user_confirmed"
    assert policy["single_position_reduce_target_ratio"] == 0.2
    assert policy["top3_position_alert_ratio"] == 1.0
    assert policy["minimum_cash_ratio"] == 0.0
    assert policy["loaded"] is True


def test_execution_quantity_uses_the_shared_reduction_target():
    policy = load_advisor_policy()
    policy["single_position_reduce_target_ratio"] = 0.20

    hint = decision_monitor_service._reduce_execution_hint(
        {"weight": 0.40, "volume": 1000, "source": "main", "sources": ["main"]},
        "reduce_priority",
        policy=policy,
    )

    assert hint["target_weight"] == 0.20
    assert hint["suggested_qty"] == 500
    assert "目标仓位不高于 20%" in hint["note"]


def test_cash_shortage_wording_uses_the_shared_minimum_cash_ratio():
    policy = load_advisor_policy()
    policy["minimum_cash_ratio"] = 0.10

    _, action = decision_monitor_service._action_for_position(
        position={"weight": 0.1, "pnl": 100, "source": "main"},
        cash_ratio=0.08,
        quote={"available": True, "change_available": True, "change_pct": 0.2},
        benchmark={"change_pct": 0.1},
        trading_session=True,
        quote_fresh=True,
        policy=policy,
    )

    assert "现金不足" in action


def test_explicit_profile_confirmation_writes_and_reads_back(tmp_path):
    path = tmp_path / "advisor_policy_user.json"

    policy = confirm_advisor_profile("稳健", path=path, confirmed_via="test_explicit_command")

    assert policy["profile_status"] == "user_confirmed"
    assert policy["profile_name"] == "稳健"
    assert policy["minimum_cash_ratio"] == ADVISOR_PROFILES["稳健"]["minimum_cash_ratio"]
    assert policy["confirmed_via"] == "test_explicit_command"
    assert path.exists()

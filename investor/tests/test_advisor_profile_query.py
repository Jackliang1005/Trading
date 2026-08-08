from __future__ import annotations

import json

from domain.policies.advisor_policy import load_advisor_policy
from domain.services import advisor_brief_service, assistant_menu_service, feishu_query_service


def test_profile_preview_never_writes_without_terminal_confirmation(monkeypatch, tmp_path):
    path = tmp_path / "advisor_policy_user.json"
    monkeypatch.setenv("INVESTOR_ADVISOR_POLICY_PATH", str(path))

    text = feishu_query_service.handle_feishu_query("/风险偏好 稳健")

    assert "待确认预览：稳健" in text
    assert "/风险偏好 稳健 确认" in text
    assert not path.exists()


def test_profile_confirmation_persists_and_reads_back(monkeypatch, tmp_path):
    path = tmp_path / "advisor_policy_user.json"
    monkeypatch.setenv("INVESTOR_ADVISOR_POLICY_PATH", str(path))

    text = feishu_query_service.handle_feishu_query("/风险偏好 均衡 确认")
    policy = load_advisor_policy(path)

    assert "已确认并回读：均衡" in text
    assert policy["profile_status"] == "user_confirmed"
    assert policy["profile_name"] == "均衡"
    assert json.loads(path.read_text(encoding="utf-8"))["confirmed_via"] == "feishu_explicit_command"


def test_profile_command_is_routed_before_generic_risk_query():
    assert feishu_query_service._normalize_intent("/风险偏好") == "advisor_profile"
    asks = [
        item.get("ask")
        for section in assistant_menu_service.build_assistant_menu()["sections"]
        for item in section["items"]
    ]
    assert "/风险偏好" in asks


def test_advisor_actions_invite_preview_but_never_choose_a_profile_for_the_user():
    actions = advisor_brief_service._build_actions(
        {"advisor_policy": {"profile_status": "system_default"}},
        {},
        {},
    )

    assert any("个人风险偏好尚未确认" in item["text"] for item in actions)
    assert any("明确发送末尾带“确认”的命令后才会写入" in item["text"] for item in actions)


def test_confirmed_profile_removes_onboarding_prompt():
    actions = advisor_brief_service._build_actions(
        {"advisor_policy": {"profile_status": "user_confirmed", "profile_name": "均衡"}},
        {},
        {},
    )

    assert not any("个人风险偏好尚未确认" in item["text"] for item in actions)

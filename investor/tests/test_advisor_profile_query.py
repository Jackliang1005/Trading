from __future__ import annotations

import json

from domain.policies.advisor_policy import load_advisor_policy
from domain.services import assistant_menu_service, feishu_query_service


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

import json
from datetime import datetime

from domain.services import advisor_brief_service as service
from domain.services import assistant_menu_service
from domain.services import feishu_query_service


def _write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _risk():
    return {
        "available": True,
        "as_of": "2026-08-10",
        "positions_count": 2,
        "total_market_value": 300000,
        "total_unrealized_pnl": 12000,
        "cash": 1000,
        "cash_ratio": 0.003,
        "cash_complete": False,
        "top1_ratio": 0.58,
        "top3_ratio": 0.9,
        "risk_flags": ["top1_concentration_high", "top3_concentration_high"],
    }


def _evolution():
    return {
        "ready": False,
        "total": 6,
        "minimum_total": 20,
        "minimum_strategies": 2,
        "strategies": [
            {"strategy": "technical", "verified": 3, "minimum": 5, "pending": 3},
            {"strategy": "sentiment", "verified": 3, "minimum": 5, "pending": 3},
        ],
        "maturity_rule": "新样本需走满3个真实交易日并通过价格锚点校验后才计入",
    }


def test_advisor_brief_unifies_risk_events_actions_and_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "build_risk_report", _risk)
    monkeypatch.setattr(service, "build_evolution_readiness", lambda as_of=None: _evolution())
    _write(
        tmp_path / "investor_assistant_capability_audit_latest.json",
        {
            "generated_at": "2026-08-10 09:00:00",
            "items": [{"name": "holdings_account_monitor", "status": "blocked"}],
        },
    )
    _write(
        tmp_path / "investor_closing_brief_latest.json",
        {
            "date": "2026-08-07",
            "events": {
                "top_events": [
                    {
                        "title": "U.S. Treasury yields fall as traders monitor possible Iran war deal",
                        "themes": [{"theme": "Global Macro"}, {"theme": "Geopolitics"}],
                    },
                    {
                        "title": "Oil rises as Iran's draft plan sees U.S. banned from Strait of Hormuz",
                        "themes": [{"theme": "Energy Commodities"}, {"theme": "Geopolitics"}],
                    },
                    {
                        "title": "Oil rises amid Iran restrictions in the Strait of Hormuz",
                        "themes": [{"theme": "Energy Commodities"}, {"theme": "Geopolitics"}],
                    },
                    {
                        "title": "台积电研发二维晶体管新技术",
                        "themes": [{"theme": "半导体"}],
                    },
                ]
            },
        },
    )
    _write(
        tmp_path / "investor_decision_monitor_latest.json",
        {
            "generated_at": "2026-08-10 10:30:00",
            "tracked_positions": [
                {
                    "name": "示例股份",
                    "action_level": "prepare",
                    "execution_hint": {"note": "建议减仓 100 股。"},
                }
            ],
        },
    )
    _write(
        tmp_path / "investor_intraday_outlook_20260810_1430.json",
        {
            "corrections": [
                {"slot": "09:30", "predicted": "up", "observed": "up", "result": "方向正确"},
                {"slot": "10:30", "predicted": "sideways", "observed": "up", "result": "方向接近但强度有偏差"},
            ]
        },
    )

    brief = service.build_advisor_brief(
        now=datetime(2026, 8, 10, 11, 0),
        reports_dir=tmp_path,
    )

    assert brief["overall_action_level"] == "prepare"
    assert brief["intraday"]["total"] == 2
    assert "当前最高行动层级：**准备**" in brief["text"]
    assert "单票 30.0% 预警 / 25.0% 降风险目标" in brief["text"]
    assert "双账户持仓与资金读取" in brief["text"]
    assert "可验证现金 1000.00元" in brief["text"]
    assert "伊朗局势变化牵动美国国债收益率" in brief["text"]
    assert brief["text"].count("伊朗霍尔木兹海峡限制方案推升石油供应担忧") == 1
    assert "台积电研发二维晶体管新技术" in brief["text"]
    assert "[准备] 示例股份：建议减仓 100 股" in brief["text"]
    assert "不会自动下单" in brief["text"]
    assert "画像化验真样本 6/20" in brief["text"]
    assert "技术3/5，待验真3" in brief["text"]
    assert "未达门槛前权重保持不变" in brief["text"]


def test_stale_decision_snapshot_cannot_create_prepare_action(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "build_risk_report", _risk)
    monkeypatch.setattr(service, "build_evolution_readiness", lambda as_of=None: _evolution())
    _write(
        tmp_path / "investor_decision_monitor_latest.json",
        {
            "generated_at": "2026-08-07 10:30:00",
            "tracked_positions": [{"name": "旧计划", "action_level": "prepare"}],
        },
    )

    brief = service.build_advisor_brief(
        now=datetime(2026, 8, 10, 11, 0),
        reports_dir=tmp_path,
    )

    assert brief["overall_action_level"] == "verify"
    assert not brief["decision"]["prepared"]
    assert "旧计划" not in brief["text"]
    assert "已过当前时效" in brief["text"]


def test_feishu_and_menu_expose_the_advisor_home_view(monkeypatch):
    monkeypatch.setattr(service, "build_advisor_brief", lambda: {"text": "统一投顾总览"})

    assert feishu_query_service._normalize_intent("/投顾") == "advisor_brief"
    assert feishu_query_service.handle_feishu_query("/投顾") == "统一投顾总览"
    asks = [item.get("ask") for section in assistant_menu_service.build_assistant_menu()["sections"] for item in section["items"]]
    assert "/投顾" in asks

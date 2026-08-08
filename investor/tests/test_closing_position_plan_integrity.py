from datetime import datetime
from unittest.mock import patch

from domain.services import closing_brief_service as service
from domain.services import decision_monitor_service as monitor_service


def _payload(position):
    return {
        "date": "2026-08-08",
        "generated_at": "2026-08-08 16:05:00",
        "events": {},
        "global_impact": {},
        "market_review": {"sentiment": "震荡", "indices": []},
        "risk": {
            "available": True,
            "effective_total_asset": 401741.23,
            "cash": 116.23,
            "cash_ratio": 0.0,
            "cash_complete": False,
            "top1_ratio": 0.623,
            "top3_ratio": 0.901,
            "risk_flags": ["top1_concentration_high", "top3_concentration_high"],
            "top_positions": [position],
        },
    }


def test_cross_account_closing_plan_requires_account_split_before_quantity():
    payload = _payload(
        {
            "code": "603986.SH",
            "name": "兆易创新",
            "weight": 0.623,
            "pnl": 19230,
            "source": "combined",
            "sources": ["main", "trade"],
            "volume": 600,
            "market_value": 250230,
        }
    )

    plan = service.build_decision_plan(payload)
    decision = plan["position_decisions"][0]
    text = "\n".join(plan["position_actions"])

    assert decision["action_level"] == "verify"
    assert decision["target_weight"] == 0.25
    assert decision["requires_account_split"] is True
    assert "行动 核验" in text
    assert "先逐账户核对可用股数" in text
    assert "不按合计股数生成卖出数量" in text
    assert "参考减持约" not in text


def test_single_account_closing_plan_can_show_a_snapshot_quantity_reference():
    payload = _payload(
        {
            "code": "603986.SH",
            "name": "兆易创新",
            "weight": 0.5,
            "pnl": -1000,
            "source": "main",
            "sources": ["main"],
            "volume": 400,
            "available_volume": 400,
            "available_volume_complete": True,
            "market_value": 200000,
        }
    )

    plan = service.build_decision_plan(payload)
    text = "\n".join(plan["position_actions"])

    assert plan["position_decisions"][0]["requires_account_split"] is False
    assert "参考减持约 200 股" in text
    assert "执行前核对" in text
    assert "25% 以下" in text


def test_next_session_monitor_inherits_plan_but_gates_cross_account_quantity(monkeypatch):
    position = {
        "code": "603986.SH",
        "name": "兆易创新",
        "weight": 0.623,
        "pnl": 19230,
        "source": "combined",
        "sources": ["main", "trade"],
        "volume": 600,
        "market_value": 250230,
    }
    closing_payload = {
        "available": True,
        "date": "2026-08-08",
        "generated_at": "2026-08-08 16:05:00",
        "decision_plan": {"positions": [position], "opportunities": [], "opening_triggers": []},
    }
    risk = {
        "available": True,
        "as_of": "2026-08-10",
        "cash": 0,
        "cash_complete": False,
        "total_market_value": 401625,
        "top1_ratio": 0.623,
        "top3_ratio": 0.901,
        "risk_flags": ["top1_concentration_high"],
        "top_positions": [position],
    }
    today = datetime.now().strftime("%Y%m%d")
    quotes = {
        "603986": {"available": True, "change_available": True, "price": 400, "change_pct": -3.2, "as_of": today + "103000"},
        "000300": {"available": True, "change_available": True, "price": 4500, "change_pct": 0.1, "as_of": today + "103000"},
    }
    monkeypatch.setattr(monitor_service, "_load_latest_closing_payload", lambda: closing_payload)
    monkeypatch.setattr(monitor_service, "build_risk_report", lambda: risk)
    monkeypatch.setattr(monitor_service, "_is_trading_session", lambda now: True)
    monkeypatch.setattr(monitor_service, "_fetch_live_cash", lambda: (None, False, "main unavailable"))
    monkeypatch.setattr(monitor_service, "_fetch_realtime_quotes", lambda codes: (quotes, ""))
    monkeypatch.setattr(monitor_service, "is_cn_trading_day", lambda value: (True, "test"))

    monitor = monitor_service.build_decision_monitor(slot="1030")
    tracked = monitor["tracked_positions"][0]

    assert tracked["decision_state"] == "reduce_priority"
    assert tracked["action_level"] == "verify"
    assert tracked["execution_hint"]["actionable"] is False
    assert "分账户核对可用数量" in tracked["execution_hint"]["note"]
    with patch.object(monitor_service, "build_decision_monitor", return_value=monitor):
        text = monitor_service.format_decision_monitor_text(slot="1030")
    assert "进入“核验”层级" in text
    assert "数量参考：" not in text


def test_closing_catalysts_fall_back_to_same_day_local_events():
    payload = {
        "date": "2026-08-07",
        "generated_at": "2026-08-07 16:05:00",
        "events": {
            "top_events": [
                {
                    "title": "Oil rises as Iran's draft plan restricts the Strait of Hormuz",
                    "published_at": "2026-08-07 13:27:03",
                    "severity": "P1",
                    "themes": [{"theme": "Energy Commodities"}, {"theme": "Geopolitics"}],
                },
                {
                    "title": "Oil rises amid Iran restrictions in the Strait of Hormuz",
                    "published_at": "2026-08-07 09:00:40",
                    "severity": "P2",
                    "themes": [{"theme": "Energy Commodities"}, {"theme": "Geopolitics"}],
                },
            ]
        },
        "global_impact": {"urgent_events": []},
        "risk": {"available": False},
        "longterm": {"summary": {"available": False}},
        "audit": {},
        "market_review": {"sentiment": "震荡", "indices": [], "quality_issues": []},
        "decision_plan": {"position_actions": [], "opportunities": [], "opening_triggers": []},
    }

    text = service.format_closing_brief(payload)

    assert "伊朗霍尔木兹海峡限制方案推升石油供应担忧" in text
    assert text.count("伊朗霍尔木兹海峡限制方案推升石油供应担忧") == 1
    assert "2026-08-07 没有通过新鲜度" not in text


def test_closing_brief_labels_longterm_portfolio_as_simulation():
    payload = {
        "date": "2026-08-07",
        "generated_at": "2026-08-07 16:05:00",
        "events": {},
        "global_impact": {},
        "risk": {"available": False, "by_source": {}},
        "longterm": {
            "summary": {
                "available": True,
                "nav": 176818.51,
                "cash": 89401.84,
                "cash_ratio": 0.506,
                "holdings_count": 2,
                "actions_count": 0,
                "as_of": "2026-08-07",
            }
        },
        "audit": {},
        "market_review": {"sentiment": "震荡", "indices": [], "quality_issues": []},
        "decision_plan": {"position_actions": [], "opportunities": [], "opening_triggers": []},
    }

    text = service.format_closing_brief(payload)

    assert "长线模拟盘（非实盘账户）" in text
    assert "模拟净值" in text
    assert "模拟现金" in text
    assert "不参与实盘资金判断" in text

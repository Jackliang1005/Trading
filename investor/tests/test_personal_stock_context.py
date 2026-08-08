from __future__ import annotations

import skill_api
from domain.services import feishu_query_service as query


def test_stock_overview_leads_with_personal_position_risk(monkeypatch):
    def skill_request(payload):
        action = payload["action"]
        if action == "quote":
            return {"ok": True, "quotes": [{"code": "603986", "name": "兆易创新", "price": 417.05, "as_of": "20260807161431"}]}
        if action == "technical":
            return {"ok": True, "as_of": "2026-08-07", "stance": "bearish", "ma5": 376.85, "ma20": 439.69, "rsi14": 47.41, "macd_cross": "none"}
        if action == "fundamental":
            return {"ok": True, "financial_period": "2026-03-31", "valuation": {"pe_ttm": 101.81, "pb": 11.83}, "financial_health": {"roe": 6.12, "debt_ratio": 8.13}}
        return {"ok": True, "date": "2026-08-07", "counts": {}, "title_level_sentiment": "neutral"}

    monkeypatch.setattr(query, "_skill_code_from_query", lambda text: ("603986", ""))
    monkeypatch.setattr(query, "_skill_request", skill_request)
    monkeypatch.setattr(
        query,
        "build_risk_report",
        lambda: {
            "as_of": "2026-08-08",
            "account_source_coverage_complete": False,
            "positions": [
                {
                    "code": "603986.SH",
                    "volume": 600,
                    "market_value": 250230,
                    "weight": 0.6229,
                    "pnl": -117146.14,
                    "pnl_ratio": -0.3189,
                    "stale_sources": ["main"],
                }
            ],
            "advisor_policy": {
                "single_position_alert_ratio": 0.3,
                "severe_loss_drawdown_ratio": 0.2,
            },
        },
    )

    text = query._query_skill_overview("/分析 603986")

    assert text.index("组合关联") < text.index("**行情")
    assert "当前已知持仓 600 股" in text
    assert "占已知组合 62.29%" in text
    assert "累计持仓盈亏 -11.71万（-31.89%）" in text
    assert "超过 30.00% 集中度警戒" in text
    assert "累计持仓亏损幅度超过 20.00% 深度亏损复核线" in text
    assert "个股信号必须服从组合降险优先级" in text
    assert "部分账户源处于降级状态" in text


def test_provided_code_screener_explains_each_rejection_and_preserves_strict_operator(monkeypatch):
    captured = {}

    def skill_request(payload):
        captured.update(payload)
        return {
            "ok": True,
            "scope": "provided_codes",
            "matches": [{"code": "603986", "name": "兆易创新", "financial_period": "20260331", "metrics": {"roe": 6.12, "debt_ratio": 8.13, "revenue": 100000000}}],
            "match_count": 1,
            "rejected": [{"code": "000725", "name": "京东方Ａ", "financial_period": "20260331", "reason": "conditions_not_met", "metrics": {"roe": 1.26}}],
        }

    monkeypatch.setattr(query, "_skill_request", skill_request)

    text = query._query_skill_screener("/选股 603986 000725 ROE>5")

    assert captured["conditions"]["min"] == {"roe": 5.0}
    assert captured["conditions"]["exclusive"]["min"] == ["roe"]
    assert "**未通过及原因**" in text
    assert "兆易创新（603986，财报 2026-03-31）" in text
    assert "京东方Ａ（000725，财报 2026-03-31）：ROE 1.26%（要求 > 5%）" in text
    assert "不展示内部过滤代码" not in text


def test_screener_api_honors_exclusive_minimum_without_breaking_inclusive_clients(monkeypatch):
    monkeypatch.setattr(
        skill_api,
        "_financial",
        lambda code: {"ok": True, "metrics": {"roe": 5.0 if code == "000001" else 5.01}},
    )
    monkeypatch.setattr(
        skill_api,
        "_quote",
        lambda codes: {"ok": True, "quotes": [{"code": code, "name": code} for code in codes]},
    )

    strict = skill_api._screener(
        {"codes": ["000001", "000002"], "conditions": {"min": {"roe": 5}, "exclusive": {"min": ["roe"]}}}
    )
    inclusive = skill_api._screener(
        {"codes": ["000001"], "conditions": {"min": {"roe": 5}}}
    )

    assert [item["code"] for item in strict["matches"]] == ["000002"]
    assert [item["code"] for item in strict["rejected"]] == ["000001"]
    assert [item["code"] for item in inclusive["matches"]] == ["000001"]

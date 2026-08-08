import json
from datetime import datetime

from domain.services import advisor_brief_service
from domain.services.portfolio_event_service import rank_portfolio_events


def _positions():
    return [
        {
            "code": "603986.SH",
            "name": "兆易创新",
            "weight": 0.6229,
            "stale_sources": ["main"],
        },
        {"code": "300475.SZ", "name": "香农芯创", "weight": 0.0790},
        {"code": "600584.SH", "name": "长电科技", "weight": 0.0194},
        {"code": "000725.SZ", "name": "京东方Ａ", "weight": 0.0876},
    ]


def _events():
    return [
        {
            "title": "Oil rises on Iran supply fears",
            "summary": "supply disruption restrictions",
            "score": 80,
            "themes": [{"theme": "Energy Commodities"}, {"theme": "Geopolitics"}],
            "url": "https://example.com/oil",
        },
        {
            "title": "加息预期突变，美股存储巨头跳水",
            "summary": "",
            "score": 55,
            "themes": [{"theme": "半导体"}],
            "url": "https://example.com/memory",
        },
    ]


def test_portfolio_exposure_outranks_unrelated_higher_scored_headline():
    ranked = rank_portfolio_events(_events(), _positions())

    assert ranked[0]["title"] == "加息预期突变，美股存储巨头跳水"
    assert ranked[0]["impact_tone"] == "downside_risk"
    assert ranked[0]["portfolio_relevance"] == "shared_theme"
    assert ranked[0]["portfolio_exposure_weight"] == 0.7213
    assert [item["name"] for item in ranked[0]["portfolio_positions"]] == ["兆易创新", "香农芯创", "长电科技"]
    assert all(item["match_basis"] == "shared_theme" for item in ranked[0]["portfolio_positions"])
    assert "京东方Ａ" not in [item["name"] for item in ranked[0]["portfolio_positions"]]


def test_exact_related_security_is_auditable_even_without_theme_mapping():
    event = {
        "title": "公司发布重要公告",
        "score": 40,
        "themes": [],
        "related_stocks": [{"code": "000725.SZ", "name": "京东方Ａ"}],
    }

    ranked = rank_portfolio_events([event], _positions())

    assert ranked[0]["portfolio_relevance"] == "exact_security"
    assert ranked[0]["portfolio_exact_weight"] == 0.0876
    assert ranked[0]["portfolio_positions"][0]["match_basis"] == "exact_security"


def test_advisor_brief_prioritizes_and_explains_portfolio_relevant_event(tmp_path, monkeypatch):
    risk = {
        "available": True,
        "as_of": "2026-08-08",
        "positions_count": 4,
        "total_market_value": 400000,
        "total_unrealized_pnl": 20000,
        "cash": 100,
        "cash_complete": False,
        "top1_ratio": 0.6229,
        "top3_ratio": 0.901,
        "risk_flags": ["top1_concentration_high", "stale_account_source"],
        "stale_account_sources": {"main": "2026-08-08"},
        "top_positions": _positions(),
        "advisor_policy": {},
    }
    monkeypatch.setattr(advisor_brief_service, "build_risk_report", lambda: risk)
    monkeypatch.setattr(
        advisor_brief_service,
        "build_evolution_readiness",
        lambda as_of=None: {
            "ready": False,
            "total": 0,
            "minimum_total": 20,
            "strategies": [],
            "maturity_rule": "三交易日后验真",
        },
    )
    (tmp_path / "investor_closing_brief_latest.json").write_text(
        json.dumps({"date": "2026-08-08", "events": {"top_events": _events()}}, ensure_ascii=False),
        encoding="utf-8",
    )

    brief = advisor_brief_service.build_advisor_brief(
        now=datetime(2026, 8, 8, 17, 0),
        reports_dir=tmp_path,
    )

    assert brief["events"]["events"][0]["title"] == "加息预期突变，美股存储巨头跳水"
    assert brief["events"]["events"][0]["portfolio_exposure_weight"] == 0.7213
    assert "组合主题关联：兆易创新 62.3%、香农芯创 7.9%、长电科技 1.9%，合计 72.1%；含历史回退账户" in brief["text"]
    assert any("占已知组合 72.1%" in item["text"] for item in brief["actions"])
    assert any("不按新闻标题直接交易" in item["text"] for item in brief["actions"])

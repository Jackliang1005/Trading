from domain.services import reflection_service as service


def _config():
    return {
        "min_evolution_samples": 20,
        "min_strategy_samples": 5,
        "min_evolution_strategies": 2,
    }


class _Repo:
    def __init__(self, performance):
        self.performance = performance

    def get_strategy_performance(self, start, end):
        return self.performance

    def get_checked_predictions_in_range(self, start, end):
        return []

    def get_overall_stats(self, start, end):
        return {"total": 3, "correct": 2, "win_rate": 66.7}


def _legacy_performance():
    return [
        {
            "strategy_used": "technical",
            "total": 1,
            "correct": 0,
            "win_rate": 0.0,
            "profiled_total": 0,
            "profiled_correct": 0,
            "profiled_win_rate": None,
            "evidence_profiles": None,
        },
        {
            "strategy_used": "sentiment",
            "total": 2,
            "correct": 2,
            "win_rate": 100.0,
            "profiled_total": 0,
            "profiled_correct": 0,
            "profiled_win_rate": None,
            "evidence_profiles": None,
        },
    ]


def _qualified_performance():
    return [
        {
            "strategy_used": "technical",
            "total": 12,
            "correct": 7,
            "win_rate": 58.3,
            "avg_score": 61,
            "profiled_total": 12,
            "profiled_correct": 7,
            "profiled_win_rate": 58.3,
            "profiled_avg_score": 61,
            "evidence_profiles": "technical_price_regime_v1",
        },
        {
            "strategy_used": "sentiment",
            "total": 12,
            "correct": 9,
            "win_rate": 75.0,
            "avg_score": 72,
            "profiled_total": 12,
            "profiled_correct": 9,
            "profiled_win_rate": 75.0,
            "profiled_avg_score": 72,
            "evidence_profiles": "sentiment_flow_news_v1",
        },
    ]


def test_weekly_reflection_does_not_rank_legacy_strategy_labels(monkeypatch):
    saved = []
    monkeypatch.setattr(service, "load_strategy_config", _config)
    monkeypatch.setattr(service, "get_prediction_evaluation_repository", lambda: _Repo(_legacy_performance()))
    monkeypatch.setattr(service.db, "add_reflection_report", lambda **kwargs: saved.append(kwargs))

    report = service.weekly_attribution("2026-08-08")

    assert report["strategy_performance"] == []
    assert report["strategy_evidence"]["ready"] is False
    assert not any("最佳策略" in item or "最差策略" in item for item in report["findings"])
    assert "不输出最佳/最差策略" in saved[0]["full_report"]


def test_monthly_audit_does_not_write_legacy_strategy_stats(monkeypatch):
    updates = []
    saved = []
    monkeypatch.setattr(service, "load_strategy_config", _config)
    monkeypatch.setattr(service, "get_prediction_evaluation_repository", lambda: _Repo(_legacy_performance()))
    monkeypatch.setattr(service.db, "update_strategy_stats", lambda **kwargs: updates.append(kwargs))
    monkeypatch.setattr(service.db, "add_reflection_report", lambda **kwargs: saved.append(kwargs))

    report = service.monthly_audit("2026-08-08")

    assert updates == []
    assert report["strategy_performance"] == []
    assert not any("月度最佳" in item or "月度最差" in item for item in report["findings"])
    assert "不比较策略优劣，不回写策略统计" in saved[0]["full_report"]


def test_complete_profiled_gate_allows_ranking_and_profiled_stats(monkeypatch):
    updates = []
    monkeypatch.setattr(service, "load_strategy_config", _config)
    monkeypatch.setattr(service, "get_prediction_evaluation_repository", lambda: _Repo(_qualified_performance()))
    monkeypatch.setattr(service.db, "update_strategy_stats", lambda **kwargs: updates.append(kwargs))
    monkeypatch.setattr(service.db, "add_reflection_report", lambda **kwargs: None)

    weekly = service.weekly_attribution("2026-08-08")
    monthly = service.monthly_audit("2026-08-08")

    assert weekly["strategy_evidence"]["ready"] is True
    assert any("最佳策略: sentiment" in item for item in weekly["findings"])
    assert any("最差策略: technical" in item for item in weekly["findings"])
    assert {item["name"] for item in updates} == {"technical", "sentiment"}
    assert any("月度最佳: sentiment" in item for item in monthly["findings"])
    assert all(item["total"] == 12 for item in updates)

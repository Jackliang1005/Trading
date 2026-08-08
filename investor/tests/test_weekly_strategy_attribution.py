from datetime import date

from domain.services import weekly_report_service as service


def _config():
    return {
        "min_evolution_samples": 20,
        "min_strategy_samples": 5,
        "min_evolution_strategies": 2,
    }


def _report(predictions):
    return {
        "period_start": "2026-08-02",
        "period_end": "2026-08-08",
        "generated_at": "2026-08-08 16:00:00",
        "events": {},
        "concept_momentum": {},
        "predictions": predictions,
        "intraday_predictions": {},
        "longterm": {"summary": {"available": False}},
    }


def test_legacy_strategy_labels_are_not_reported_as_strategy_performance(monkeypatch):
    monkeypatch.setattr(service, "load_strategy_config", _config)
    monkeypatch.setattr(
        service.db,
        "get_checked_predictions_in_range",
        lambda start, end: [
            {"strategy_used": "sentiment", "is_correct": 1, "evidence_profile": "", "prediction_run_id": ""},
            {"strategy_used": "technical", "is_correct": 0, "evidence_profile": "", "prediction_run_id": ""},
        ],
    )

    summary = service._summarize_predictions(date(2026, 8, 2), date(2026, 8, 8))
    text = service.format_weekly_report(_report(summary))

    assert summary["total"] == 2
    assert summary["qualified_total"] == 0
    assert summary["unqualified_total"] == 2
    assert summary["strategy_comparison_ready"] is False
    assert "整体正确率 50.0%" in text
    assert "历史或未画像化市场预测" in text
    assert "情绪 1/1" not in text
    assert "技术 0/1" not in text
    assert "分策略胜率" in text


def test_profiled_samples_below_gate_show_progress_without_win_rates(monkeypatch):
    monkeypatch.setattr(service, "load_strategy_config", _config)
    rows = [
        {
            "strategy_used": "sentiment",
            "is_correct": 1,
            "evidence_profile": "sentiment_flow_news_v1",
            "prediction_run_id": f"run-{index}",
        }
        for index in range(3)
    ]
    monkeypatch.setattr(service.db, "get_checked_predictions_in_range", lambda start, end: rows)

    summary = service._summarize_predictions(date(2026, 8, 2), date(2026, 8, 8))
    text = service.format_weekly_report(_report(summary))

    assert summary["qualified_total"] == 3
    assert summary["strategies"][1]["win_rate"] is None
    assert "本周画像化 3/20" in text
    assert "情绪3/5" in text
    assert "情绪 3/3（100.0%）" not in text
    assert "不输出分策略胜率或优劣排序" in text


def test_only_complete_profiled_gate_exposes_strategy_win_rates(monkeypatch):
    monkeypatch.setattr(service, "load_strategy_config", _config)
    rows = []
    for strategy, profile, correct_count in (
        ("technical", "technical_price_regime_v1", 6),
        ("sentiment", "sentiment_flow_news_v1", 8),
    ):
        for index in range(10):
            rows.append(
                {
                    "strategy_used": strategy,
                    "is_correct": index < correct_count,
                    "evidence_profile": profile,
                    "prediction_run_id": f"{strategy}-{index}",
                }
            )
    monkeypatch.setattr(service.db, "get_checked_predictions_in_range", lambda start, end: rows)

    summary = service._summarize_predictions(date(2026, 8, 2), date(2026, 8, 8))
    text = service.format_weekly_report(_report(summary))

    assert summary["strategy_comparison_ready"] is True
    assert "画像化分策略" in text
    assert "技术 6/10（60.0%）" in text
    assert "情绪 8/10（80.0%）" in text


def test_wrong_profile_or_missing_run_id_is_not_qualified(monkeypatch):
    monkeypatch.setattr(service, "load_strategy_config", _config)
    monkeypatch.setattr(
        service.db,
        "get_checked_predictions_in_range",
        lambda start, end: [
            {
                "strategy_used": "technical",
                "is_correct": 1,
                "evidence_profile": "technical_price_regime_v1",
                "prediction_run_id": "",
            },
            {
                "strategy_used": "technical",
                "is_correct": 1,
                "evidence_profile": "sentiment_flow_news_v1",
                "prediction_run_id": "run-wrong",
            },
        ],
    )

    summary = service._summarize_predictions(date(2026, 8, 2), date(2026, 8, 8))

    assert summary["qualified_total"] == 0
    assert summary["unqualified_total"] == 2

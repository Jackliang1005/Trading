from __future__ import annotations

from unittest.mock import patch

from domain.services import legacy_entry_service


def test_0930_prediction_returns_profiled_inventory_and_outlook():
    outlook = {"text": "📈 09:30 开盘预测\n\n- 预测结论", "prediction_direction": "sideways"}
    with patch.object(legacy_entry_service, "init_system"), patch.object(
        legacy_entry_service, "generate_predictions", return_value=[11, 12]
    ), patch.object(
        legacy_entry_service, "_profiled_predictions_for_day", return_value=2
    ), patch.object(legacy_entry_service, "build_intraday_outlook", return_value=outlook) as build:
        result = legacy_entry_service.cron_daily_predict()

    build.assert_called_once_with("0930", save=True)
    assert result["prediction_ids"] == [11, 12]
    assert result["profiled_count"] == 2
    assert result["prediction_status"] == "profiled_evidence_persisted"
    assert result["outlook"] == outlook


def test_0930_prediction_fails_loudly_when_no_profiled_evidence_was_persisted(capsys):
    outlook = {"text": "📈 09:30 开盘预测\n\n- 风险观察", "prediction_direction": "sideways"}
    with patch.object(legacy_entry_service, "init_system"), patch.object(
        legacy_entry_service, "generate_predictions", return_value=[]
    ), patch.object(
        legacy_entry_service, "_profiled_predictions_for_day", return_value=0
    ), patch.object(legacy_entry_service, "build_intraday_outlook", return_value=outlook):
        try:
            legacy_entry_service.cron_daily_predict()
        except RuntimeError as exc:
            assert "画像化预测生成门禁失败" in str(exc)
        else:
            raise AssertionError("missing profiled evidence must fail the scheduled task")

    assert "09:30 开盘预测" in capsys.readouterr().out


def test_0930_prediction_rerun_accepts_existing_profiled_rows_when_outputs_are_duplicates():
    outlook = {"text": "📈 09:30 开盘预测", "prediction_direction": "up"}
    with patch.object(legacy_entry_service, "init_system"), patch.object(
        legacy_entry_service, "generate_predictions", return_value=[]
    ), patch.object(
        legacy_entry_service, "_profiled_predictions_for_day", return_value=4
    ), patch.object(legacy_entry_service, "build_intraday_outlook", return_value=outlook):
        result = legacy_entry_service.cron_daily_predict()

    assert result["count"] == 0
    assert result["profiled_count"] == 4
    assert result["prediction_status"] == "profiled_evidence_persisted"


def test_profiled_inventory_requires_matching_profile_and_run_id():
    rows = [
        {
            "strategy_used": "technical",
            "evidence_profile": "technical_price_regime_v1",
            "prediction_run_id": "run-1",
        },
        {
            "strategy_used": "technical",
            "evidence_profile": "sentiment_flow_news_v1",
            "prediction_run_id": "run-wrong-profile",
        },
        {
            "strategy_used": "sentiment",
            "evidence_profile": "sentiment_flow_news_v1",
            "prediction_run_id": "",
        },
        {"strategy_used": "technical", "evidence_profile": "", "prediction_run_id": ""},
    ]
    with patch.object(legacy_entry_service.db, "get_predictions_in_range", return_value=rows):
        count = legacy_entry_service._profiled_predictions_for_day("2026-08-10")

    assert count == 1

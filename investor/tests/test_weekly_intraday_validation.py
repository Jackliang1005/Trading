import json
from datetime import date

from domain.services import weekly_report_service


def _write_report(path, corrections):
    path.write_text(
        json.dumps({"corrections": corrections}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_intraday_summary_uses_latest_snapshot_without_double_counting(tmp_path):
    _write_report(
        tmp_path / "investor_intraday_outlook_20260807_1030.json",
        [
            {
                "slot": "09:30",
                "predicted": "sideways",
                "observed": "sideways",
                "result": "方向正确",
            }
        ],
    )
    _write_report(
        tmp_path / "investor_intraday_outlook_20260807_1430.json",
        [
            {
                "slot": "09:30",
                "predicted": "sideways",
                "observed": "sideways",
                "result": "方向正确",
            },
            {
                "slot": "10:30",
                "predicted": "up",
                "observed": "sideways",
                "result": "方向接近但强度有偏差",
            },
        ],
    )

    summary = weekly_report_service._summarize_intraday_predictions(
        date(2026, 8, 7),
        date(2026, 8, 7),
        reports_dir=tmp_path,
    )

    assert summary["total"] == 2
    assert summary["correct"] == 1
    assert summary["close"] == 1
    assert summary["wrong"] == 0
    assert summary["exact_rate"] == 50.0
    assert summary["usable_rate"] == 100.0
    assert summary["days"] == 1
    assert all(item["source_slot"] == "1430" for item in summary["samples"])
    assert summary["attributable_to_strategy"] is False


def test_weekly_report_discloses_intraday_evidence_without_weight_claim():
    report = {
        "period_start": "2026-08-01",
        "period_end": "2026-08-07",
        "generated_at": "2026-08-08 10:00:00",
        "events": {},
        "predictions": {"total": 0, "strategies": []},
        "intraday_predictions": {
            "total": 2,
            "correct": 1,
            "close": 1,
            "wrong": 0,
            "exact_rate": 50.0,
            "usable_rate": 100.0,
            "days": 1,
            "attributable_to_strategy": False,
        },
        "longterm": {"summary": {"available": False}},
    }

    text = weekly_report_service.format_weekly_report(report)

    assert "日内方向已验证 2 次" in text
    assert "日内方向闭环：1 个交易日、2 次验证" in text
    assert "不进入权重更新" in text
    assert "策略归因样本：暂无" in text
    assert "验证样本：暂无" not in text
    assert "本周暂无完成验证的预测样本" not in text
    assert "正确率 0.0%" not in text

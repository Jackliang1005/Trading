from __future__ import annotations

from datetime import date

import db
from domain.services import feishu_query_service as query


def _evolution(*, ready: bool = False, total: int = 0):
    return {
        "ready": ready,
        "total": total,
        "minimum_total": 20,
        "minimum_strategies": 2,
        "eligible_strategies": ["technical", "sentiment"] if ready else [],
        "strategies": [
            {"strategy": "technical", "verified": total // 2, "minimum": 5, "pending": 0},
            {"strategy": "sentiment", "verified": total - total // 2, "minimum": 5, "pending": 0},
        ],
        "maturity_rule": "新样本需走满3个真实交易日并通过价格锚点校验后才计入",
    }


def test_prediction_query_isolates_legacy_unscorable_and_pending_samples(monkeypatch):
    legacy_rows = [
        {
            "strategy_used": "technical",
            "evidence_profile": "",
            "prediction_run_id": "",
            "is_correct": index < 2,
        }
        for index in range(6)
    ]
    monkeypatch.setattr(db, "get_checked_predictions_in_range", lambda start, end: legacy_rows)
    monkeypatch.setattr(
        db,
        "get_prediction_validation_activity",
        lambda activity_date: {
            "activity_processed": 24,
            "activity_evaluated": 9,
            "activity_profiled_evaluated": 0,
            "activity_legacy_evaluated": 9,
            "activity_unscorable": 15,
            "pending": 4,
        },
    )
    monkeypatch.setattr(query, "build_evolution_readiness", lambda as_of: _evolution())

    text = query._query_predictions(as_of=date(2026, 8, 8))

    assert "2026-08-02 至 2026-08-08" in text
    assert "画像化成熟样本 0/20" in text
    assert "当前尚无画像化样本入库" in text
    assert "下一交易日 09:30" in text
    assert "不再静默视为成功" in text
    assert "旧版或未画像化样本 6 条，方向正确 2 条" in text
    assert "仅作迁移审计，不代表升级后策略质量" in text
    assert "共处理 24 条：可评分 9 条（当前画像化 0、旧版或未画像化 9），隔离异常 15 条" in text
    assert "15 条因价格锚点或行情证据异常不可评分" in text
    assert "另有 4 条尚未走满验证窗口" in text
    assert "历史胜率" not in text
    assert "%" not in text


def test_prediction_query_only_shows_rate_after_shared_evolution_gate(monkeypatch):
    profiled_rows = [
        {
            "strategy_used": "technical",
            "evidence_profile": "technical_price_regime_v1",
            "prediction_run_id": f"run-{index}",
            "is_correct": index == 0,
        }
        for index in range(2)
    ]
    monkeypatch.setattr(db, "get_checked_predictions_in_range", lambda start, end: profiled_rows)
    monkeypatch.setattr(
        db,
        "get_prediction_validation_activity",
        lambda activity_date: {
            "activity_processed": 2,
            "activity_evaluated": 2,
            "activity_profiled_evaluated": 2,
            "activity_legacy_evaluated": 0,
            "activity_unscorable": 0,
            "pending": 0,
        },
    )
    monkeypatch.setattr(query, "build_evolution_readiness", lambda as_of: _evolution(ready=True, total=20))

    text = query._query_predictions(as_of=date(2026, 8, 8))

    assert "当前策略证据已达到比较门槛" in text
    assert "当前画像化样本 2 条，方向正确 1 条，正确率 50.0%" in text
    assert "旧版或未画像化样本" not in text


def test_prediction_query_with_profiled_samples_below_gate_does_not_show_rate(monkeypatch):
    monkeypatch.setattr(
        db,
        "get_checked_predictions_in_range",
        lambda start, end: [
            {
                "strategy_used": "sentiment",
                "evidence_profile": "sentiment_flow_news_v1",
                "prediction_run_id": "run-current",
                "is_correct": True,
            }
        ],
    )
    monkeypatch.setattr(
        db,
        "get_prediction_validation_activity",
        lambda activity_date: {
            "activity_processed": 1,
            "activity_evaluated": 1,
            "activity_profiled_evaluated": 1,
            "activity_legacy_evaluated": 0,
            "activity_unscorable": 0,
            "pending": 0,
        },
    )
    monkeypatch.setattr(query, "build_evolution_readiness", lambda as_of: _evolution(total=1))

    text = query._query_predictions(as_of=date(2026, 8, 8))

    assert "当前画像化样本 1 条，方向正确 1 条" in text
    assert "整体证据门槛未达成，暂不换算正确率" in text
    assert "%" not in text

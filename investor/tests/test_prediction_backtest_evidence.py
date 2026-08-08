from __future__ import annotations

import datetime as dt
import sys
import types

import pandas as pd

import data_collector
import db
from domain.services import reflection_runtime_service as runtime


def test_historical_index_dates_are_normalized_before_filtering(monkeypatch):
    index_frame = pd.DataFrame(
        [
            {"date": dt.date(2026, 8, 3), "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1},
            {"date": dt.date(2026, 8, 4), "open": 101, "high": 103, "low": 100, "close": 102, "volume": 1},
            {"date": dt.date(2026, 8, 5), "open": 102, "high": 104, "low": 101, "close": 103, "volume": 1},
        ]
    )
    fake_akshare = types.SimpleNamespace(
        stock_zh_index_daily=lambda symbol: index_frame,
        stock_zh_a_hist=lambda **kwargs: pd.DataFrame(),
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)

    rows = data_collector.fetch_historical_kline("sh000001", "2026-08-04", "2026-08-05")

    assert [row["date"] for row in rows] == ["2026-08-04", "2026-08-05"]


def test_historical_position_code_strips_exchange_suffix(monkeypatch):
    calls = []
    stock_frame = pd.DataFrame(
        [{"日期": "2026-08-05", "开盘": 10, "最高": 11, "最低": 9, "收盘": 10.5, "成交量": 1, "涨跌幅": 5}]
    )

    def stock_hist(**kwargs):
        calls.append(kwargs)
        return stock_frame

    fake_akshare = types.SimpleNamespace(
        stock_zh_index_daily=lambda symbol: pd.DataFrame(),
        stock_zh_a_hist=stock_hist,
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)

    rows = data_collector.fetch_historical_kline("603986.SH", "2026-08-05", "2026-08-05")

    assert calls[0]["symbol"] == "603986"
    assert rows[0]["close"] == 10.5


def test_tencent_index_quote_fallback_parses_canonical_codes(monkeypatch):
    payload = (
        'v_s_sh000001="1~上证指数~000001~3940.04~39.69~1.02~100~200~~0~ZS~";\n'
        'v_s_sz399001="51~深证成指~399001~14311.01~200.89~1.42~300~400~~0~ZS~";\n'
    ).encode("gbk")

    class Response:
        def read(self):
            return payload

    monkeypatch.setattr(data_collector.urllib.request, "urlopen", lambda request, timeout: Response())

    rows = data_collector.fetch_tencent_index_quotes(["sh000001", "sz399001"])

    assert [row["code"] for row in rows] == ["sh000001", "sz399001"]
    assert rows[0]["price"] == 3940.04
    assert rows[0]["source"] == "tencent-index-quote"


def test_backtest_waits_for_three_actual_trading_bars(monkeypatch):
    predictions = [
        {
            "id": 1,
            "created_at": "2026-08-05 01:30:00",
            "target": "sh000001",
            "actual_price_at_predict": 100,
            "trend_3d": "bullish",
        },
        {
            "id": 2,
            "created_at": "2026-08-06 01:30:00",
            "target": "sz399001",
            "actual_price_at_predict": 100,
            "trend_3d": "bullish",
        },
        {
            "id": 3,
            "created_at": "2026-08-05 01:30:00",
            "target": "sz399006",
            "actual_price_at_predict": 10,
            "trend_3d": "bullish",
        },
    ]
    bars = {
        "sh000001": [
            {"open": 100, "high": 102, "low": 99, "close": 101},
            {"open": 101, "high": 103, "low": 100, "close": 102},
            {"open": 102, "high": 104, "low": 101, "close": 103},
        ],
        "sz399001": [
            {"open": 100, "high": 102, "low": 99, "close": 101},
            {"open": 101, "high": 103, "low": 100, "close": 102},
        ],
        "sz399006": [
            {"open": 100, "high": 102, "low": 99, "close": 101},
            {"open": 101, "high": 103, "low": 100, "close": 102},
            {"open": 102, "high": 104, "low": 101, "close": 103},
        ],
    }
    updates = []
    evaluations = []
    unscorable = []
    monkeypatch.setattr(runtime.db, "get_unchecked_predictions", lambda before_date=None: predictions)
    monkeypatch.setattr(runtime, "fetch_historical_kline", lambda target, start, end: bars[target])
    monkeypatch.setattr(runtime, "calculate_prediction_score", lambda *args, **kwargs: 80)
    monkeypatch.setattr(runtime.db, "update_prediction_result", lambda **kwargs: updates.append(kwargs))
    monkeypatch.setattr(runtime.db, "add_prediction_evaluation", lambda **kwargs: evaluations.append(kwargs))
    monkeypatch.setattr(runtime.db, "mark_prediction_unscorable", lambda **kwargs: unscorable.append(kwargs))

    result = runtime.backtest_predictions(target_date="2026-08-07")

    assert result == {
        "date": "2026-08-07",
        "total": 3,
        "checked": 1,
        "correct": 1,
        "win_rate": 100.0,
        "deferred": 1,
        "unscorable": 1,
    }
    assert [item["pred_id"] for item in updates] == [1]
    assert [item["prediction_id"] for item in evaluations] == [1]
    assert [item["pred_id"] for item in unscorable] == [3]


def test_unscorable_prediction_is_excluded_from_strategy_metrics(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "investor.db"))
    db.init_db()
    pred_id = db.add_prediction(
        target="sh000001",
        direction="neutral",
        confidence=0.5,
        reasoning="bad source price",
        strategy_used="technical",
        model_used="test",
        actual_price=10,
        trend_3d="ranging",
    )

    db.mark_prediction_unscorable(pred_id, "anchor mismatch")

    assert db.get_unchecked_predictions() == []
    assert db.get_strategy_performance("2020-01-01", "2030-01-01") == []
    assert db.get_overall_stats("2020-01-01", "2030-01-01")["total"] == 0


def test_validation_activity_includes_earlier_runs_and_separates_unscorable(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "investor.db"))
    db.init_db()
    evaluated_id = db.add_prediction(
        target="sh000001",
        direction="up",
        confidence=0.5,
        reasoning="valid",
        strategy_used="technical",
        model_used="test",
        actual_price=100,
    )
    profiled_id = db.add_prediction(
        target="sh000001",
        direction="down",
        confidence=0.5,
        reasoning="current evidence chain",
        strategy_used="technical",
        model_used="test",
        actual_price=100,
        evidence_profile="technical_price_regime_v1",
        prediction_run_id="run-current",
    )
    unscorable_id = db.add_prediction(
        target="sz399001",
        direction="up",
        confidence=0.5,
        reasoning="bad anchor",
        strategy_used="technical",
        model_used="test",
        actual_price=1,
    )
    db.add_prediction(
        target="sz399006",
        direction="neutral",
        confidence=0.5,
        reasoning="pending",
        strategy_used="technical",
        model_used="test",
        actual_price=100,
    )
    db.update_prediction_result(evaluated_id, 101, 1.0, True, 80, "valid")
    db.update_prediction_result(profiled_id, 101, 1.0, False, 20, "wrong")
    db.mark_prediction_unscorable(unscorable_id, "anchor mismatch")

    activity = db.get_prediction_validation_activity(dt.date.today().isoformat())

    assert activity["activity_processed"] == 3
    assert activity["activity_evaluated"] == 2
    assert activity["activity_correct"] == 1
    assert activity["activity_win_rate"] == 50.0
    assert activity["activity_profiled_evaluated"] == 1
    assert activity["activity_profiled_correct"] == 0
    assert activity["activity_profiled_win_rate"] == 0.0
    assert activity["activity_legacy_evaluated"] == 1
    assert activity["activity_unscorable"] == 1
    assert activity["pending"] == 1


def test_reflection_uses_generation_day_validation_activity_not_only_latest_run():
    text = runtime.format_reflection_push_text(
        {},
        "",
        {
            "checked": 0,
            "activity_evaluated": 3,
            "activity_correct": 2,
            "activity_win_rate": 66.6667,
            "activity_profiled_evaluated": 0,
            "activity_legacy_evaluated": 3,
            "activity_unscorable": 4,
            "pending": 2,
        },
        "2026-08-08 20:30:00",
        "2026-08-08",
    )

    assert "\u751f\u6210\u65e5\u7d2f\u8ba1\u9a8c\u8bc1 3 \u6761" in text
    assert "\u6b63\u786e 2 \u6761" in text
    assert "66.7%" in text
    assert "\u4e0a\u8ff0 3 \u6761\u5747\u4e3a\u65e7\u7248\u6216\u672a\u753b\u50cf\u5316\u6837\u672c" in text
    assert "\u4e0d\u4ee3\u8868\u5347\u7ea7\u540e\u7b56\u7565\u8d28\u91cf" in text
    assert "4 \u6761\u56e0\u4ef7\u683c\u951a\u70b9\u6216\u884c\u60c5\u8bc1\u636e\u4e0d\u4e00\u81f4\u800c\u4e0d\u53ef\u8bc4\u5206" in text
    assert "2 \u6761\u5c1a\u672a\u8d70\u6ee1\u9a8c\u8bc1\u7a97\u53e3" in text

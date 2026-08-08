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

from datetime import datetime

from domain.services import intraday_outlook_service as service


def _monitor(as_of="20260810093000"):
    return {
        "calendar_open": True,
        "trading_session": True,
        "benchmarks": {
            "000001.SH": {"available": True, "change_available": True, "change_pct": 1.2, "as_of": as_of},
            "399001.SZ": {"available": True, "change_available": True, "change_pct": 1.0, "as_of": as_of},
            "399006.SZ": {"available": True, "change_available": True, "change_pct": 0.9, "as_of": as_of},
            "000300.SH": {"available": True, "change_available": True, "change_pct": 0.8, "as_of": as_of},
        },
        "tracked_positions": [],
        "risk_flags": [],
    }


def test_saved_forecast_is_strategy_attributed_and_idempotent(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "build_decision_monitor", lambda slot: _monitor())
    monkeypatch.setattr(service.db, "add_prediction", lambda **kwargs: calls.append(kwargs) or 73)

    current = datetime(2026, 8, 10, 9, 30)
    first = service.build_intraday_outlook("0930", now=current, reports_dir=tmp_path, save=True)
    second = service.build_intraday_outlook("0930", now=current, reports_dir=tmp_path, save=True)

    assert len(calls) == 1
    assert calls[0]["strategy_used"] == "technical"
    assert calls[0]["timeframe"] == "intraday"
    assert first["prediction_record"]["id"] == 73
    assert second["prediction_record"]["id"] == 73
    assert first["prediction_attribution"]["basis"]
    assert "预测归因：技术面" in first["text"]


def test_preview_never_writes_prediction_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "build_decision_monitor", lambda slot: _monitor())
    monkeypatch.setattr(
        service.db,
        "add_prediction",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("preview must not write")),
    )
    monkeypatch.setattr(
        service.db,
        "update_prediction_result",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("preview must not write")),
    )

    report = service.build_intraday_outlook(
        "0930",
        now=datetime(2026, 8, 10, 9, 30),
        reports_dir=tmp_path,
        save=False,
    )

    assert report["prediction_attribution"]["strategy_used"] == "technical"
    assert "prediction_record" not in report


def test_final_correction_updates_the_original_prediction(tmp_path, monkeypatch):
    updates = []
    monkeypatch.setattr(service, "build_decision_monitor", lambda slot: _monitor("20260810143000"))
    monkeypatch.setattr(service.db, "update_prediction_result", lambda *args, **kwargs: updates.append((args, kwargs)))
    monkeypatch.setattr(service.db, "add_prediction", lambda **kwargs: 99)
    service._snapshot_path("2026-08-10", "1030", tmp_path).write_text(
        '{"prediction_direction":"up","prediction_record":{"id":88,"strategy_used":"technical"}}',
        encoding="utf-8",
    )

    report = service.build_intraday_outlook(
        "1430",
        now=datetime(2026, 8, 10, 14, 30),
        reports_dir=tmp_path,
        save=True,
    )

    assert len(updates) == 1
    assert updates[0][0][0] == 88
    assert report["corrections"][0]["prediction_id"] == 88
    assert report["corrections"][0]["strategy_used"] == "technical"
    assert "归因 technical" in report["text"]

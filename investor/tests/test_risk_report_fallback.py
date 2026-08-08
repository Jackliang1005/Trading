from domain.services import risk_report_service as service


def _snapshot(snapshot_id, as_of, data):
    return {
        "id": snapshot_id,
        "as_of_date": as_of,
        "created_at": f"{as_of} 10:00:00",
        "data": data,
    }


def test_verified_empty_account_is_not_mistaken_for_missing_data():
    item = _snapshot(1, "2026-08-08", {"qmt_account": {"account_id": "verified-empty"}, "qmt_positions": []})
    assert service._snapshot_is_usable(item) is True


def test_risk_report_uses_recent_nonempty_snapshot_instead_of_fake_zero(monkeypatch):
    empty = _snapshot(2, "2026-08-08", {"qmt_account": {}, "qmt_positions": []})
    fallback = _snapshot(
        1,
        "2026-08-07",
        {
            "qmt_account": {"account_id": "A1", "total_asset": 1000, "cash": 400},
            "qmt_positions": [
                {"stock_code": "600000.SH", "stock_name": "测试银行", "volume": 10, "market_value": 600, "float_profit": 20, "_source": "main"}
            ],
        },
    )
    monkeypatch.setattr(service, "_init_db_quietly", lambda: None)
    monkeypatch.setattr(service.db, "get_latest_portfolio_snapshot", lambda account_scope="combined": empty)
    monkeypatch.setattr(service, "_recent_combined_snapshots", lambda limit=50: [empty, fallback])
    monkeypatch.setattr(service, "_snapshot_age_days", lambda value: 1)

    report = service.build_risk_report()

    assert report["available"] is True
    assert report["fallback_snapshot"] is True
    assert report["positions_count"] == 1
    assert "不代表当前实时持仓" in report["text"]
    assert "当前 0 只持仓" not in report["text"]


def test_risk_report_fails_closed_when_no_snapshot_is_usable(monkeypatch):
    empty = _snapshot(2, "2026-08-08", {"qmt_account": {}, "qmt_positions": []})
    monkeypatch.setattr(service, "_init_db_quietly", lambda: None)
    monkeypatch.setattr(service.db, "get_latest_portfolio_snapshot", lambda account_scope="combined": empty)
    monkeypatch.setattr(service, "_recent_combined_snapshots", lambda limit=50: [empty])

    report = service.build_risk_report()

    assert report["available"] is False
    assert "不会把缺失值写成零持仓" in report["text"]


def test_account_sentinel_is_excluded_from_cash_and_concentration():
    metrics = service._account_metrics(
        {
            "qmt_trading_summary": {
                "accounts": {
                    "main": {"cash": 99_999_999_999.0, "total_asset": 100_000_285_200.0},
                    "trade": {"cash": 116.23, "total_asset": 116_540.23},
                }
            }
        },
        total_position_value=359_400.0,
    )

    assert metrics["cash"] == 116.23
    assert metrics["cash_complete"] is False
    assert metrics["effective_total"] < 1_000_000
    assert metrics["invalid_sources"] == ["main"]

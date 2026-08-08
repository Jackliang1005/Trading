from domain.services import feishu_query_service as service


def test_nontrading_snapshot_keeps_current_portfolio_and_latest_trading_day_trades():
    current = {
        "as_of_date": "2026-08-08",
        "positions_count": 5,
        "total_unrealized_pnl": -155991.15,
        "positions": [{"code": "603986.SH", "pnl": -117146.14}],
        "today_trades": [],
        "prediction_validation": {"activity_date": "2026-08-08", "activity_evaluated": 9},
    }
    trading = {
        "as_of_date": "2026-08-07",
        "positions_count": 6,
        "total_unrealized_pnl": 18071.70,
        "today_trades": [
            {"stock_code": "513050.SH", "traded_volume": 32700, "traded_price": 1.161, "order_type": 23}
        ],
    }

    merged = service._compose_reflection_summary(current, trading, "2026-08-07")

    assert merged["as_of_date"] == "2026-08-08"
    assert merged["positions_count"] == 5
    assert merged["total_unrealized_pnl"] == -155991.15
    assert merged["today_trades"] == trading["today_trades"]
    assert merged["portfolio_as_of"] == "2026-08-08"
    assert merged["trading_review_date"] == "2026-08-07"
    assert merged["validation_run_date"] == "2026-08-08"

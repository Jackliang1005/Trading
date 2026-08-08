from domain.services import reflection_runtime_service as service


def _fills():
    return [
        {"stock_code": "513050.SH", "order_type": 23, "traded_volume": 32700, "traded_price": 1.161},
        {"stock_code": "513050.SH", "order_type": 23, "traded_volume": 33400, "traded_price": 1.159},
        {"stock_code": "588940.SH", "order_type": 24, "traded_volume": 5000, "traded_price": 0.898},
        {"stock_code": "588940.SH", "order_type": 24, "traded_volume": 38100, "traded_price": 0.898},
    ]


def test_partial_fills_are_aggregated_by_code_and_direction():
    groups = service._aggregate_trades(_fills())

    assert len(groups) == 2
    assert groups[0]["code"] == "513050.SH"
    assert groups[0]["side"] == "buy"
    assert groups[0]["volume"] == 66100
    assert groups[0]["fills"] == 2
    assert round(groups[0]["avg_price"], 3) == 1.160
    assert groups[1]["code"] == "588940.SH"
    assert groups[1]["volume"] == 43100
    assert groups[1]["fills"] == 2


def test_reflection_reconciles_trade_groups_with_latest_known_positions():
    summary = {
        "as_of_date": "2026-08-08",
        "portfolio_as_of": "2026-08-08",
        "trading_review_date": "2026-08-07",
        "validation_run_date": "2026-08-08",
        "today_trades": _fills(),
        "positions": [
            {
                "code": "513050.SH",
                "name": "示例ETF",
                "volume": 66100,
                "market_value": 76676,
                "cost_value": 76685.3,
                "pnl": -9.3,
            }
        ],
        "position_coverage_complete": True,
        "account_source_coverage_complete": False,
    }

    text = service.format_reflection_push_text(summary, "", {}, "2026-08-08 18:00:00", "2026-08-08")

    assert "汇总 买入 **513050.SH**｜66100 股" in text
    assert "汇总 卖出 **588940.SH**｜43100 股" in text
    assert "成交额口径资金净流出" in text
    assert "最新已知持仓 66100 股；仅作数量对照" in text
    assert "最新已知持仓未显示该标的，与卖出或清仓方向相符" in text
    assert "不据此认定实时清仓" in text

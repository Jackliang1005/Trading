from domain.services import feishu_query_service


def _position(position_profit=-89543.53):
    return {
        "stock_code": "603986.SH",
        "volume": 500,
        "avg_price": 596.13706,
        "last_price": 417.05,
        "market_value": 208525.0,
        "position_profit": position_profit,
        "m_dFloatProfit": 16025.0,
    }


def test_feishu_position_summary_uses_cumulative_position_profit_not_daily_float():
    headline, lines = feishu_query_service._summarize_positions([_position()])

    assert "持仓盈亏 -89,543.53 元" in headline
    assert "持仓盈亏 -89,543.53 元" in lines[0]
    assert "+16,025.00" not in headline + lines[0]
    assert "浮动盈亏" not in headline + lines[0]


def test_feishu_position_summary_discloses_cost_basis_conflict():
    headline, lines = feishu_query_service._summarize_positions([_position(position_profit=-1000)])

    assert "持仓盈亏 -89,543.53 元" in headline
    assert "盈亏字段待核验" in lines[0]


def test_holdings_command_routes_to_live_positions_instead_of_help():
    assert feishu_query_service._normalize_intent("/持仓") == "positions"

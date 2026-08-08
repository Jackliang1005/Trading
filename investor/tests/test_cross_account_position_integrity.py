import qmt_client
from domain.services import risk_report_service as risk_service


class _PositionsClient:
    def __init__(self, rows):
        self.rows = rows

    def get_positions(self, **kwargs):
        return [dict(item) for item in self.rows]


class _FailingPositionsClient:
    def get_positions(self, **kwargs):
        raise TimeoutError("account gateway timeout")


def test_dual_account_merge_preserves_the_same_security_in_each_account():
    manager = qmt_client.QMTManager.__new__(qmt_client.QMTManager)
    manager.main = _PositionsClient([{"stock_code": "603986.SH", "volume": 500, "market_value": 208525}])
    manager.trade = _PositionsClient([{"stock_code": "603986.SH", "volume": 100, "market_value": 41705}])
    manager._account_id = ""
    manager._account_type = "STOCK"

    rows = manager.get_all_positions()

    assert len(rows) == 2
    assert sum(item["volume"] for item in rows) == 600
    assert {item["_source"] for item in rows} == {"main", "trade"}
    assert {item["_position_identity"] for item in rows} == {"main:603986.SH", "trade:603986.SH"}


def test_one_failed_account_does_not_hide_the_other_accounts_positions():
    manager = qmt_client.QMTManager.__new__(qmt_client.QMTManager)
    manager.main = _FailingPositionsClient()
    manager.trade = _PositionsClient([{"stock_code": "603986.SH", "volume": 100, "market_value": 41705}])
    manager._account_id = ""
    manager._account_type = "STOCK"
    manager.last_errors = {}

    rows = manager.get_all_positions()

    assert len(rows) == 1
    assert rows[0]["_source"] == "trade"
    assert "main.positions" in manager.last_errors


def test_missing_expected_account_is_explicitly_incomplete():
    metrics = risk_service._account_metrics(
        {
            "qmt_trading_summary": {
                "expected_sources": ["main", "trade"],
                "accounts": {"trade": {"cash": 1000, "total_asset": 11000, "market_value": 10000}},
            }
        },
        total_position_value=10000,
    )

    assert metrics["source_coverage_complete"] is False
    assert metrics["missing_sources"] == ["main"]
    assert metrics["cash_complete"] is False


def test_risk_report_detects_a_cross_account_position_gap(monkeypatch):
    snapshot = {
        "id": 127,
        "created_at": "2026-08-08 08:00:00",
        "as_of_date": "2026-08-08",
        "data": {
            "qmt_trading_summary": {
                "accounts": {
                    "main": {
                        "cash": 99_999_999_999,
                        "total_asset": 100_000_285_200,
                        "market_value": 285201,
                    },
                    "trade": {"cash": 116.23, "total_asset": 116540.23, "market_value": 116424},
                }
            },
            "qmt_positions": [
                {"_source": "main", "stock_code": "513050.SH", "volume": 66100, "market_value": 76676},
                {"_source": "main", "stock_code": "603986.SH", "volume": 500, "market_value": 208525},
                {"_source": "trade", "stock_code": "600584.SH", "volume": 100, "market_value": 7775},
                {"_source": "trade", "stock_code": "000725.SZ", "volume": 5800, "market_value": 35206},
                {"_source": "trade", "stock_code": "300475.SZ", "volume": 200, "market_value": 31738},
            ],
        },
    }
    monkeypatch.setattr(risk_service, "_init_db_quietly", lambda: None)
    monkeypatch.setattr(risk_service, "_select_risk_snapshot", lambda: (snapshot, False))

    report = risk_service.build_risk_report()

    assert report["position_coverage_complete"] is False
    assert report["position_market_value_gap"] == 41705
    assert report["reported_account_market_value"] == 401625
    assert "position_coverage_incomplete" in report["risk_flags"]
    assert report["effective_total_asset"] == 401741.23
    assert "持仓明细覆盖不完整" in report["text"]
    assert "不视为完整组合比例" in report["text"]
    assert "可能存在跨账户同代码被合并" in report["text"]


def test_risk_concentration_aggregates_the_same_security_across_accounts(monkeypatch):
    snapshot = {
        "created_at": "2026-08-08 08:00:00",
        "as_of_date": "2026-08-08",
        "data": {
            "qmt_trading_summary": {
                "accounts": {
                    "main": {"cash": 99_999_999_999, "total_asset": 100_000_285_200, "market_value": 285201},
                    "trade": {"cash": 116.23, "total_asset": 116540.23, "market_value": 116424},
                }
            },
            "qmt_positions": [
                {"_source": "main", "stock_code": "603986.SH", "volume": 500, "market_value": 208525, "unrealized_pnl": 16025},
                {"_source": "main", "stock_code": "513050.SH", "volume": 66100, "market_value": 76676},
                {"_source": "trade", "stock_code": "603986.SH", "volume": 100, "market_value": 41705, "unrealized_pnl": 3200},
                {"_source": "trade", "stock_code": "600584.SH", "volume": 100, "market_value": 7775},
                {"_source": "trade", "stock_code": "000725.SZ", "volume": 5800, "market_value": 35206},
                {"_source": "trade", "stock_code": "300475.SZ", "volume": 200, "market_value": 31738},
            ],
        },
    }
    monkeypatch.setattr(risk_service, "_init_db_quietly", lambda: None)
    monkeypatch.setattr(risk_service, "_select_risk_snapshot", lambda: (snapshot, False))

    report = risk_service.build_risk_report()

    assert report["position_coverage_complete"] is True
    assert report["position_rows_count"] == 6
    assert report["positions_count"] == 5
    assert report["top_positions"][0]["code"] == "603986.SH"
    assert report["top_positions"][0]["volume"] == 600
    assert report["top_positions"][0]["market_value"] == 250230
    assert report["top_positions"][0]["sources"] == ["main", "trade"]
    assert report["top1_ratio"] > 0.62
    assert "国金、东莞" in report["text"]


def test_partial_live_snapshot_can_use_explicitly_stale_per_account_fallback(monkeypatch):
    latest = {
        "id": 200,
        "created_at": "2026-08-10 08:00:00",
        "as_of_date": "2026-08-10",
        "metadata": {},
        "data": {
            "qmt_trading_summary": {
                "expected_sources": ["main", "trade"],
                "source_errors": {"main.asset": "timeout", "main.positions": "timeout"},
                "accounts": {"trade": {"cash": 116.23, "total_asset": 116540.23, "market_value": 116424}},
            },
            "qmt_positions": [
                {"_source": "trade", "stock_code": "603986.SH", "volume": 100, "market_value": 41705},
                {"_source": "trade", "stock_code": "600584.SH", "volume": 100, "market_value": 7775},
                {"_source": "trade", "stock_code": "000725.SZ", "volume": 5800, "market_value": 35206},
                {"_source": "trade", "stock_code": "300475.SZ", "volume": 200, "market_value": 31738},
            ],
        },
    }
    prior = {
        "id": 199,
        "created_at": "2026-08-07 23:30:00",
        "as_of_date": "2026-08-08",
        "metadata": {},
        "data": {
            "qmt_trading_summary": {
                "accounts": {"main": {"cash": 99999999999, "total_asset": 100000285200, "market_value": 285201}}
            },
            "qmt_positions": [
                {"_source": "main", "stock_code": "603986.SH", "volume": 500, "market_value": 208525},
                {"_source": "main", "stock_code": "513050.SH", "volume": 66100, "market_value": 76676},
            ],
        },
    }

    enriched = risk_service._enrich_partial_snapshot(latest, [latest, prior])
    monkeypatch.setattr(risk_service, "_init_db_quietly", lambda: None)
    monkeypatch.setattr(risk_service, "_select_risk_snapshot", lambda: (enriched, False))
    report = risk_service.build_risk_report()

    assert enriched["data"]["qmt_trading_summary"]["stale_sources"] == {"main": "2026-08-08"}
    assert len(enriched["data"]["qmt_positions"]) == 6
    assert report["position_coverage_complete"] is True
    assert report["stale_account_sources"] == {"main": "2026-08-08"}
    assert report["top_positions"][0]["market_value"] == 250230
    assert "部分账户持仓使用历史快照" in report["text"]
    assert "不冒充实时持仓" in report["text"]

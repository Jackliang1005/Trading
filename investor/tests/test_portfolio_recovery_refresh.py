from __future__ import annotations

from datetime import datetime

from domain.services import portfolio_refresh_service as service


def _summary(errors=None, accounts=None):
    return {
        "expected_sources": ["main", "trade"],
        "source_errors": errors or {},
        "accounts": accounts if accounts is not None else {"main": {"total_asset": 10}, "trade": {"total_asset": 20}},
        "positions": [{"stock_code": "603986.SH", "_source": "main"}],
        "today_orders": [],
        "today_trades": [],
        "positions_count": 1,
    }


def test_verified_dual_account_refresh_saves_recovery_snapshot(monkeypatch):
    saved = []
    monkeypatch.setattr(service, "fetch_qmt_trading_summary", lambda: _summary())
    monkeypatch.setattr(service.db, "save_portfolio_snapshot", lambda *args, **kwargs: saved.append((args, kwargs)) or 42)

    result = service.refresh_verified_portfolio_snapshot(as_of=datetime(2026, 8, 10, 10, 15))

    assert result["verified"] is True
    assert result["saved"] is True
    assert result["snapshot_id"] == 42
    assert result["verified_sources"] == ["main", "trade"]
    assert saved[0][0][0] == "combined"
    assert saved[0][1]["source_snapshot_type"] == "portfolio_recovery"


def test_incomplete_recovery_does_not_replace_last_safe_snapshot(monkeypatch):
    monkeypatch.setattr(
        service,
        "fetch_qmt_trading_summary",
        lambda: _summary(errors={"main.positions": "offline"}, accounts={"trade": {"total_asset": 20}}),
    )
    monkeypatch.setattr(
        service.db,
        "save_portfolio_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not save partial recovery")),
    )

    result = service.refresh_verified_portfolio_snapshot(as_of=datetime(2026, 8, 10, 10, 15))

    assert result["verified"] is False
    assert result["saved"] is False
    assert result["reason"] == "account_sources_incomplete"
    assert "main" in result["missing_accounts"]

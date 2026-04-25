#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Unified dual-server QMT client.

QMTClient  — wraps HTTP calls to a single QMT2HTTP server.
QMTManager — manages two clients (main + trade) with unified access.

Environment variables
---------------------
  QMT2HTTP_MAIN_URL      Main server (market data + trading)
  QMT2HTTP_TRADE_URL     Trade-only server
  QMT2HTTP_DONGGUAN_BASE_URL Alias of trade-only server URL
  QMT2HTTP_DISABLE_TRADE Disable trade-only server client (1/true/on/yes)
  QMT2HTTP_API_TOKEN     Shared API token
  QMT2HTTP_BASE_URL      Legacy single-server fallback
  QMT_ACCOUNT_ID         Account ID
  QMT_ACCOUNT_TYPE       Account type (default: STOCK)
"""

from __future__ import annotations

import json
import os
import threading
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


DEFAULT_MAIN_URL = "http://39.105.48.176:8085"
DEFAULT_TRADE_URL = "http://150.158.31.115:8085"
DISABLE_TRUE_VALUES = {"1", "true", "on", "yes", "disable", "disabled", "none", "off"}


# ---------------------------------------------------------------------------
# QMTClient  (single server)
# ---------------------------------------------------------------------------

class QMTClient:
    """HTTP client for a single QMT2HTTP server."""

    def __init__(
        self,
        base_url: str,
        api_token: str = "",
        timeout: int = 20,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token.strip()
        self.timeout = timeout

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if self.api_token:
            h["Authorization"] = f"Bearer {self.api_token}"
            h["X-API-Token"] = self.api_token
        if extra:
            h.update(extra)
        return h

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=body, headers=self._headers(headers), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                result: Dict[str, Any] = json.loads(text) if text else {}
                return result
        except Exception as e:
            raise RuntimeError(f"QMT request failed [{self.base_url}{path}]: {e}") from e

    # -- RPC helpers ----------------------------------------------------------

    def rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        payload = json.dumps(
            {"method": method, "params": params or {}}, ensure_ascii=False
        ).encode("utf-8")
        result = self._request(
            "POST", "/rpc/data_fetcher",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        if not result.get("success", False):
            raise RuntimeError(result.get("message") or f"RPC failed: {method}")
        return result.get("data")

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        query = urllib.parse.urlencode(
            {k: v for k, v in (params or {}).items() if v is not None and v != ""},
            doseq=True,
        )
        final_path = path if not query else f"{path}?{query}"
        result = self._request("GET", final_path)
        if not result.get("success", False):
            raise RuntimeError(result.get("message") or f"GET failed: {path}")
        return result.get("data")

    # -- health ---------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/health")

    # -- account / positions / orders / trades --------------------------------

    def get_account_asset(self, account_id: str = "", account_type: str = "STOCK") -> Dict[str, Any]:
        params = {"account_id": account_id, "account_type": account_type}
        return self.get("/api/stock/asset", params) or {}

    def get_positions(self, account_id: str = "", account_type: str = "STOCK") -> List[Dict[str, Any]]:
        params = {"account_id": account_id, "account_type": account_type}
        return self.get("/api/stock/positions", params) or []

    def get_orders(self, account_id: str = "", account_type: str = "STOCK") -> List[Dict[str, Any]]:
        params = {"account_id": account_id, "account_type": account_type}
        return self.get("/api/stock/orders", params) or []

    def get_trades(self, account_id: str = "", account_type: str = "STOCK") -> List[Dict[str, Any]]:
        params = {"account_id": account_id, "account_type": account_type}
        return self.get("/api/stock/trades", params) or []

    def get_trade_records(self, record_type: str, account_id: str = "", account_type: str = "STOCK") -> Any:
        params = {"record_type": record_type, "account_id": account_id, "account_type": account_type}
        return self.get("/api/trade/records", params)

    # -- market data ----------------------------------------------------------

    def get_realtime_data(self, code: str) -> Dict[str, Any]:
        return self.rpc("get_realtime_data", {"code": code}) or {}

    def get_batch_realtime_data(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        return self.rpc("get_batch_realtime_data", {"codes": codes}) or {}

    def get_current_price(self, code: str) -> float:
        return self.rpc("get_current_price", {"code": code}) or 0.0

    def get_stock_sectors(self, code: str) -> List[str]:
        return self.rpc("get_stock_sectors", {"code": code}) or []


# ---------------------------------------------------------------------------
# QMTManager  (dual-server)
# ---------------------------------------------------------------------------

class QMTManager:
    """Manages two QMT2HTTP servers: main (market+trading) and trade (trading only).

    Defaults to dual-server mode with built-in MAIN/TRADE URLs.
    Set QMT2HTTP_DISABLE_TRADE=1 to force single-server mode.
    """

    def __init__(
        self,
        main_url: Optional[str] = None,
        trade_url: Optional[str] = None,
        api_token: Optional[str] = None,
        timeout: int = 20,
    ):
        token = api_token or _env("QMT2HTTP_API_TOKEN", "998811")

        main_url = main_url or _env("QMT2HTTP_MAIN_URL")
        trade_url = trade_url or _env("QMT2HTTP_TRADE_URL") or _env("QMT2HTTP_DONGGUAN_BASE_URL")
        trade_disabled = _env("QMT2HTTP_DISABLE_TRADE").lower() in DISABLE_TRUE_VALUES

        # Fallback to legacy single-server config
        if not main_url:
            main_url = _env("QMT2HTTP_BASE_URL", DEFAULT_MAIN_URL)
        if not trade_url and not trade_disabled:
            trade_url = DEFAULT_TRADE_URL

        self.main = QMTClient(main_url, api_token=token, timeout=timeout)
        self.trade = QMTClient(trade_url, api_token=token, timeout=timeout) if (trade_url and not trade_disabled) else None
        self.dual_mode = self.trade is not None
        self._account_id = _env("QMT_ACCOUNT_ID")
        self._account_type = _env("QMT_ACCOUNT_TYPE", "STOCK")

    # -- account helpers ------------------------------------------------------

    def _account_params(self) -> Dict[str, str]:
        return {"account_id": self._account_id, "account_type": self._account_type}

    # -- unified access -------------------------------------------------------

    def get_all_positions(self) -> List[Dict[str, Any]]:
        """Merge positions from both servers, deduplicated by stock_code."""
        seen: set = set()
        merged: List[Dict[str, Any]] = []
        for pos in self.main.get_positions(**self._account_params()):
            code = pos.get("stock_code", "")
            if code and code not in seen:
                seen.add(code)
                pos["_source"] = "main"
                merged.append(pos)
        if self.trade:
            for pos in self.trade.get_positions(**self._account_params()):
                code = pos.get("stock_code", "")
                if code and code not in seen:
                    seen.add(code)
                    pos["_source"] = "trade"
                    merged.append(pos)
        return merged

    def get_all_accounts(self) -> Dict[str, Dict[str, Any]]:
        """Return account info from both servers keyed by source label."""
        result: Dict[str, Dict[str, Any]] = {}
        result["main"] = self.main.get_account_asset(**self._account_params())
        if self.trade:
            result["trade"] = self.trade.get_account_asset(**self._account_params())
        return result

    def get_all_orders(self) -> List[Dict[str, Any]]:
        """Return today's orders from both servers."""
        orders = self.main.get_orders(**self._account_params())
        for o in orders:
            o["_source"] = "main"
        if self.trade:
            trade_orders = self.trade.get_orders(**self._account_params())
            for o in trade_orders:
                o["_source"] = "trade"
            orders.extend(trade_orders)
        return orders

    def get_all_trades(self) -> List[Dict[str, Any]]:
        """Return today's trades from both servers."""
        trades = self.main.get_trades(**self._account_params())
        for t in trades:
            t["_source"] = "main"
        if self.trade:
            trade_trades = self.trade.get_trades(**self._account_params())
            for t in trade_trades:
                t["_source"] = "trade"
            trades.extend(trade_trades)
        return trades

    def get_market_data(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch realtime market data from the main server only."""
        return self.main.get_batch_realtime_data(codes)

    def health(self) -> Dict[str, Any]:
        """Health check of both servers."""
        result = {"main": self.main.health()}
        if self.trade:
            result["trade"] = self.trade.health()
        return result

    # -- trading summary ------------------------------------------------------

    def get_trading_summary(self) -> Dict[str, Any]:
        """Compute a combined P&L / trade summary across both servers."""
        accounts = self.get_all_accounts()
        positions = self.get_all_positions()
        trades = self.get_all_trades()
        orders = self.get_all_orders()

        # P&L from positions
        total_market_value = 0.0
        total_pnl = 0.0
        for pos in positions:
            mv = float(pos.get("market_value", 0) or 0)
            cost = float(pos.get("cost_value", 0) or 0)
            total_market_value += mv
            total_pnl += mv - cost

        # Trade direction breakdown
        buy_count = 0
        sell_count = 0
        buy_volume = 0
        sell_volume = 0
        buy_amount = 0.0
        sell_amount = 0.0
        for t in trades:
            otype = t.get("order_type", 0)
            vol = int(t.get("trade_volume", 0) or t.get("deal_volume", 0) or 0)
            amt = float(t.get("trade_amount", 0) or t.get("deal_amount", 0) or 0)
            if otype in (23, "buy", "BUY"):
                buy_count += 1
                buy_volume += vol
                buy_amount += amt
            else:
                sell_count += 1
                sell_volume += vol
                sell_amount += amt

        return {
            "accounts": accounts,
            "positions": positions,
            "positions_count": len(positions),
            "total_market_value": round(total_market_value, 2),
            "total_unrealized_pnl": round(total_pnl, 2),
            "today_trades": trades,
            "today_trade_count": len(trades),
            "today_orders": orders,
            "today_order_count": len(orders),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "buy_volume": buy_volume,
            "sell_volume": sell_volume,
            "buy_amount": round(buy_amount, 2),
            "sell_amount": round(sell_amount, 2),
            "net_amount": round(buy_amount - sell_amount, 2),
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_manager: Optional[QMTManager] = None
_manager_lock = threading.Lock()


def get_qmt_manager() -> QMTManager:
    """Get or create the global QMTManager singleton."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = QMTManager()
    return _manager


def reset_qmt_manager() -> None:
    """Reset the global singleton (useful for testing)."""
    global _manager
    with _manager_lock:
        _manager = None

import os
from typing import Dict, Optional

import requests


def normalize_stock_code(code: str, code_format: str = "plain") -> str:
    text = str(code or "").strip().upper()
    if not text:
        raise ValueError("stock_code 不能为空")
    if code_format == "plain":
        return text.split(".", 1)[0]
    if "." in text:
        return text
    if text.startswith(("60", "68", "90", "50", "51", "52", "56", "58", "11")):
        return f"{text}.SH"
    return f"{text}.SZ"


class Qmt2HttpClient:
    def __init__(self, config: Optional[Dict] = None):
        config = config or {}
        self.base_url = str(
            config.get("base_url")
            or os.getenv("QMT2HTTP_BASE_URL")
            or "http://150.158.31.115:8085"
        ).rstrip("/")
        self.api_token = str(config.get("api_token") or os.getenv("QMT2HTTP_API_TOKEN") or "").strip()
        self.timeout = float(config.get("timeout") or os.getenv("QMT2HTTP_TIMEOUT") or 20)
        self.account_id = str(config.get("account_id") or os.getenv("QMT_ACCOUNT_ID") or "").strip()
        self.account_type = str(config.get("account_type") or os.getenv("QMT_ACCOUNT_TYPE") or "STOCK").strip() or "STOCK"
        self.code_format = str(config.get("code_format") or os.getenv("QMT2HTTP_CODE_FORMAT") or "plain").strip().lower() or "plain"
        self.session = requests.Session()

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["X-API-Token"] = self.api_token
        return headers

    def _request(self, method: str, path: str, payload: Optional[Dict] = None) -> Dict:
        response = self.session.request(
            method=method,
            url=f"{self.base_url}{path}",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"qmt2http 返回非 JSON 响应: HTTP {response.status_code}") from exc
        if response.status_code >= 400 or not data.get("success", False):
            raise RuntimeError(data.get("message") or f"qmt2http 请求失败: HTTP {response.status_code}")
        return data

    def health(self) -> Dict:
        return self._request("GET", "/health")

    def probe_status(self) -> Dict:
        try:
            data = self.health()
        except Exception as exc:
            return {
                "reachable": False,
                "market_connected": False,
                "trade_connected": False,
                "status": "down",
                "reason": str(exc),
                "raw": {},
            }
        payload = data.get("data", {}) if isinstance(data, dict) else {}
        return {
            "reachable": True,
            "market_connected": bool(payload.get("xtdata_connected") or payload.get("xtconn_connected")),
            "trade_connected": bool(payload.get("trade_connected")),
            "status": str(payload.get("status", "ok")),
            "reason": str(payload.get("trade", {}).get("last_error") or data.get("message") or "").strip(),
            "raw": payload,
        }

    def place_order(
        self,
        stock_code: str,
        side: str,
        price: float,
        amount: int,
        strategy_name: str = "",
        order_remark: str = "",
    ) -> Dict:
        payload = {
            "stock_code": normalize_stock_code(stock_code, self.code_format),
            "side": side,
            "price": float(price),
            "amount": int(amount),
            "account_id": self.account_id or None,
            "account_type": self.account_type or None,
            "strategy_name": strategy_name or None,
            "order_remark": order_remark or None,
        }
        return self._request("POST", "/api/trade/order", payload)

    def cancel_order(self, entrust_no: str) -> Dict:
        payload = {
            "entrust_no": str(entrust_no),
            "account_id": self.account_id or None,
            "account_type": self.account_type or None,
        }
        return self._request("POST", "/api/trade/cancel", payload)

    def query_positions(self) -> Dict:
        params = {}
        if self.account_id:
            params["account_id"] = self.account_id
        if self.account_type:
            params["account_type"] = self.account_type
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        path = f"/api/stock/positions?{qs}" if qs else "/api/stock/positions"
        return self._request("GET", path)

    def query_balance(self) -> Dict:
        params = {}
        if self.account_id:
            params["account_id"] = self.account_id
        if self.account_type:
            params["account_type"] = self.account_type
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        path = f"/api/stock/asset?{qs}" if qs else "/api/stock/asset"
        return self._request("GET", path)

    def query_today_orders(self) -> Dict:
        params = {}
        if self.account_id:
            params["account_id"] = self.account_id
        if self.account_type:
            params["account_type"] = self.account_type
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        path = f"/api/stock/orders?{qs}" if qs else "/api/stock/orders"
        return self._request("GET", path)

    def query_today_trades(self) -> Dict:
        params = {}
        if self.account_id:
            params["account_id"] = self.account_id
        if self.account_type:
            params["account_type"] = self.account_type
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        path = f"/api/stock/trades?{qs}" if qs else "/api/stock/trades"
        return self._request("GET", path)

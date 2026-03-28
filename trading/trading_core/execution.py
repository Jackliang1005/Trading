import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .paths import BASE_DIR, COMMAND_BOOK_PATH, DEFAULT_CONFIG, PORTFOLIO_STATE_PATH
from .storage import atomic_write_json, load_json


class TradingExecutionBook:
    def __init__(self, base_dir: Path = BASE_DIR):
        self.base_dir = Path(base_dir)
        self.config_path = self.base_dir / DEFAULT_CONFIG.name
        self.portfolio_path = self.base_dir / PORTFOLIO_STATE_PATH.parent.name / PORTFOLIO_STATE_PATH.name
        self.command_book_path = self.base_dir / COMMAND_BOOK_PATH.parent.name / COMMAND_BOOK_PATH.name

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _load_stock_map(self) -> Dict[str, Dict]:
        data = load_json(self.config_path, {"stocks": []})
        result = {}
        for item in data.get("stocks", []):
            code = str(item.get("code", "")).strip()
            if not code:
                continue
            result[code] = {
                "code": code,
                "name": item.get("name", code),
                "base_position": int(item.get("base_position", 0) or 0),
                "per_trade_shares": int(item.get("per_trade_shares", 0) or 0),
            }
        return result

    def load_portfolio_state(self) -> Dict:
        today = self._today()
        stocks = self._load_stock_map()
        data = load_json(self.portfolio_path, {"date": today, "updated_at": "", "stocks": {}})
        if data.get("date") != today or not isinstance(data.get("stocks"), dict):
            data = {"date": today, "updated_at": "", "stocks": {}}
        for code, item in stocks.items():
            state = data["stocks"].setdefault(
                code,
                {
                    "code": code,
                    "name": item["name"],
                    "base_position": item["base_position"],
                    "carry_position": item["base_position"],
                    "intraday_buy": 0,
                    "intraday_sell": 0,
                    "current_position": item["base_position"],
                    "available_to_sell": item["base_position"],
                    "available_to_buy_back": 0,
                    "last_trade_at": "",
                },
            )
            state["name"] = item["name"]
            state["base_position"] = item["base_position"]
            state["carry_position"] = item["base_position"]
            self._refresh_stock_state(state)
        return data

    def save_portfolio_state(self, data: Dict) -> None:
        data["date"] = self._today()
        data["updated_at"] = self._now()
        atomic_write_json(self.portfolio_path, data)

    def _refresh_stock_state(self, state: Dict) -> None:
        carry = int(state.get("carry_position", 0) or 0)
        buy_qty = int(state.get("intraday_buy", 0) or 0)
        sell_qty = int(state.get("intraday_sell", 0) or 0)
        state["current_position"] = carry + buy_qty - sell_qty
        state["available_to_sell"] = max(0, carry - sell_qty)
        state["available_to_buy_back"] = max(0, sell_qty - buy_qty)

    def load_command_book(self) -> Dict:
        today = self._today()
        data = load_json(self.command_book_path, {"date": today, "updated_at": "", "commands": []})
        if data.get("date") != today or not isinstance(data.get("commands"), list):
            return {"date": today, "updated_at": "", "commands": []}
        return data

    def save_command_book(self, data: Dict) -> None:
        data["date"] = self._today()
        data["updated_at"] = self._now()
        atomic_write_json(self.command_book_path, data)

    def create_command(
        self,
        stock_code: str,
        action: str,
        price: float,
        quantity: int,
        reason: str = "",
        source: str = "manual",
    ) -> Dict:
        stocks = self._load_stock_map()
        stock = stocks.get(stock_code, {"name": stock_code})
        command_book = self.load_command_book()
        command = {
            "id": uuid.uuid4().hex[:8],
            "created_at": self._now(),
            "updated_at": self._now(),
            "stock_code": stock_code,
            "stock_name": stock.get("name", stock_code),
            "action": action,
            "price": float(price),
            "quantity": int(quantity),
            "reason": reason,
            "source": source,
            "status": "pending",
            "broker_channel": "",
            "broker_order_id": "",
            "broker_error": "",
            "broker_response": {},
            "execution_price": 0.0,
            "execution_note": "",
            "executed_at": "",
        }
        command_book["commands"].append(command)
        self.save_command_book(command_book)
        return command

    def list_commands(self, statuses: Optional[List[str]] = None) -> List[Dict]:
        commands = deepcopy(self.load_command_book().get("commands", []))
        if statuses:
            allowed = set(statuses)
            commands = [item for item in commands if item.get("status") in allowed]
        return sorted(commands, key=lambda item: item.get("created_at", ""), reverse=True)

    def find_command(self, command_id: str) -> Optional[Dict]:
        command_book = self.load_command_book()
        for item in command_book.get("commands", []):
            if item.get("id") == command_id:
                return item
        return None

    def update_command_status(self, command_id: str, status: str, note: str = "") -> Optional[Dict]:
        command_book = self.load_command_book()
        for item in command_book.get("commands", []):
            if item.get("id") != command_id:
                continue
            item["status"] = status
            item["updated_at"] = self._now()
            if note:
                item["execution_note"] = note
            self.save_command_book(command_book)
            return item
        return None

    def mark_command_submitted(
        self,
        command_id: str,
        broker_channel: str,
        broker_order_id: str = "",
        broker_response: Optional[Dict] = None,
        note: str = "",
    ) -> Optional[Dict]:
        command_book = self.load_command_book()
        for item in command_book.get("commands", []):
            if item.get("id") != command_id:
                continue
            item["status"] = "submitted"
            item["updated_at"] = self._now()
            item["broker_channel"] = broker_channel
            item["broker_order_id"] = str(broker_order_id or "")
            item["broker_response"] = broker_response or {}
            item["broker_error"] = ""
            if note:
                item["execution_note"] = note
            self.save_command_book(command_book)
            return item
        return None

    def mark_command_failed(
        self,
        command_id: str,
        broker_channel: str,
        error: str,
        broker_response: Optional[Dict] = None,
    ) -> Optional[Dict]:
        command_book = self.load_command_book()
        for item in command_book.get("commands", []):
            if item.get("id") != command_id:
                continue
            item["status"] = "failed"
            item["updated_at"] = self._now()
            item["broker_channel"] = broker_channel
            item["broker_error"] = error
            item["broker_response"] = broker_response or {}
            self.save_command_book(command_book)
            return item
        return None

    def match_pending_command(self, stock_code: str, action: str) -> Optional[Dict]:
        for item in self.list_commands(["pending", "acknowledged"]):
            if item.get("stock_code") == stock_code and item.get("action") == action:
                return item
        return None

    def record_execution(
        self,
        stock_code: str,
        action: str,
        price: float,
        quantity: int,
        note: str = "",
    ) -> Dict:
        state = self.load_portfolio_state()
        stock_state = state["stocks"].setdefault(
            stock_code,
            {
                "code": stock_code,
                "name": stock_code,
                "base_position": 0,
                "carry_position": 0,
                "intraday_buy": 0,
                "intraday_sell": 0,
                "current_position": 0,
                "available_to_sell": 0,
                "available_to_buy_back": 0,
                "last_trade_at": "",
            },
        )
        if action == "buy":
            stock_state["intraday_buy"] = int(stock_state.get("intraday_buy", 0) or 0) + int(quantity)
        elif action == "sell":
            stock_state["intraday_sell"] = int(stock_state.get("intraday_sell", 0) or 0) + int(quantity)
        stock_state["last_trade_at"] = self._now()
        stock_state["last_trade_price"] = float(price)
        if note:
            stock_state["last_trade_note"] = note
        self._refresh_stock_state(stock_state)
        self.save_portfolio_state(state)

        matched = self.match_pending_command(stock_code, action)
        if matched:
            self.update_command_execution(matched["id"], price, note)
        return stock_state

    def update_command_execution(self, command_id: str, price: float, note: str = "") -> Optional[Dict]:
        command_book = self.load_command_book()
        for item in command_book.get("commands", []):
            if item.get("id") != command_id:
                continue
            item["status"] = "executed"
            item["updated_at"] = self._now()
            item["execution_price"] = float(price)
            item["executed_at"] = self._now()
            if note:
                item["execution_note"] = note
            self.save_command_book(command_book)
            return item
        return None

import csv
from datetime import datetime
from pathlib import Path
from typing import Dict

from .paths import BASE_DIR, DEFAULT_CONFIG
from .storage import atomic_write_json, load_json


class TradingRecorder:
    def __init__(self, data_dir: str = "trading_data", base_dir: Path = BASE_DIR):
        self.base_dir = Path(base_dir)
        self.data_dir = self.base_dir / data_dir
        self.data_dir.mkdir(exist_ok=True)
        self.stocks = self._load_stock_map()

    def _load_stock_map(self) -> Dict[str, Dict]:
        data = load_json(self.base_dir / DEFAULT_CONFIG.name, {"stocks": []})
        result = {}
        for item in data.get("stocks", []):
            code = str(item.get("code", "")).strip()
            if not code:
                continue
            result[code] = {
                "name": item.get("name", "未知"),
                "cost": item.get("cost_price", 0),
                "position": item.get("base_position", 0),
            }
        return result

    def _load_records(self, date=None):
        day = date or datetime.now().strftime("%Y-%m-%d")
        record_file = self.data_dir / f"trades_{day}.json"
        return record_file, load_json(record_file, [])

    def _save_records(self, record_file: Path, records):
        atomic_write_json(record_file, records)

    def record_trade(self, stock_code, operation, price, quantity, reason=""):
        record_file, records = self._load_records()
        trade = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stock_code": str(stock_code),
            "stock_name": self.stocks.get(stock_code, {}).get("name", "未知"),
            "operation": operation,
            "price": float(price),
            "quantity": int(quantity),
            "amount": float(price) * int(quantity),
            "reason": reason,
            "profit": 0.0,
        }
        if operation == "sell" and records:
            for prev in reversed(records):
                if prev["stock_code"] == stock_code and prev["operation"] == "buy" and not prev.get("matched", False):
                    trade["profit"] = (price - prev["price"]) * quantity
                    prev["matched"] = True
                    prev["profit"] = trade["profit"]
                    break
        records.append(trade)
        self._save_records(record_file, records)
        return trade

    def record_skip(self, stock_code, reason="", planned_action=""):
        record_file, records = self._load_records()
        event = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stock_code": str(stock_code),
            "stock_name": self.stocks.get(stock_code, {}).get("name", "未知"),
            "operation": "skip",
            "planned_action": planned_action,
            "price": 0.0,
            "quantity": 0,
            "amount": 0.0,
            "reason": reason or "放弃执行",
            "profit": 0.0,
        }
        records.append(event)
        self._save_records(record_file, records)
        return event

    def get_daily_summary(self, date=None):
        day = date or datetime.now().strftime("%Y-%m-%d")
        record_file = self.data_dir / f"trades_{day}.json"
        records = load_json(record_file, [])
        total_profit = sum(t.get("profit", 0) for t in records if t.get("operation") == "sell")
        stock_stats = {}
        for trade in records:
            code = trade["stock_code"]
            stats = stock_stats.setdefault(
                code,
                {"name": trade["stock_name"], "trades": 0, "profit": 0, "buy_qty": 0, "sell_qty": 0},
            )
            stats["trades"] += 1
            if trade["operation"] == "sell":
                stats["profit"] += trade.get("profit", 0)
                stats["sell_qty"] += trade["quantity"]
            elif trade["operation"] == "buy":
                stats["buy_qty"] += trade["quantity"]
        return {
            "date": day,
            "total_trades": len(records),
            "buy_count": sum(1 for t in records if t["operation"] == "buy"),
            "sell_count": sum(1 for t in records if t["operation"] == "sell"),
            "skip_count": sum(1 for t in records if t["operation"] == "skip"),
            "total_profit": total_profit,
            "stock_stats": stock_stats,
            "trades": records,
        }

    def export_to_csv(self, date=None):
        day = date or datetime.now().strftime("%Y-%m-%d")
        summary = self.get_daily_summary(day)
        csv_file = self.data_dir / f"trades_{day}.csv"
        with csv_file.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["日期", "股票代码", "股票名称", "操作", "时间", "价格", "数量", "金额", "原因", "盈亏"])
            for trade in summary["trades"]:
                writer.writerow([
                    trade["timestamp"][:10],
                    trade["stock_code"],
                    trade["stock_name"],
                    "买入" if trade["operation"] == "buy" else ("卖出" if trade["operation"] == "sell" else "放弃"),
                    trade["timestamp"][11:],
                    f"{trade['price']:.2f}" if trade["operation"] != "skip" else "-",
                    trade["quantity"] if trade["operation"] != "skip" else "-",
                    f"{trade['amount']:.2f}" if trade["operation"] != "skip" else "-",
                    trade["reason"],
                    f"{trade.get('profit', 0):.2f}",
                ])
        return csv_file

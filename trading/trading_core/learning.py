from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from .models import LearningProfile
from .paths import BASE_DIR
from .storage import load_json


def _recent_trade_files(base_dir: Path, lookback_days: int = 10) -> List[Path]:
    data_dir = Path(base_dir) / "trading_data"
    today = datetime.now().date()
    files = []
    for offset in range(lookback_days):
        day = today - timedelta(days=offset)
        path = data_dir / f"trades_{day.isoformat()}.json"
        if path.exists():
            files.append(path)
    return files


def load_learning_profile(stock_code: str, base_dir: Path = BASE_DIR, lookback_days: int = 10) -> LearningProfile:
    profits = []
    for path in _recent_trade_files(base_dir, lookback_days=lookback_days):
        records = load_json(path, [])
        for item in records:
            if str(item.get("stock_code")) != str(stock_code):
                continue
            if item.get("operation") != "sell":
                continue
            profits.append(float(item.get("profit", 0) or 0))
    if not profits:
        return LearningProfile(sample_count=0, win_rate=0.0, avg_profit=0.0, bias="neutral")
    wins = [p for p in profits if p > 0]
    win_rate = len(wins) / len(profits)
    avg_profit = sum(profits) / len(profits)
    bias = "neutral"
    if len(profits) >= 3:
        if win_rate >= 0.6 and avg_profit > 0:
            bias = "aggressive"
        elif win_rate <= 0.4 or avg_profit < 0:
            bias = "defensive"
    return LearningProfile(
        sample_count=len(profits),
        win_rate=round(win_rate, 2),
        avg_profit=round(avg_profit, 2),
        bias=bias,
    )

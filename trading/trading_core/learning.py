from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

from .models import LearningProfile
from .paths import BASE_DIR, LEARNING_SNAPSHOT_PATH
from .storage import atomic_write_json, load_json


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


def _recent_journal_files(base_dir: Path, lookback_days: int = 10) -> List[Path]:
    data_dir = Path(base_dir) / "trading_data"
    today = datetime.now().date()
    files = []
    for offset in range(lookback_days):
        day = today - timedelta(days=offset)
        path = data_dir / f"decision_journal_{day.isoformat()}.json"
        if path.exists():
            files.append(path)
    return files


def _load_decision_rows(base_dir: Path, lookback_days: int) -> List[Dict]:
    rows: List[Dict] = []
    for path in _recent_journal_files(base_dir, lookback_days=lookback_days):
        payload = load_json(path, {"decisions": []})
        for item in payload.get("decisions", []):
            if isinstance(item, dict):
                rows.append(item)
    rows.sort(key=lambda item: str(item.get("timestamp", "")))
    return rows


def _match_structure_stats(stock_code: str, trade_rows: List[Dict], decision_rows: List[Dict]) -> Tuple[str, Dict[str, Dict[str, float]]]:
    structure_stats: Dict[str, Dict[str, float]] = {}
    for trade in trade_rows:
        if str(trade.get("stock_code")) != str(stock_code):
            continue
        if trade.get("operation") != "sell":
            continue
        profit = float(trade.get("profit", 0) or 0)
        trade_ts = str(trade.get("timestamp", "")).strip()
        if not trade_ts:
            continue
        matched = None
        for item in decision_rows:
            if str(item.get("stock_code")) != str(stock_code):
                continue
            if str(item.get("timestamp", "")).strip() <= trade_ts:
                matched = item
        if not matched:
            continue
        structure = str(matched.get("analysis_structure", "") or "unknown").strip() or "unknown"
        stats = structure_stats.setdefault(structure, {"count": 0, "profit": 0.0, "wins": 0})
        stats["count"] += 1
        stats["profit"] = round(float(stats.get("profit", 0.0) or 0.0) + profit, 2)
        if profit > 0:
            stats["wins"] += 1
    preferred = ""
    best_score = None
    for structure, stats in structure_stats.items():
        count = int(stats.get("count", 0) or 0)
        if count <= 0:
            continue
        avg_profit = float(stats.get("profit", 0.0) or 0.0) / count
        win_rate = float(stats.get("wins", 0) or 0) / count
        score = avg_profit * 0.7 + win_rate * 10
        if best_score is None or score > best_score:
            best_score = score
            preferred = structure
    return preferred, structure_stats


def load_learning_profile(stock_code: str, base_dir: Path = BASE_DIR, lookback_days: int = 10) -> LearningProfile:
    profits = []
    trade_rows: List[Dict] = []
    for path in _recent_trade_files(base_dir, lookback_days=lookback_days):
        records = load_json(path, [])
        for item in records:
            if not isinstance(item, dict):
                continue
            trade_rows.append(item)
            if str(item.get("stock_code")) != str(stock_code):
                continue
            if item.get("operation") != "sell":
                continue
            profits.append(float(item.get("profit", 0) or 0))
    if not profits:
        return LearningProfile(sample_count=0, win_rate=0.0, avg_profit=0.0, bias="neutral")
    decision_rows = _load_decision_rows(base_dir, lookback_days=lookback_days)
    preferred_structure, structure_stats = _match_structure_stats(stock_code, trade_rows, decision_rows)
    wins = [p for p in profits if p > 0]
    win_rate = len(wins) / len(profits)
    avg_profit = sum(profits) / len(profits)
    bias = "neutral"
    risk_factor = 1.0
    entry_tolerance = 1.0
    if len(profits) >= 3:
        if win_rate >= 0.6 and avg_profit > 0:
            bias = "aggressive"
            risk_factor = 1.15
            entry_tolerance = 1.05
        elif win_rate <= 0.4 or avg_profit < 0:
            bias = "defensive"
            risk_factor = 0.75
            entry_tolerance = 0.92
    if preferred_structure:
        stats = structure_stats.get(preferred_structure, {})
        count = int(stats.get("count", 0) or 0)
        if count >= 2:
            avg_structure_profit = float(stats.get("profit", 0.0) or 0.0) / count
            if avg_structure_profit > 0:
                risk_factor = min(1.3, risk_factor + 0.05)
            elif avg_structure_profit < 0:
                risk_factor = max(0.65, risk_factor - 0.05)
    return LearningProfile(
        sample_count=len(profits),
        win_rate=round(win_rate, 2),
        avg_profit=round(avg_profit, 2),
        bias=bias,
        risk_factor=round(risk_factor, 2),
        entry_tolerance=round(entry_tolerance, 2),
        preferred_structure=preferred_structure,
    )


def save_learning_snapshot(profiles: Dict[str, LearningProfile], base_dir: Path = BASE_DIR) -> Dict:
    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stocks": {
            code: {
                "sample_count": profile.sample_count,
                "win_rate": profile.win_rate,
                "avg_profit": profile.avg_profit,
                "bias": profile.bias,
                "risk_factor": profile.risk_factor,
                "entry_tolerance": profile.entry_tolerance,
                "preferred_structure": profile.preferred_structure,
            }
            for code, profile in profiles.items()
        },
    }
    path = Path(base_dir) / LEARNING_SNAPSHOT_PATH.parent.name / LEARNING_SNAPSHOT_PATH.name
    atomic_write_json(path, payload)
    return payload

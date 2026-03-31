from datetime import datetime
from pathlib import Path
from typing import Dict

from .models import DecisionResult, MarketRegimeReport, Playbook, StockRule
from .paths import BASE_DIR
from .storage import atomic_write_json, load_json


class ReviewEngine:
    def __init__(self, base_dir: Path = BASE_DIR):
        self.base_dir = Path(base_dir)
        self.data_dir = self.base_dir / "trading_data"
        self.data_dir.mkdir(exist_ok=True)

    def _journal_path(self, day: str = "") -> Path:
        value = day or datetime.now().strftime("%Y-%m-%d")
        return self.data_dir / f"decision_journal_{value}.json"

    def _trades_path(self, day: str = "") -> Path:
        value = day or datetime.now().strftime("%Y-%m-%d")
        return self.data_dir / f"trades_{value}.json"

    def append_decision(
        self,
        rule: StockRule,
        playbook: Playbook,
        market_regime: MarketRegimeReport,
        decision: DecisionResult,
        quote: Dict,
        selection: Dict | None = None,
    ) -> None:
        path = self._journal_path()
        data = load_json(path, {"date": datetime.now().strftime("%Y-%m-%d"), "decisions": []})
        selection = selection or {}
        data.setdefault("decisions", []).append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stock_code": rule.code,
            "stock_name": rule.name,
            "playbook": playbook.name,
            "market_regime": market_regime.regime,
            "selection_action": selection.get("action", ""),
            "selection_am_mode": selection.get("am_mode", selection.get("selection_am_mode", "")),
            "selection_pm_mode": selection.get("pm_mode", selection.get("selection_pm_mode", "")),
            "selection_reason": selection.get("reason", selection.get("selection_reason", "")),
            "selection_buy_blocked": bool(selection.get("buy_blocked", selection.get("selection_buy_blocked", False))),
            "selection_buy_block_reason": str(selection.get("buy_block_reason", selection.get("selection_buy_block_reason", "")) or ""),
            "price": float(quote.get("price", 0) or 0),
            "change_percent": float(quote.get("change_percent", 0) or 0),
            "action": decision.action,
            "score": decision.score,
            "level": decision.level,
            "trigger_price": decision.trigger_price,
            "execution_price": decision.execution_price,
            "target_price": decision.target_price,
            "stop_price": decision.stop_price,
            "allow_auto_trade": decision.allow_auto_trade,
            "reason": decision.reason,
            "risk_flags": decision.risk_flags,
        })
        atomic_write_json(path, data)

    def build_daily_review(self, day: str = "") -> Dict:
        path = self._journal_path(day)
        data = load_json(path, {"date": day or datetime.now().strftime("%Y-%m-%d"), "decisions": []})
        decisions = data.get("decisions", [])
        action_counts = {"buy": 0, "sell": 0, "wait": 0}
        playbook_counts = {}
        auto_candidates = 0
        blocked_candidates = 0
        for item in decisions:
            action = item.get("action", "wait")
            action_counts[action] = action_counts.get(action, 0) + 1
            playbook = item.get("playbook", "")
            playbook_counts[playbook] = playbook_counts.get(playbook, 0) + 1
            if item.get("allow_auto_trade"):
                auto_candidates += 1
            if item.get("selection_buy_blocked"):
                blocked_candidates += 1
        return {
            "date": data.get("date"),
            "decision_count": len(decisions),
            "action_counts": action_counts,
            "playbook_counts": playbook_counts,
            "auto_trade_candidates": auto_candidates,
            "buy_blocked_candidates": blocked_candidates,
        }

    def build_mode_review(self, day: str = "") -> Dict:
        path = self._journal_path(day)
        data = load_json(path, {"date": day or datetime.now().strftime("%Y-%m-%d"), "decisions": []})
        decisions = data.get("decisions", [])
        mode_stats: Dict[str, Dict] = {}
        decision_rows = []
        for item in decisions:
            decision_rows.append(item)
            for key in ("selection_am_mode", "selection_pm_mode"):
                mode = item.get(key) or "unknown"
                stats = mode_stats.setdefault(
                    mode,
                    {"count": 0, "buy": 0, "sell": 0, "wait": 0, "auto": 0, "realized_profit": 0.0, "wins": 0, "losses": 0},
                )
                stats["count"] += 1
                action = item.get("action", "wait")
                stats[action] = stats.get(action, 0) + 1
                if item.get("allow_auto_trade"):
                    stats["auto"] += 1
        trades = load_json(self._trades_path(day), [])
        for trade in trades:
            if trade.get("operation") != "sell":
                continue
            profit = float(trade.get("profit", 0) or 0)
            if profit == 0:
                continue
            stock_code = str(trade.get("stock_code", "")).strip()
            trade_ts = str(trade.get("timestamp", "")).strip()
            if not stock_code or not trade_ts:
                continue
            matched = None
            for item in decision_rows:
                if str(item.get("stock_code", "")).strip() != stock_code:
                    continue
                if str(item.get("timestamp", "")).strip() <= trade_ts:
                    matched = item
            if not matched:
                continue
            try:
                trade_hour = datetime.strptime(trade_ts, "%Y-%m-%d %H:%M:%S").hour
            except ValueError:
                trade_hour = 15
            mode = matched.get("selection_am_mode") if trade_hour < 13 else matched.get("selection_pm_mode")
            mode = mode or "unknown"
            stats = mode_stats.setdefault(
                mode,
                {"count": 0, "buy": 0, "sell": 0, "wait": 0, "auto": 0, "realized_profit": 0.0, "wins": 0, "losses": 0},
            )
            stats["realized_profit"] = round(float(stats.get("realized_profit", 0.0) or 0.0) + profit, 2)
            if profit > 0:
                stats["wins"] += 1
            elif profit < 0:
                stats["losses"] += 1
        return {
            "date": data.get("date"),
            "mode_stats": mode_stats,
            "decision_count": len(decisions),
        }

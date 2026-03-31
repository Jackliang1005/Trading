import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .models import StockRule
from .paths import DAILY_PLAN_PATH, STATE_PATH


def _safe_float(value) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def load_json(path: Path, default):
    if not path.exists():
        return deepcopy(default)
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_config(path: Path) -> Dict:
    return load_json(path, {})


def merge_daily_plan(config: Dict, plan_path: Path = DAILY_PLAN_PATH) -> Dict:
    daily = load_daily_plan(plan_path)
    overrides = {str(item["code"]): item for item in daily.get("stocks", [])}
    merged = deepcopy(config)
    for stock in merged.get("stocks", []):
        override = overrides.get(str(stock.get("code")))
        if override:
            stock.update(override)
    return merged


def load_daily_plan(plan_path: Path = DAILY_PLAN_PATH) -> Dict:
    today = datetime.now().strftime("%Y-%m-%d")
    default = {"date": today, "updated_at": "", "source": "monitor", "stocks": []}
    data = load_json(plan_path, default)
    if data.get("date") != today:
        return deepcopy(default)
    if not isinstance(data.get("stocks"), list):
        data["stocks"] = []
    return data


def save_daily_plan(plan: Dict, plan_path: Path = DAILY_PLAN_PATH, source: str = "") -> None:
    plan["date"] = datetime.now().strftime("%Y-%m-%d")
    plan["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if source:
        plan["source"] = source
    elif "source" not in plan:
        plan["source"] = "monitor"
    atomic_write_json(plan_path, plan)


def upsert_plan_override(plan: Dict, rule: StockRule) -> Dict:
    for item in plan.get("stocks", []):
        if str(item.get("code")) == rule.code:
            return item
    item = {"code": rule.code, "name": rule.name}
    plan.setdefault("stocks", []).append(item)
    return item


def load_state(path: Path = STATE_PATH) -> Dict:
    return load_json(path, {"signals": {}})


def save_state(state: Dict, path: Path = STATE_PATH) -> None:
    atomic_write_json(path, state)


def apply_rule_mode_defaults(rule: StockRule) -> None:
    if (rule.watch_mode or "").lower() == "light":
        rule.avoid_reverse_t = True
    if rule.buy_blocked:
        rule.avoid_reverse_t = True


def parse_rules(config: Dict) -> List[StockRule]:
    rules = []
    for item in config.get("stocks", []):
        rules.append(
            StockRule(
                code=str(item["code"]),
                name=item.get("name", str(item["code"])),
                cost_price=float(item["cost_price"]),
                base_position=int(item["base_position"]),
                per_trade_shares=int(item.get("per_trade_shares", 100)),
                buy_range=(float(item["buy_range"][0]), float(item["buy_range"][1])),
                sell_range=(float(item["sell_range"][0]), float(item["sell_range"][1])),
                stop_loss=float(item["stop_loss"]),
                strategy=item.get("strategy", "观察"),
                note=item.get("note", ""),
                watch_mode=str(item.get("watch_mode", "") or "").strip(),
                preopen_risk_mode=item.get("preopen_risk_mode", ""),
                avoid_reverse_t=bool(item.get("avoid_reverse_t", False)),
                abandon_buy_below=float(item.get("abandon_buy_below", 0) or 0),
                allow_rebound_watch_after_stop=bool(item.get("allow_rebound_watch_after_stop", False)),
                rebound_buy_above=float(item.get("rebound_buy_above", 0) or 0),
                allow_market_panic_reverse_t=bool(item.get("allow_market_panic_reverse_t", True)),
                panic_rebound_pct=float(item.get("panic_rebound_pct", 0.8) or 0.8),
                sector_tags=[str(v) for v in item.get("sector_tags", []) if str(v).strip()],
                enabled=bool(item.get("enabled", True)),
                buy_blocked=bool(item.get("buy_blocked", item.get("selection_buy_blocked", False))),
                buy_block_reason=str(
                    item.get("buy_block_reason", item.get("selection_buy_block_reason", "")) or ""
                ).strip(),
            )
        )
    for rule in rules:
        apply_rule_mode_defaults(rule)
    return rules

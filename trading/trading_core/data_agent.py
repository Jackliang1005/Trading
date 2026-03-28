import sys
from pathlib import Path
from typing import Dict, List

from .analysis import get_quotes_map
from .models import StockDataSnapshot, StockRule


FLOW_SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "eastmoney-flow"
if str(FLOW_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(FLOW_SKILL_DIR))
from eastmoney_flow import get_stock_flow  # type: ignore


def _amplitude_pct(high: float, low: float, pre_close: float) -> float:
    if pre_close <= 0:
        return 0.0
    return round((high - low) / pre_close * 100, 2)


def build_stock_data_snapshots(rules: List[StockRule]) -> Dict[str, StockDataSnapshot]:
    quotes = get_quotes_map([rule.code for rule in rules])
    snapshots: Dict[str, StockDataSnapshot] = {}
    for rule in rules:
        quote = quotes.get(rule.code, {})
        flow = get_stock_flow(rule.code)
        price = float(quote.get("price", 0) or 0)
        high = float(quote.get("high", 0) or 0)
        low = float(quote.get("low", 0) or 0)
        pre_close = float(quote.get("pre_close", 0) or 0)
        inflow = float(flow.get("main_net_inflow", 0) or 0)
        snapshots[rule.code] = StockDataSnapshot(
            code=rule.code,
            name=rule.name,
            price=price,
            change_percent=float(quote.get("change_percent", 0) or 0),
            amount=float(quote.get("amount", 0) or 0),
            high=high,
            low=low,
            open=float(quote.get("open", 0) or 0),
            pre_close=pre_close,
            amplitude_pct=_amplitude_pct(high, low, pre_close),
            main_net_inflow=inflow,
            flow_bias="inflow" if inflow > 0 else ("outflow" if inflow < 0 else "flat"),
        )
    return snapshots

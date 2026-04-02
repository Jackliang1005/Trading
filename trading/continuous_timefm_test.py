#!/usr/bin/env python3
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from trading_core.analysis import (
    MARKET_INDEXES,
    adjust_for_strategy,
    build_market_context,
    compute_intraday_analysis,
    fetch_full_intraday_bars,
    fetch_yesterday_daily,
    get_quotes_map,
)
from trading_core.decision_engine import make_decision
from trading_core.learning import load_learning_profile
from trading_core.market_regime import build_market_regime
from trading_core.paths import DEFAULT_CONFIG
from trading_core.playbook import select_playbook
from trading_core.storage import atomic_write_json, load_config, load_state, merge_daily_plan, parse_rules
from trading_core.timefm_agent import enrich_analysis_with_timefm


DATA_DIR = Path(__file__).resolve().parent / "trading_data"


def _format_summary(rule, quote, analysis, decision) -> str:
    forecast = analysis.forecast_summary or "TimeFM unavailable"
    return (
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"{rule.code} {rule.name} "
        f"px={float(quote.get('price', 0) or 0):.2f} "
        f"chg={float(quote.get('change_percent', 0) or 0):+.2f}% "
        f"struct={analysis.structure} "
        f"Tbuy={analysis.t_buy_target:.2f} "
        f"Tsell={analysis.t_sell_target:.2f} "
        f"spread={analysis.t_spread_pct:.1f}% "
        f"forecast={analysis.forecast_bias}/{analysis.forecast_return_pct:+.2f}%/{analysis.forecast_confidence} "
        f"decision={decision.action}/{decision.level}/{decision.score} "
        f"reason={decision.reason} "
        f"| {forecast}"
    )


def _snapshot_row(rule, quote, analysis, decision) -> dict:
    now = datetime.now()
    return {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "stock_code": rule.code,
        "stock_name": rule.name,
        "price": round(float(quote.get("price", 0) or 0), 2),
        "change_percent": round(float(quote.get("change_percent", 0) or 0), 2),
        "structure": analysis.structure,
        "t_buy_target": analysis.t_buy_target,
        "t_sell_target": analysis.t_sell_target,
        "t_spread_pct": analysis.t_spread_pct,
        "forecast_source": analysis.forecast_source,
        "forecast_bias": analysis.forecast_bias,
        "forecast_confidence": analysis.forecast_confidence,
        "forecast_return_pct": analysis.forecast_return_pct,
        "forecast_end_price": analysis.forecast_end_price,
        "forecast_high_price": analysis.forecast_high_price,
        "forecast_low_price": analysis.forecast_low_price,
        "forecast_summary": analysis.forecast_summary,
        "decision_action": decision.action,
        "decision_level": decision.level,
        "decision_score": decision.score,
        "decision_reason": decision.reason,
        "trigger_price": decision.trigger_price,
        "target_price": decision.target_price,
        "stop_price": decision.stop_price,
    }


def _append_snapshot(row: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    jsonl_path = DATA_DIR / f"continuous_timefm_{day}.jsonl"
    latest_path = DATA_DIR / "continuous_timefm_latest.json"
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    latest_payload = load_config(latest_path) if latest_path.exists() else {"updated_at": "", "rows": []}
    rows = [item for item in latest_payload.get("rows", []) if str(item.get("stock_code")) != str(row.get("stock_code"))]
    rows.append(row)
    latest_payload["updated_at"] = row["timestamp"]
    latest_payload["rows"] = sorted(rows, key=lambda item: str(item.get("stock_code", "")))
    atomic_write_json(latest_path, latest_payload)


def run_once(config_path: Path, target_codes: set[str] | None = None) -> int:
    config = merge_daily_plan(load_config(config_path), config_path.parent / "今日交易计划.json")
    rules = parse_rules(config)
    if target_codes:
        rules = [rule for rule in rules if rule.code in target_codes]
    if not rules:
        print("no matching enabled rules")
        return 1

    quote_codes = [rule.code for rule in rules] + [code for code, _ in MARKET_INDEXES]
    quotes = get_quotes_map(quote_codes)
    market = build_market_context(quotes)
    market_regime = build_market_regime(market)
    state = load_state()

    printed = 0
    for rule in rules:
        quote = quotes.get(rule.code)
        if not quote:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {rule.code} quote missing")
            continue
        bars = fetch_full_intraday_bars(rule.code)
        yesterday = fetch_yesterday_daily(rule.code)
        analysis = compute_intraday_analysis(rule.code, rule, bars or [], yesterday)
        if not analysis:
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"{rule.code} {rule.name} analysis unavailable bars={len(bars) if bars else 0}"
            )
            continue
        analysis = adjust_for_strategy(analysis, rule)
        analysis = enrich_analysis_with_timefm(rule.code, bars or [], analysis, config_path=config_path)
        learning = load_learning_profile(rule.code, base_dir=config_path.parent)
        playbook = select_playbook(rule)
        decision = make_decision(
            rule=rule,
            playbook=playbook,
            analysis=analysis,
            quote=quote,
            market_regime=market_regime,
            learning=learning,
            portfolio_state={},
            state=state,
        )
        row = _snapshot_row(rule, quote, analysis, decision)
        _append_snapshot(row)
        print(_format_summary(rule, quote, analysis, decision))
        printed += 1
    return 0 if printed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Continuous dry-run TimeFM test for trading")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="config file path")
    parser.add_argument("--codes", default="", help="comma-separated stock codes, default all enabled")
    parser.add_argument("--interval", type=int, default=60, help="seconds between runs")
    parser.add_argument("--iterations", type=int, default=0, help="0 means run forever")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    target_codes = {code.strip() for code in args.codes.split(",") if code.strip()} or None
    iteration = 0
    try:
        while True:
            iteration += 1
            run_once(config_path, target_codes=target_codes)
            if args.iterations > 0 and iteration >= args.iterations:
                return 0
            time.sleep(max(args.interval, 1))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

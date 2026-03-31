import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .analysis import (
    MARKET_INDEXES,
    adjust_for_strategy,
    apply_preopen_plan_override,
    build_market_context,
    classify_zone,
    compute_intraday_analysis,
    evaluate_preopen_warning,
    evaluate_rebound_watch,
    evaluate_rule,
    fetch_auction_snapshot,
    fetch_full_intraday_bars,
    fetch_yesterday_daily,
    format_market_context,
    format_signal,
    get_analysis_phase,
    get_quotes_map,
    has_obvious_price_scale_mismatch,
    is_first_crossing,
    mark_t_signal_sent,
    search_stock_news,
    should_send_t_signal,
    should_suppress_buy_signal,
    should_use_dynamic_range,
)
from .decision_engine import format_decision_message, make_decision
from .data_agent import build_stock_data_snapshots
from .learning import load_learning_profile
from .market_regime import build_market_regime
from .news_agent import build_news_snapshots
from .notifier import send_feishu
from .paths import DAILY_PLAN_PATH, DEFAULT_CONFIG
from .playbook import select_playbook
from .qmt2http_client import Qmt2HttpClient
from .review_engine import ReviewEngine
from .selector_agent import (
    format_selection_report,
    load_selection_map,
    save_selection_outputs,
    select_focus_list,
    sync_selection_to_daily_plan,
)
from .command_service import TradingCommandService
from .storage import load_config, load_daily_plan, load_state, merge_daily_plan, parse_rules, save_daily_plan, save_state


def in_trade_hours(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 930 <= hm <= 1130 or 1300 <= hm <= 1500


def in_preopen_hours(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 830 <= hm <= 920


def can_send_monitor_message(now: Optional[datetime] = None, allow_preopen: bool = False) -> bool:
    return in_trade_hours(now) or (allow_preopen and in_preopen_hours(now))


def should_send_signal(state: Dict, stock_code: str, signal_type: str, cooldown_minutes: int) -> bool:
    signals = state.setdefault("signals", {})
    last_sent = signals.setdefault(stock_code, {}).get(signal_type)
    if not last_sent:
        return True
    try:
        delta = (datetime.now() - datetime.fromisoformat(last_sent)).total_seconds() / 60
        return delta >= cooldown_minutes
    except ValueError:
        return True


def mark_signal_sent(state: Dict, stock_code: str, signal_type: str) -> None:
    state.setdefault("signals", {}).setdefault(stock_code, {})[signal_type] = datetime.now().isoformat(timespec="seconds")


def should_send_summary(state: Dict, summary_key: str, message: str) -> bool:
    summaries = state.setdefault("summary_push", {})
    cached = summaries.get(summary_key, {})
    if not isinstance(cached, dict):
        return True
    return str(cached.get("message", "") or "") != str(message or "")


def mark_summary_sent(state: Dict, summary_key: str, message: str) -> None:
    state.setdefault("summary_push", {})[summary_key] = {
        "sent_at": datetime.now().isoformat(timespec="seconds"),
        "message": str(message or ""),
    }


def selection_refresh_slot(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    hm = now.hour * 100 + now.minute
    if 930 <= hm < 1000:
        return "open"
    if 1000 <= hm < 1100:
        return "mid_am"
    if 1100 <= hm <= 1130:
        return "late_am"
    if 1300 <= hm < 1400:
        return "early_pm"
    if 1400 <= hm <= 1500:
        return "late_pm"
    return ""


def get_qmt_status(config: Dict) -> Dict:
    qmt_cfg = config.get("qmt2http", {}) if isinstance(config, dict) else {}
    if not bool(qmt_cfg.get("enabled", True)):
        return {
            "reachable": False,
            "market_connected": False,
            "trade_connected": False,
            "status": "disabled",
            "reason": "qmt2http.enabled=false",
            "raw": {},
        }
    return Qmt2HttpClient(qmt_cfg).probe_status()


def check_once(config_path: Path, dry_run: bool = False) -> int:
    config = merge_daily_plan(load_config(config_path), DAILY_PLAN_PATH)
    rules = parse_rules(config)
    if not rules:
        print("未配置有效股票")
        return 1
    monitor_cfg = config.get("monitor", {})
    feishu_cfg = config.get("feishu", {})
    cooldown_minutes = int(monitor_cfg.get("cooldown_minutes", 20))
    only_trade_hours = bool(monitor_cfg.get("only_trade_hours", True))
    allow_preopen_alerts = bool(monitor_cfg.get("allow_preopen_alerts", False))
    t_signal_enabled = bool(monitor_cfg.get("t_signal_enabled", True))
    t_signal_cooldown = int(monitor_cfg.get("t_signal_cooldown_minutes", 30))
    t_signal_max_per_day = int(monitor_cfg.get("t_signal_max_per_day", 4))
    dynamic_range_threshold = float(monitor_cfg.get("dynamic_range_threshold_pct", 8))
    context_refresh_cooldown = int(monitor_cfg.get("context_refresh_cooldown_minutes", 120))
    auto_trade_enabled = bool(config.get("qmt2http", {}).get("auto_trade_enabled", False))
    account_cfg = config.get("account", {})
    floating_cash_budget = float(account_cfg.get("floating_cash_budget", 100000) or 100000)
    max_single_t_ratio_of_holding = float(account_cfg.get("max_single_t_ratio_of_holding", 0.35) or 0.35)
    now = datetime.now()
    preopen_mode = in_preopen_hours(now)
    qmt_status = get_qmt_status(config)
    auto_trade_allowed = auto_trade_enabled and qmt_status.get("trade_connected", False)
    if only_trade_hours and not (in_trade_hours(now) or preopen_mode):
        print("当前非交易时段，跳过检查")
        return 0
    quotes = get_quotes_map([r.code for r in rules] + [code for code, _ in MARKET_INDEXES])
    market = build_market_context(quotes)
    market_regime = build_market_regime(market)
    print(format_market_context(market))
    print(market_regime.summary)
    qmt_summary = (
        f"QMT通道 status={qmt_status.get('status')} "
        f"market={'ok' if qmt_status.get('market_connected') else 'down'} "
        f"trade={'ok' if qmt_status.get('trade_connected') else 'down'}"
    )
    if qmt_status.get("reason"):
        qmt_summary += f" reason={qmt_status.get('reason')}"
    print(qmt_summary)
    state = load_state()
    send_allowed = can_send_monitor_message(now, allow_preopen=allow_preopen_alerts)
    if preopen_mode:
        plan = load_daily_plan(DAILY_PLAN_PATH)
        plan_changed = False
        sent_count = 0
        today = now.strftime("%Y-%m-%d")
        data_snapshots = build_stock_data_snapshots(rules)
        news_snapshots = build_news_snapshots(rules, now=now)
        selections = select_focus_list(
            rules,
            data_snapshots,
            news_snapshots,
            market_regime,
            floating_cash_budget=floating_cash_budget,
            max_single_t_ratio_of_holding=max_single_t_ratio_of_holding,
            now=now,
        )
        save_selection_outputs(selections, now=now)
        sync_selection_to_daily_plan(rules, selections)
        selection_message = format_selection_report(selections, title="盘前持仓做T筛选")
        if not qmt_status.get("trade_connected", False):
            selection_message += "\n通道状态：交易接口不可用，本轮自动交易已降级关闭。"
        print(selection_message)
        print("-" * 60)
        selection_signal_type = f"selection_report_{today}"
        summary_key = "preopen_selection_report"
        if should_send_signal(state, "system", selection_signal_type, 99999) and should_send_summary(state, summary_key, selection_message):
            delivered = dry_run or not feishu_cfg.get("enabled", True) or (send_allowed and send_feishu(feishu_cfg.get("target", "").strip(), selection_message))
            if delivered:
                mark_signal_sent(state, "system", selection_signal_type)
                mark_summary_sent(state, summary_key, selection_message)
                if feishu_cfg.get("enabled", True) and not dry_run and send_allowed:
                    sent_count += 1
        for rule in rules:
            result = evaluate_preopen_warning(rule, quotes.get(rule.code, {}), fetch_auction_snapshot(rule.code), market)
            if not result:
                continue
            level, message = result
            apply_preopen_plan_override(rule, plan, market, fetch_auction_snapshot(rule.code), level)
            plan_changed = True
            signal_type = f"preopen_warning_{today}"
            if not should_send_signal(state, rule.code, signal_type, 99999):
                continue
            delivered = dry_run or not feishu_cfg.get("enabled", True) or (send_allowed and send_feishu(feishu_cfg.get("target", "").strip(), message))
            print(message)
            print("-" * 60)
            if delivered:
                mark_signal_sent(state, rule.code, signal_type)
                if feishu_cfg.get("enabled", True) and not dry_run and send_allowed:
                    sent_count += 1
        if plan_changed:
            save_daily_plan(plan, DAILY_PLAN_PATH, source="monitor-preopen")
        save_state(state)
        print(f"本轮盘前检查完成，发送 {sent_count} 条预警")
        return 0
    phase = get_analysis_phase(now)
    today_str = now.strftime("%Y-%m-%d")
    selection_map = load_selection_map()
    yesterday_cache = state.setdefault("yesterday_cache", {})
    if yesterday_cache.get("date") != today_str:
        yesterday_cache.clear()
        yesterday_cache["date"] = today_str
    intraday_state = state.setdefault("intraday", {})
    for code_key in list(intraday_state.keys()):
        if isinstance(intraday_state[code_key], dict) and intraday_state[code_key].get("date") != today_str:
            del intraday_state[code_key]
    sent_count = 0
    auto_trade_count = 0
    command_service = TradingCommandService(base_dir=config_path.parent)
    review_engine = ReviewEngine(base_dir=config_path.parent)
    portfolio_state = command_service.get_execution_book().load_portfolio_state()
    context_slot = selection_refresh_slot(now)
    refresh_state = state.setdefault("context_refresh", {})
    if context_slot and refresh_state.get("date") != today_str:
        refresh_state.clear()
        refresh_state["date"] = today_str
    if context_slot and refresh_state.get("slot") != context_slot:
        data_snapshots = build_stock_data_snapshots(rules)
        news_snapshots = build_news_snapshots(rules, now=now)
        refreshed_selections = select_focus_list(
            rules,
            data_snapshots,
            news_snapshots,
            market_regime,
            floating_cash_budget=floating_cash_budget,
            max_single_t_ratio_of_holding=max_single_t_ratio_of_holding,
            now=now,
        )
        save_selection_outputs(refreshed_selections, now=now)
        sync_selection_to_daily_plan(rules, refreshed_selections)
        selection_map = load_selection_map()
        refresh_state["slot"] = context_slot
        selection_message = format_selection_report(refreshed_selections, title="盘中上下文刷新")
        print(selection_message)
        print("-" * 60)
        signal_type = "context_refresh_summary"
        summary_key = "intraday_context_refresh"
        if should_send_signal(state, "system", signal_type, context_refresh_cooldown) and should_send_summary(state, summary_key, selection_message):
            delivered = dry_run or not feishu_cfg.get("enabled", True) or (send_allowed and send_feishu(feishu_cfg.get("target", "").strip(), selection_message))
            if delivered:
                mark_signal_sent(state, "system", signal_type)
                mark_summary_sent(state, summary_key, selection_message)
                if feishu_cfg.get("enabled", True) and not dry_run and send_allowed:
                    sent_count += 1
    for rule in rules:
        selection = selection_map.get(rule.code, {})
        pool_action = selection.get("action", "focus")
        am_mode = str(selection.get("am_mode", selection.get("selection_am_mode", "")) or "")
        pm_mode = str(selection.get("pm_mode", selection.get("selection_pm_mode", "")) or "")
        day_mode = am_mode if now.hour < 13 else (pm_mode or am_mode)
        buy_budget = float(selection.get("buy_budget_amount", selection.get("selection_buy_budget_amount", 0)) or 0)
        sell_budget = float(selection.get("sell_budget_amount", selection.get("selection_sell_budget_amount", 0)) or 0)
        selection_reason = str(selection.get("reason", selection.get("selection_reason", "")) or "")
        selection_explanation = str(selection.get("explanation", selection.get("selection_explanation", "")) or "")
        suggested_buy_shares = int(selection.get("suggested_buy_shares", selection.get("selection_buy_shares", 0)) or 0)
        suggested_sell_shares = int(selection.get("suggested_sell_shares", selection.get("selection_sell_shares", 0)) or 0)
        if pool_action == "avoid":
            print(f"{rule.name}({rule.code}) 今日在回避池，跳过盘中决策")
            continue
        quote = quotes.get(rule.code)
        if not quote:
            print(f"未获取到 {rule.code} 行情")
            continue
        if has_obvious_price_scale_mismatch(rule, quote):
            print(f"{rule.name}({rule.code}) 行情口径疑似异常，跳过本轮信号判断")
            continue
        analysis = None
        is_dynamic = False
        if t_signal_enabled and phase in ("first_wave", "steady", "winddown"):
            yesterday = yesterday_cache.get(rule.code) or fetch_yesterday_daily(rule.code)
            if yesterday:
                yesterday_cache[rule.code] = yesterday
            bars = fetch_full_intraday_bars(rule.code)
            if bars and len(bars) >= 15:
                raw_analysis = compute_intraday_analysis(rule.code, rule, bars, yesterday)
                if raw_analysis:
                    analysis = adjust_for_strategy(raw_analysis, rule)
            is_dynamic = analysis is not None and should_use_dynamic_range(rule, quote, dynamic_range_threshold)
        playbook = select_playbook(rule)
        learning = load_learning_profile(rule.code, base_dir=config_path.parent)
        signal_type = evaluate_rule(rule, quote)
        if not signal_type:
            signal_type = evaluate_rebound_watch(rule, quote, state)
        price = float(quote["price"])
        state.setdefault("zones", {})[rule.code] = classify_zone(price, rule)
        if analysis and phase in ("first_wave", "steady", "winddown"):
            decision = make_decision(
                rule=rule,
                playbook=playbook,
                analysis=analysis,
                quote=quote,
                market_regime=market_regime,
                learning=learning,
                portfolio_state=portfolio_state,
                state=state,
                selection_mode=day_mode,
            )
            review_engine.append_decision(rule, playbook, market_regime, decision, quote, selection=selection)
            if decision.action in ("buy", "sell") and should_send_t_signal(state, rule.code, t_signal_cooldown, t_signal_max_per_day):
                trade_shares = suggested_buy_shares if decision.action == "buy" else suggested_sell_shares
                if trade_shares <= 0:
                    trade_shares = rule.per_trade_shares
                active_budget = buy_budget if decision.action == "buy" else sell_budget
                message = format_decision_message(rule, decision, market_regime, learning)
                message += (
                    f"\n观察池分组：{pool_action} / 时段模式：AM {am_mode or '-'} / PM {pm_mode or '-'} / 当前 {day_mode or '-'}"
                    f" / 买预算：{buy_budget:.0f} / 卖预算：{sell_budget:.0f} / 当前预算：{active_budget:.0f} / 建议做T股数：{trade_shares}"
                )
                if selection_reason:
                    message += f"\n选票原因：{selection_reason}"
                if selection_explanation:
                    message += f"\n新闻解释：{selection_explanation}"
                if is_dynamic:
                    message += "\n补充：当前处于动态区间模式。"
                print(message)
                print("-" * 60)
                delivered = dry_run or not feishu_cfg.get("enabled", True) or (send_allowed and send_feishu(feishu_cfg.get("target", "").strip(), message))
                if delivered:
                    mark_t_signal_sent(state, rule.code, analysis)
                    if feishu_cfg.get("enabled", True) and not dry_run and send_allowed:
                        sent_count += 1
                if auto_trade_allowed and not dry_run and pool_action == "focus" and decision.allow_auto_trade:
                    exec_result = command_service.create_and_execute_command(
                        stock_code=rule.code,
                        action=decision.action,
                        price=decision.execution_price or float(quote["price"]),
                        quantity=trade_shares,
                        reason=f"{decision.playbook_name} {decision.level} {phase}",
                        source="decision-engine-auto",
                    )
                    stock_intraday = state.setdefault("intraday", {}).setdefault(rule.code, {"date": today_str})
                    if stock_intraday.get("date") != today_str:
                        stock_intraday.clear()
                        stock_intraday["date"] = today_str
                    last_auto_action = str(stock_intraday.get("last_auto_action", "") or "")
                    if last_auto_action and last_auto_action != decision.action:
                        stock_intraday["round_trip_count"] = int(stock_intraday.get("round_trip_count", 0) or 0) + 1
                    stock_intraday["last_auto_action"] = decision.action
                    stock_intraday["auto_trade_count"] = int(stock_intraday.get("auto_trade_count", 0) or 0) + 1
                    print(exec_result)
                    print("-" * 60)
                    auto_trade_count += 1
                    portfolio_state = command_service.get_execution_book().load_portfolio_state()
                continue
        if not signal_type:
            continue
        if signal_type in ("buy", "rebound_buy") and day_mode in ("sell_only", "observe_only"):
            continue
        if signal_type in ("buy", "rebound_buy") and should_suppress_buy_signal(rule, quote, market):
            state.setdefault("zones", {})[rule.code] = "buy"
            continue
        if not should_send_signal(state, rule.code, signal_type, cooldown_minutes):
            continue
        current_zone = "buy" if signal_type == "rebound_buy" else signal_type
        if signal_type in ("buy", "sell") and not is_first_crossing(rule.code, rule, current_zone, state):
            continue
        if signal_type == "rebound_buy":
            state.setdefault("zones", {})[rule.code] = "buy"
        news_context = ""
        if signal_type == "risk":
            news_context = search_stock_news(rule.name, "大跌" if float(quote.get("change_percent", 0)) < -5 else "下跌")
            state.setdefault("zones", {})[rule.code] = "risk"
        elif abs(float(quote.get("change_percent", 0))) > 5:
            news_context = search_stock_news(rule.name, "大涨" if float(quote.get("change_percent", 0)) > 0 else "大跌")
        message = format_signal(rule, quote, signal_type, market, news_context)
        fallback_shares = suggested_buy_shares if signal_type in ("buy", "rebound_buy") else suggested_sell_shares
        if fallback_shares <= 0:
            fallback_shares = rule.per_trade_shares
        message += (
            f"\n观察池分组：{pool_action} / 时段模式：AM {am_mode or '-'} / PM {pm_mode or '-'} / 当前 {day_mode or '-'}"
            f" / 买预算：{buy_budget:.0f} / 卖预算：{sell_budget:.0f} / 建议做T股数：{fallback_shares}"
        )
        if selection_reason:
            message += f"\n选票原因：{selection_reason}"
        if selection_explanation:
            message += f"\n新闻解释：{selection_explanation}"
        if analysis:
            message += f"\n--- 做T大脑摘要 ---\nVWAP {analysis.vwap:.2f} / T买 {analysis.t_buy_target:.2f} / T卖 {analysis.t_sell_target:.2f} / 价差 {analysis.t_spread_pct:.1f}% / 置信度 {analysis.confidence}"
        print(message)
        print("-" * 60)
        delivered = dry_run or not feishu_cfg.get("enabled", True) or (send_allowed and send_feishu(feishu_cfg.get("target", "").strip(), message))
        if delivered:
            mark_signal_sent(state, rule.code, signal_type)
            if feishu_cfg.get("enabled", True) and not dry_run and send_allowed:
                sent_count += 1
    save_state(state)
    print(f"本轮检查完成，发送 {sent_count} 条提醒，自动下单 {auto_trade_count} 笔")
    return 0


def run_daemon(config_path: Path, dry_run: bool = False) -> int:
    poll_seconds = int(merge_daily_plan(load_config(config_path), DAILY_PLAN_PATH).get("monitor", {}).get("poll_seconds", 60))
    print(f"开始监控，轮询间隔 {poll_seconds} 秒")
    try:
        while True:
            check_once(config_path, dry_run=dry_run)
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        print("监控已停止")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="做T半自动飞书监控")
    parser.add_argument("mode", choices=["check", "daemon"], help="check: 检查一次, daemon: 持续监控")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不发飞书")
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        print(f"配置文件不存在: {config_path}")
        return 1
    return check_once(config_path, dry_run=args.dry_run) if args.mode == "check" else run_daemon(config_path, dry_run=args.dry_run)

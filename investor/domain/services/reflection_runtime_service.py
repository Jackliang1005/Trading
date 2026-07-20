#!/usr/bin/env python3
"""Runtime reflection helpers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

import db
from data_collector import (
    fetch_akshare_stock_history,
    fetch_historical_kline,
    fetch_market_quotes,
    fetch_qmt_trading_summary,
)
from domain.policies.scoring_policy import calculate_prediction_score

# 评分阈值
NEUTRAL_THRESHOLD = 0.3
MICRO_MOVE_THRESHOLD = 0.1
DECISION_MONITOR_PATH = Path("/root/.openclaw/workspace/reports/investor_decision_monitor_latest.json")


def backtest_predictions(target_date: str | None = None) -> Dict:
    """
    回测3天前的预测（给足时间让K线走完）。

    对于新格式预测（有 trend_3d + kline_day1）:
      - 拉取预测日期之后3个交易日的实际K线
      - 用新评分算法打分

    对于旧格式预测（direction up/down/neutral）:
      - 使用旧评分逻辑兼容
    """
    if target_date is None:
        target_date = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")

    print(f"🔍 回测预测 (截止日期: {target_date}, 给3天K线走完)")

    # Get unchecked predictions from 3+ days ago AND already-checked ones from 3 days ago
    # that were marked with the old scoring and have new-format data
    unchecked = db.get_unchecked_predictions(before_date=target_date)

    # Also include predictions from exactly 3 days ago that were checked
    # with legacy scoring but have new-format data (trend_3d not NULL)
    if not unchecked:
        print("  ℹ️ 没有需要回测的预测")
        return {"total": 0, "checked": 0, "correct": 0, "win_rate": 0}

    print(f"  📋 找到 {len(unchecked)} 条待回测预测")
    checked = 0
    correct = 0

    for pred in unchecked:
        pred_id = pred["id"]
        target = pred["target"]
        price_at_predict = pred.get("actual_price_at_predict", 0) or 0

        # Determine prediction date
        pred_date = (pred.get("created_at") or "")[:10]

        # Check if this is a new-format prediction
        trend_3d = (pred.get("trend_3d") or "").strip()

        if trend_3d:
            # ── New format: 3d-kline backtest ──
            # Fetch 3 trading days after prediction date
            end_date = target_date
            actual_kline = fetch_historical_kline(target, pred_date, end_date)

            if not actual_kline or len(actual_kline) < 1:
                print(f"  ⚠️ 无法获取 {target} 的3日K线数据，跳过")
                continue

            # Take up to 3 days
            actual_3d = actual_kline[:3]
            actual_3d_high = max(d.get("high", 0) or 0 for d in actual_3d)
            actual_3d_low = min(d.get("low", float("inf")) or float("inf") for d in actual_3d)
            if actual_3d_low == float("inf"):
                actual_3d_low = 0

            # 3-day return: from prediction day's price to last day's close
            if price_at_predict and price_at_predict > 0 and actual_3d[-1].get("close", 0):
                actual_3d_close_final = float(actual_3d[-1]["close"])
                actual_3d_return = (actual_3d_close_final - price_at_predict) / price_at_predict * 100
            else:
                actual_3d_close_final = float(actual_3d[-1].get("close", 0) or 0)
                actual_3d_return = 0

            # Determine correctness for old is_correct field compatibility
            trend = str(trend_3d)
            is_correct = False
            if trend == "bullish" and actual_3d_return > 0.5:
                is_correct = True
            elif trend == "bearish" and actual_3d_return < -0.5:
                is_correct = True
            elif trend == "ranging" and abs(actual_3d_return) < 1.5:
                is_correct = True

            score = calculate_prediction_score(
                pred,
                actual_3d_kline=actual_3d,
                actual_3d_return=actual_3d_return,
                actual_3d_high=actual_3d_high,
                actual_3d_low=actual_3d_low,
                actual_3d_close=actual_3d_close_final,
            )
            note = (
                f"趋势:{trend} 实际3日收益:{actual_3d_return:+.2f}% "
                f"高{actual_3d_high:.2f}/低{actual_3d_low:.2f}"
            )

            actual_price = actual_3d_close_final
            actual_change = actual_3d_return

        else:
            # ── Legacy format fallback ──
            actual_price = None
            actual_change = None

            quotes = fetch_market_quotes(target)
            if quotes and not quotes[0].get("error"):
                quote = quotes[0]
                actual_price = quote.get("price", 0)
                actual_change = quote.get("change_percent", 0)
            else:
                clean_code = target.replace("sh", "").replace("sz", "")
                hist = fetch_akshare_stock_history(clean_code, days=2)
                if hist:
                    latest = hist[-1]
                    actual_price = latest.get("close", 0)
                    actual_change = latest.get("change_pct", 0)

            if actual_price is None or actual_price == 0:
                print(f"  ⚠️ 无法获取 {target} 的实际价格，跳过")
                continue

            if actual_change is None and price_at_predict and price_at_predict > 0:
                actual_change = (actual_price - price_at_predict) / price_at_predict * 100

            direction = pred.get("direction", "")
            is_correct = False
            is_near_miss = False
            if direction == "up" and actual_change is not None:
                if actual_change > 0:
                    is_correct = True
                elif abs(actual_change) < MICRO_MOVE_THRESHOLD:
                    is_near_miss = True
            elif direction == "down" and actual_change is not None:
                if actual_change < 0:
                    is_correct = True
                elif abs(actual_change) < MICRO_MOVE_THRESHOLD:
                    is_near_miss = True
            elif direction == "neutral" and actual_change is not None:
                if abs(actual_change) < NEUTRAL_THRESHOLD:
                    is_correct = True

            score = calculate_prediction_score(pred, actual_change, is_correct, is_near_miss)
            note = f"方向预测{'正确' if is_correct else '错误'}，预测{direction}，实际涨跌{actual_change:.2f}%"

        # Write back results
        db.update_prediction_result(
            pred_id=pred_id,
            actual_price=actual_price,
            actual_change=actual_change if actual_change is not None else 0,
            is_correct=is_correct,
            score=score,
            note=note,
        )
        db.add_prediction_evaluation(
            prediction_id=pred_id,
            actual_price=actual_price,
            actual_change=actual_change if actual_change is not None else 0,
            is_correct=is_correct,
            score=score,
            note=note,
            target_date=target_date,
            source="backtest",
        )

        checked += 1
        if is_correct:
            correct += 1
        trend_label = f"趋势{trend_3d}" if trend_3d else pred.get("direction", "?")
        print(f"  {'✅' if is_correct else '❌'} [{target}] {trend_label} | 得分{score:.0f}")

    win_rate = (correct / checked * 100) if checked > 0 else 0
    result = {
        "date": target_date,
        "total": len(unchecked),
        "checked": checked,
        "correct": correct,
        "win_rate": win_rate,
    }
    print(f"\n📊 回测完成: {checked}/{len(unchecked)} 已检查, 胜率 {win_rate:.1f}%")
    return result


def load_reflection_context() -> Dict:
    """优先从 packet 体系加载反思上下文，回退到实时拉取。"""
    bundle = db.get_latest_analysis_context_bundle(packet_types=["prediction_context"])
    prediction_context = (bundle.get("research_packets", {}) or {}).get("prediction_context")
    portfolio_snapshot = bundle.get("portfolio_snapshot")

    trading_summary = {}
    qmt_account = {}
    qmt_positions = []
    qmt_orders = []
    qmt_trades = []
    source = "runtime"
    as_of_date = ""

    if prediction_context or portfolio_snapshot:
        source = "research_packets"
        as_of_date = str(
            (prediction_context or {}).get("as_of_date")
            or (portfolio_snapshot or {}).get("as_of_date")
            or ""
        )
        if prediction_context:
            pdata = prediction_context.get("data", {}) or {}
            trading_summary = pdata.get("qmt_trading_summary", {}) or {}
            qmt_account = pdata.get("qmt_account", {}) or {}
            qmt_positions = pdata.get("qmt_positions", []) or []
            qmt_orders = pdata.get("qmt_orders", []) or []
            qmt_trades = pdata.get("qmt_trades", []) or []
        if portfolio_snapshot:
            vdata = portfolio_snapshot.get("data", {}) or {}
            trading_summary = trading_summary or (vdata.get("qmt_trading_summary", {}) or {})
            qmt_account = qmt_account or (vdata.get("qmt_account", {}) or {})
            qmt_positions = qmt_positions or (vdata.get("qmt_positions", []) or [])
            qmt_orders = qmt_orders or (vdata.get("qmt_orders", []) or [])
            qmt_trades = qmt_trades or (vdata.get("qmt_trades", []) or [])

    if not trading_summary:
        source = "runtime"
        trading_summary = fetch_qmt_trading_summary()
        qmt_account = trading_summary.get("accounts", {}) or qmt_account
        qmt_positions = trading_summary.get("positions", []) or qmt_positions
        qmt_orders = trading_summary.get("today_orders", []) or qmt_orders
        qmt_trades = trading_summary.get("today_trades", []) or qmt_trades

    return {
        "source": source,
        "as_of_date": as_of_date,
        "trading_summary": trading_summary,
        "qmt_account": qmt_account,
        "qmt_positions": qmt_positions,
        "qmt_orders": qmt_orders,
        "qmt_trades": qmt_trades,
    }


def _build_reflection_trading_summary(context: Dict) -> Dict:
    """Normalize reflection trading summary with fallback fields from context."""
    ts = dict(context.get("trading_summary", {}) or {})
    positions = _resolve_positions(ts) or list(context.get("qmt_positions", []) or [])
    orders = _resolve_orders(ts) or list(context.get("qmt_orders", []) or [])
    trades = _resolve_trades(ts) or list(context.get("qmt_trades", []) or [])
    accounts = _resolve_accounts(ts) or dict(context.get("qmt_account", {}) or {})

    ts["positions"] = positions
    ts["today_orders"] = orders
    ts["today_trades"] = trades
    if accounts:
        ts["accounts"] = accounts
    ts["positions_count"] = int(ts.get("positions_count", len(positions)) or len(positions))
    ts["today_order_count"] = int(ts.get("today_order_count", len(orders)) or len(orders))
    ts["today_trade_count"] = int(ts.get("today_trade_count", len(trades)) or len(trades))
    if "as_of_date" not in ts and context.get("as_of_date"):
        ts["as_of_date"] = str(context.get("as_of_date"))

    def _usable_position_rows(rows: list) -> bool:
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            vol = float(item.get("volume", item.get("current_volume", 0)) or 0)
            mv = float(item.get("market_value", 0) or 0)
            cost = float(item.get("cost_price", item.get("open_price", 0)) or 0)
            price = float(item.get("current_price", item.get("last_price", 0)) or 0)
            pnl = item.get("unrealized_pnl", item.get("float_profit", item.get("m_dFloatProfit", item.get("profit_loss"))))
            pnl = float(pnl or 0)
            if vol > 0 or mv > 0 or cost > 0 or price > 0 or abs(pnl) > 0:
                return True
        return False

    # packet 里仅有占位持仓时，回退一次实时摘要补齐明细
    if ts.get("positions_count", 0) and not _usable_position_rows(ts.get("positions", [])):
        try:
            runtime = fetch_qmt_trading_summary() or {}
        except Exception:
            runtime = {}
        runtime_positions = runtime.get("positions", []) or []
        if _usable_position_rows(runtime_positions):
            ts["positions"] = runtime_positions
            ts["today_orders"] = runtime.get("today_orders", ts.get("today_orders", [])) or []
            ts["today_trades"] = runtime.get("today_trades", ts.get("today_trades", [])) or []
            if runtime.get("accounts"):
                ts["accounts"] = runtime.get("accounts")
            ts["positions_count"] = int(runtime.get("positions_count", len(runtime_positions)) or len(runtime_positions))
            ts["today_order_count"] = int(runtime.get("today_order_count", len(ts.get("today_orders", []))) or len(ts.get("today_orders", [])))
            ts["today_trade_count"] = int(runtime.get("today_trade_count", len(ts.get("today_trades", []))) or len(ts.get("today_trades", [])))
            ts["total_market_value"] = float(runtime.get("total_market_value", ts.get("total_market_value", 0)) or 0)
            ts["total_unrealized_pnl"] = float(runtime.get("total_unrealized_pnl", ts.get("total_unrealized_pnl", 0)) or 0)
            ts["context_source_detail"] = "runtime_fallback_for_positions"
    return ts


def get_reflection_context_summary() -> Dict:
    context = load_reflection_context()
    trading_summary = _build_reflection_trading_summary(context)
    return {
        "source": context.get("source", "unknown"),
        "as_of_date": context.get("as_of_date", ""),
        "positions_count": trading_summary.get("positions_count", len(context.get("qmt_positions", []) or [])),
        "today_trade_count": trading_summary.get("today_trade_count", len(context.get("qmt_trades", []) or [])),
        "today_order_count": trading_summary.get("today_order_count", len(context.get("qmt_orders", []) or [])),
        "total_unrealized_pnl": trading_summary.get("total_unrealized_pnl", 0),
        "has_trading_summary": bool(trading_summary),
    }


def _resolve_positions(ts: Dict) -> list:
    """从多种 key 中解析持仓列表。"""
    for key in ("positions", "qmt_positions", "holdings"):
        val = ts.get(key)
        if isinstance(val, list) and val:
            return val
    return []


def _resolve_trades(ts: Dict) -> list:
    for key in ("today_trades", "qmt_trades", "trades"):
        val = ts.get(key)
        if isinstance(val, list) and val:
            return val
    return []


def _resolve_orders(ts: Dict) -> list:
    for key in ("today_orders", "qmt_orders", "orders"):
        val = ts.get(key)
        if isinstance(val, list) and val:
            return val
    return []


def _resolve_accounts(ts: Dict) -> dict:
    for key in ("accounts", "qmt_account"):
        val = ts.get(key)
        if isinstance(val, dict) and val:
            return val
    return {}


def _valid_trades(ts: Dict) -> list:
    """Only broker-like fills with positive quantity and price count as trades."""
    rows = []
    for item in _resolve_trades(ts):
        if not isinstance(item, dict):
            continue
        volume = float(item.get("trade_volume", item.get("deal_volume", 0)) or 0)
        price = float(item.get("trade_price", item.get("deal_price", 0)) or 0)
        if volume > 0 and price > 0:
            rows.append(item)
    return rows


def _build_positions_table(positions: list) -> tuple[str, list]:
    """Build position P&L table. Returns (markdown_lines, enriched_objects)."""
    enriched = []
    for pos in positions:
        code = str(pos.get("stock_code", pos.get("code", "")) or "")
        name = str(pos.get("stock_name", pos.get("instrument_name", pos.get("name", code))) or code)
        vol = int(pos.get("volume", pos.get("current_volume", 0)) or 0)
        cost = float(pos.get("cost_price", pos.get("open_price", 0)) or 0)
        price = float(pos.get("current_price", pos.get("last_price", 0)) or 0)
        mv = float(pos.get("market_value", 0) or 0)
        # 有时 mv 由 volume * price 计算
        if not mv and vol and price:
            mv = vol * price
        pnl = pos.get("unrealized_pnl", pos.get("float_profit", pos.get("m_dFloatProfit", pos.get("profit_loss"))))
        if pnl is None and vol and cost:
            pnl = mv - (cost * vol)
        pnl = float(pnl or 0)
        pnl_pct = pos.get("pnl_pct", pos.get("profit_loss_ratio"))
        if pnl_pct is None and cost and vol and cost * vol != 0:
            pnl_pct = (pnl / (cost * vol)) * 100
        pnl_pct = float(pnl_pct or 0)
        # 没有数量且没有市值就不是真实持仓；行情里残留的最新价不能让它重新出现。
        if vol <= 0 and mv <= 0:
            continue
        enriched.append(
            {
                "code": code,
                "name": name,
                "volume": vol,
                "cost": cost,
                "price": price,
                "market_value": mv,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            }
        )
    enriched.sort(key=lambda x: x["pnl"], reverse=True)

    lines = []
    if enriched:
        lines.append("| 排名 | 代码 | 持仓量 | 成本价 | 现价 | 市值 | 盈亏 | 盈亏% |")
        lines.append("|------|------|--------|--------|------|------|------|-------|")
        for i, item in enumerate(enriched, 1):
            lines.append(
                f"| {i} | {item['code']} | {item['volume']} | {item['cost']:.2f} | "
                f"{item['price']:.2f} | {item['market_value']:,.2f} | "
                f"{item['pnl']:+,.2f} | {item['pnl_pct']:+.2f}% |"
            )
    return lines, enriched


def build_trading_summary_report(ts: Dict) -> str:
    """生成实盘交易摘要 (Markdown 格式)。"""
    lines = []
    lines.append("## 实盘交易摘要\n")

    accounts = _resolve_accounts(ts)
    if accounts:
        lines.append("### 账户概况")
        # 支持两种结构: {"main": {...}, "trade": {...}} 或 {"guojin": {...}, "dongguan": {...}}
        for src, acct in accounts.items():
            if not isinstance(acct, dict):
                continue
            total = float(acct.get("total_asset", 0) or 0)
            avail = float(acct.get("available", acct.get("cash", 0)) or 0)
            frozen = float(acct.get("frozen", 0) or 0)
            mv = float(acct.get("market_value", 0) or 0)
            if not total:
                continue
            lines.append(f"**{src}**")
            lines.append(f"- 总资产: {total:,.2f}")
            lines.append(f"- 可用资金: {avail:,.2f}")
            lines.append(f"- 冻结资金: {frozen:,.2f}")
            lines.append(f"- 持仓市值: {mv:,.2f}")
            lines.append("")

    trades = _valid_trades(ts)
    buys = [item for item in trades if item.get("order_type", 0) in (23, "buy", "BUY")]
    sells = [item for item in trades if item not in buys]
    buy_amount = sum(float(item.get("trade_amount", item.get("deal_amount", 0)) or 0) or float(item.get("trade_volume", item.get("deal_volume", 0)) or 0) * float(item.get("trade_price", item.get("deal_price", 0)) or 0) for item in buys)
    sell_amount = sum(float(item.get("trade_amount", item.get("deal_amount", 0)) or 0) or float(item.get("trade_volume", item.get("deal_volume", 0)) or 0) * float(item.get("trade_price", item.get("deal_price", 0)) or 0) for item in sells)
    lines.append("### 今日交易活动")
    lines.append(f"- 已验证成交: {len(trades)} 笔")
    lines.append(f"- 买入 {len(buys)} 笔 / {buy_amount:,.2f} 元")
    lines.append(f"- 卖出 {len(sells)} 笔 / {sell_amount:,.2f} 元")
    net = buy_amount - sell_amount
    direction = "净买入" if net > 0 else "净卖出" if net < 0 else "持平"
    lines.append(f"- 净额: {abs(net):,.2f} ({direction})")
    lines.append("")

    if trades:
        lines.append("#### 成交明细")
        lines.append("| 代码 | 方向 | 成交量 | 成交价 | 成交额 |")
        lines.append("|------|------|--------|--------|--------|")
        for item in trades[:20]:
            code = item.get("stock_code", item.get("code", ""))
            otype = item.get("order_type", 0)
            direction_label = "买" if otype in (23, "buy", "BUY") else "卖"
            vol = item.get("trade_volume", item.get("deal_volume", 0)) or 0
            price = item.get("trade_price", item.get("deal_price", 0)) or 0
            amt = item.get("trade_amount", item.get("deal_amount", 0)) or 0
            lines.append(f"| {code} | {direction_label} | {vol} | {price} | {float(amt):,.2f} |")
        lines.append("")

    positions = _resolve_positions(ts)
    total_unrealized_pnl = ts.get("total_unrealized_pnl", 0)
    total_market_value = ts.get("total_market_value", 0)

    lines.append("### 持仓盈亏排名")
    pos_table_lines, enriched = _build_positions_table(positions)
    if enriched:
        total_market_value = total_market_value or sum(item["market_value"] for item in enriched)
        # Position-level QMT float_profit is authoritative.  Older snapshots may
        # contain the historical "market_value minus zero cost" aggregation bug.
        total_unrealized_pnl = sum(item["pnl"] for item in enriched)
        lines.append(f"总持仓市值: {total_market_value:,.2f}")
        lines.append(f"总未实现盈亏: {total_unrealized_pnl:+,.2f}")
        # 计算盈亏分布
        winners = [item for item in enriched if item["pnl"] > 0]
        losers = [item for item in enriched if item["pnl"] < 0]
        lines.append(
            f"盈利 {len(winners)}/{len(enriched)} 只 (盈利总额 {sum(item['pnl'] for item in winners):+,.2f}), "
            f"亏损 {len(losers)}/{len(enriched)} 只 (亏损总额 {sum(item['pnl'] for item in losers):+,.2f})"
        )
        lines.append("")
        lines.extend(pos_table_lines)
    else:
        reported_count = int(ts.get("positions_count", 0) or 0)
        lines.append(f"总持仓市值: {total_market_value:,.2f}")
        lines.append(f"总未实现盈亏: {total_unrealized_pnl:,.2f}")
        lines.append("")
        if reported_count > 0:
            lines.append("(持仓明细缺失：仅有聚合计数，缺少可用字段)")
        else:
            lines.append("(当前无持仓)")

    orders = _resolve_orders(ts)
    pending = [item for item in orders if item.get("order_status") not in (3, 6, 7) and float(item.get("order_volume", 0) or 0) > 0 and float(item.get("order_price", 0) or 0) > 0]
    if pending:
        lines.append("\n### 待成交委托")
        lines.append("| 代码 | 方向 | 委托量 | 委托价 |")
        lines.append("|------|------|--------|--------|")
        for item in pending:
            code = item.get("stock_code", item.get("code", ""))
            direction_label = "买" if item.get("order_type", 0) in (23, "buy", "BUY") else "卖"
            vol = item.get("order_volume", 0)
            price = item.get("order_price", 0)
            lines.append(f"| {code} | {direction_label} | {vol} | {price} |")
        lines.append("")

    return "\n".join(lines)


def _decision_monitor_attribution(trading_summary: Dict) -> str:
    """Compare scheduled risk recommendations with same-day executed trades."""
    if not DECISION_MONITOR_PATH.exists():
        return "## 盘中建议闭环\n- 当日没有保存的盘中建议快照。"
    try:
        monitor = json.loads(DECISION_MONITOR_PATH.read_text(encoding="utf-8"))
    except Exception:
        return "## 盘中建议闭环\n- 建议快照格式异常，本次不用于执行归因。"

    generated_at = str(monitor.get("generated_at") or "")
    today = datetime.now().strftime("%Y-%m-%d")
    if not generated_at.startswith(today):
        return f"## 盘中建议闭环\n- 最新建议快照为 {generated_at or 'unknown'}，非当日，不用于本次执行归因。"

    reduce_states = {"reduce_priority", "reduce_candidate", "reduce_candidate_no_intraday_change", "hold_or_reduce", "hold_or_reduce_no_intraday_change"}
    recommended = [
        str(item.get("code") or "")
        for item in (monitor.get("tracked_positions") or [])
        if str(item.get("decision_state") or "") in reduce_states and item.get("code")
    ]
    trade_codes = {
        str(item.get("stock_code") or item.get("code") or "")
        for item in (_valid_trades(trading_summary) or [])
        if isinstance(item, dict)
    }
    matched = [code for code in recommended if code in trade_codes]
    pending = [code for code in recommended if code not in trade_codes]
    slot = str(monitor.get("slot") or "").replace(":", "")
    slot_label = {
        "0930": "09:30 开盘预测",
        "0935": "09:35 风险检查",
        "1030": "10:30 走势修正",
        "1430": "14:30 仓位决策",
    }.get(slot, "盘中检查")
    lines = ["## 盘中建议闭环", f"- 建议时间：{generated_at}；时点：{slot_label}"]
    lines.append(f"- 风险处理建议：{', '.join(recommended) if recommended else '无'}")
    lines.append(f"- 当日成交代码：{', '.join(sorted(code for code in trade_codes if code)) if trade_codes else '无'}")
    lines.append(f"- 已有成交证据：{', '.join(matched) if matched else '无'}")
    lines.append(f"- 尚无成交证据：{', '.join(pending) if pending else '无'}")
    return "\n".join(lines)


def _reflection_freshness(as_of: str, generated_at: str) -> tuple[bool, str]:
    data_date = str(as_of or "").strip()[:10]
    report_date = str(generated_at or "").strip()[:10]
    if not data_date:
        return False, "未取得有效数据日期，持仓与成交只能视为来源不明的历史快照。"
    try:
        data_day = datetime.strptime(data_date, "%Y-%m-%d").date()
        report_day = datetime.strptime(report_date, "%Y-%m-%d").date()
    except ValueError:
        return False, "数据日期格式异常，持仓与成交不能视为当前状态。"
    age_days = (report_day - data_day).days
    if age_days == 0:
        return True, "数据日与报告生成日一致。"
    if age_days > 0:
        return False, f"数据滞后 {age_days} 天；持仓与成交仅代表 {data_date} 快照，不可视为当前状态。"
    return False, f"数据日期晚于报告生成日 {abs(age_days)} 天，日期口径冲突，本次仅作排障参考。"


def format_reflection_push_text(trading_summary: Dict, decision_attribution: str, backtest_result: Dict, generated_at: str, as_of: str) -> str:
    from domain.services.report_style_service import money, source_label

    trades = _valid_trades(trading_summary)
    raw_trades = _resolve_trades(trading_summary)
    _, positions = _build_positions_table(_resolve_positions(trading_summary))
    total_pnl = sum(float(item.get("pnl") or 0) for item in positions)
    is_fresh, freshness_note = _reflection_freshness(as_of, generated_at)
    position_scope = "当前" if is_fresh else "数据快照中"
    lines = [
        "📝 每日交易复盘",
        f"数据日：{as_of or '未知'}",
        "",
        "**核心结论**",
        f"- 已验证成交 {len(trades)} 笔；{position_scope}有 {len(positions)} 只有效持仓，组合浮动盈亏 {money(total_pnl)}。",
    ]
    if len(raw_trades) > len(trades):
        lines.append(f"- 有 {len(raw_trades) - len(trades)} 条成交记录缺少正数成交量或成交价，已排除，不计入交易完成。")
    if total_pnl < 0:
        lines.append("- 组合处于浮亏，下一交易日先控制回撤和集中度，不以热点补仓替代风控。")

    lines.extend(["", "**有效成交**"])
    if not trades:
        lines.append("- 未取得可验证成交；不根据委托或零值记录推断已成交。")
    for item in trades[:8]:
        side = "买入" if item.get("order_type", 0) in (23, "buy", "BUY") else "卖出"
        code = item.get("stock_code", item.get("code", ""))
        volume = float(item.get("trade_volume", item.get("deal_volume", 0)) or 0)
        price = float(item.get("trade_price", item.get("deal_price", 0)) or 0)
        lines.append(f"- {side} **{code}**｜{volume:g} 股｜{price:.3f} 元")

    lines.extend(["", "**持仓风险**"])
    if not positions:
        lines.append("- 当前没有有效持仓明细。")
    for item in sorted(positions, key=lambda row: abs(float(row.get("pnl") or 0)), reverse=True)[:6]:
        lines.append(f"- **{item.get('name')}（{item.get('code')}）**｜市值 {money(item.get('market_value'))}｜盈亏 {money(item.get('pnl'))}｜{float(item.get('pnl_pct') or 0):+.1f}%")

    attribution_lines = [line for line in str(decision_attribution or "").splitlines() if line.strip() and not line.startswith("##")]
    lines.extend(["", "**建议闭环**", *(attribution_lines or ["- 当日没有可用的盘中建议快照。"])])
    checked = int(backtest_result.get("checked", 0) or 0)
    lines.extend(["", "**预测复盘**"])
    if checked:
        lines.append(f"- 已验证 {checked} 条，正确 {int(backtest_result.get('correct', 0) or 0)} 条，胜率 {float(backtest_result.get('win_rate', 0) or 0):.1f}%。")
    else:
        lines.append("- 今日没有完成验证的预测样本，不输出胜率结论。")
    lines.extend(
        [
            "",
            "**数据可信度**",
            f"- {freshness_note}",
            f"- 生成时间：{generated_at}；仅正数成交量和成交价的记录计为有效成交。",
        ]
    )
    return "\n".join(lines)


def _build_prediction_breakdown_table(backtest_result: Dict) -> str:
    """生成预测偏差分析表，从 prediction_log 拉取最近已回测预测。"""
    import db as db_mod

    bt = backtest_result or {}
    if not bt.get("checked"):
        return ""

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    checked = db_mod.get_checked_predictions_in_range(start_date, end_date)

    if not checked:
        return ""

    lines = ["### 预测偏差分析", ""]

    # 按目标分组
    by_target = {}
    for pred in checked:
        target_name = pred.get("target_name", pred.get("target", "?"))
        by_target.setdefault(target_name, []).append(pred)

    lines.append("#### 按标的统计")
    lines.append("| 标的 | 预测数 | 正确 | 胜率 | 平均得分 | 平均偏差 |")
    lines.append("|------|--------|------|------|----------|----------|")
    for target_name, preds in sorted(by_target.items()):
        total = len(preds)
        correct = sum(1 for p in preds if p.get("is_correct"))
        win_rate = correct / total * 100 if total else 0
        avg_score = sum(p.get("score", 0) or 0 for p in preds) / total if total else 0
        avg_deviation = sum(abs(p.get("predicted_change", 0) or 0 - (p.get("actual_change", 0) or 0)) for p in preds) / total if total else 0
        lines.append(f"| {target_name} | {total} | {correct} | {win_rate:.0f}% | {avg_score:.0f} | {avg_deviation:+.2f}% |")
    lines.append("")

    # 按策略分组
    by_strategy = {}
    for pred in checked:
        strategy = pred.get("strategy_used", "unknown") or "unknown"
        by_strategy.setdefault(strategy, []).append(pred)

    if len(by_strategy) > 1:
        lines.append("#### 按策略统计")
        lines.append("| 策略 | 预测数 | 正确 | 胜率 | 平均得分 |")
        lines.append("|------|--------|------|------|----------|")
        for strategy, preds in sorted(by_strategy.items(), key=lambda x: -len(x[1])):
            total = len(preds)
            correct = sum(1 for p in preds if p.get("is_correct"))
            win_rate = correct / total * 100 if total else 0
            avg_score = sum(p.get("score", 0) or 0 for p in preds) / total if total else 0
            lines.append(f"| {strategy} | {total} | {correct} | {win_rate:.0f}% | {avg_score:.0f} |")
        lines.append("")

    # 连胜/连败检测
    sorted_preds = sorted(checked, key=lambda p: p.get("created_at", ""))
    current_streak = 0
    streak_type = None
    for pred in reversed(sorted_preds):
        is_correct = pred.get("is_correct")
        if streak_type is None:
            streak_type = "win" if is_correct else "loss"
            current_streak = 1
        elif (is_correct and streak_type == "win") or (not is_correct and streak_type == "loss"):
            current_streak += 1
        else:
            break

    if current_streak >= 2:
        label = "连胜" if streak_type == "win" else "连败"
        lines.append(f"**当前{label}: {current_streak} 次**")
        if streak_type == "loss" and current_streak >= 3:
            lines.append("⚠️ 连续预测失败次数较多，建议审查当前策略适用性")
        lines.append("")

    return "\n".join(lines)


def daily_reflection() -> Dict:
    """每日反思：回测 + 实盘交易摘要 + 预测偏差分析。"""
    print(f"🌅 每日反思任务 [{datetime.now().strftime('%Y-%m-%d %H:%M')}]")
    print("  💼 获取反思上下文...")
    reflection_context = load_reflection_context()
    trading_summary = _build_reflection_trading_summary(reflection_context)
    print(f"  📦 反思上下文来源: {reflection_context.get('source', 'unknown')}")
    if reflection_context.get("as_of_date"):
        print(f"  📅 数据日期: {reflection_context.get('as_of_date')}")

    trading_report = build_trading_summary_report(trading_summary)
    decision_attribution = _decision_monitor_attribution(trading_summary)
    positions = _resolve_positions(trading_summary)
    pos_count = trading_summary.get("positions_count", len(positions))
    print(
        f"  ✅ 交易摘要: 持仓{pos_count}只, "
        f"盈亏{trading_summary.get('total_unrealized_pnl', 0)}, "
        f"成交{trading_summary.get('today_trade_count', 0)}笔"
    )

    bt_result = backtest_predictions()
    prediction_breakdown = _build_prediction_breakdown_table(bt_result)
    full_report = {
        "timestamp": datetime.now().isoformat(),
        "context_source": reflection_context.get("source", "unknown"),
        "trading_summary": trading_summary,
        "trading_report": trading_report,
        "decision_attribution": decision_attribution,
        "backtest_result": bt_result,
        "prediction_breakdown": prediction_breakdown,
    }

    reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "reflection_reports")
    os.makedirs(reports_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")

    summary_path = os.path.join(reports_dir, f"trading_summary_{date_str}.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(trading_summary, f, ensure_ascii=False, indent=2, default=str)

    report_path = os.path.join(reports_dir, f"reflection_{date_str}.md")
    md_lines = [
        "# 每日反思报告",
        f"**生成时间：** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**数据日期：** {reflection_context.get('as_of_date', datetime.now().strftime('%Y-%m-%d'))}",
        "",
        trading_report,
        "",
        decision_attribution,
        "",
        "## 预测回测结果",
        f"- 已检查: {bt_result.get('checked', 0)} 条",
        f"- 正确: {bt_result.get('correct', 0)} 条",
        f"- 胜率: {bt_result.get('win_rate', 0):.1f}%",
    ]
    if prediction_breakdown:
        md_lines.append("")
        md_lines.append(prediction_breakdown)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    full_report["report_path"] = report_path
    full_report["summary_path"] = summary_path
    full_report["text"] = format_reflection_push_text(
        trading_summary,
        decision_attribution,
        bt_result,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        str(reflection_context.get("as_of_date") or ""),
    )

    today = datetime.now()
    if today.weekday() == 6:
        print("\n📊 触发周度归因分析...")
        from domain.services.reflection_service import weekly_attribution as weekly_attribution_service
        weekly_attribution_service()
    if today.day == 1:
        print("\n📋 触发月度策略审计...")
        from domain.services.reflection_service import monthly_audit as monthly_audit_service
        monthly_audit_service()
    return full_report

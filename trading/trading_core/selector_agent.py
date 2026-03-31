from datetime import datetime
from pathlib import Path
from typing import Dict, List

from .budget_agent import allocate_t_budget
from .concept_agent import build_concept_snapshots, get_weekly_hot_concepts, summarize_hot_concepts
from .execution import TradingExecutionBook
from .models import MarketRegimeReport, SelectionResult, StockDataSnapshot, StockNewsSnapshot, StockRule
from .paths import BASE_DIR, FOCUS_LIST_PATH, UNIVERSE_STATE_PATH
from .storage import atomic_write_json, load_daily_plan, load_json, save_daily_plan


def _item_brief(item: Dict) -> str:
    title = str(item.get("title", "")).strip()
    source = str(item.get("source", "")).strip()
    published_at = str(item.get("published_at", "")).strip()
    parts = [title]
    meta = " ".join(part for part in [source, published_at] if part)
    if meta:
        parts.append(f"({meta})")
    return " ".join(part for part in parts if part)


def _selection_review_path(day: str = "") -> Path:
    value = day or datetime.now().strftime("%Y-%m-%d")
    return BASE_DIR / "trading_data" / f"selection_review_{value}.json"


def _remaining_cash_budget(
    floating_cash_budget: float,
    data_snapshots: Dict[str, StockDataSnapshot],
    base_dir: Path = BASE_DIR,
) -> float:
    budget = max(0.0, float(floating_cash_budget or 0.0))
    if budget <= 0:
        return 0.0
    execution_book = TradingExecutionBook(base_dir=base_dir)
    portfolio = execution_book.load_portfolio_state()
    occupied = 0.0
    for code, item in portfolio.get("stocks", {}).items():
        quote = data_snapshots.get(code)
        if not quote or quote.price <= 0:
            continue
        intraday_buy = int(item.get("intraday_buy", 0) or 0)
        intraday_sell = int(item.get("intraday_sell", 0) or 0)
        net_buy_qty = max(0, intraday_buy - intraday_sell)
        occupied += net_buy_qty * float(quote.price)
    pending_statuses = {"pending", "acknowledged", "submitted"}
    for command in execution_book.load_command_book().get("commands", []):
        if str(command.get("action", "")).lower() != "buy":
            continue
        if str(command.get("status", "")).lower() not in pending_statuses:
            continue
        quantity = int(command.get("quantity", 0) or 0)
        if quantity <= 0:
            continue
        price = float(command.get("price", 0) or 0)
        if price <= 0:
            code = str(command.get("stock_code", "")).strip()
            quote = data_snapshots.get(code)
            if not quote or quote.price <= 0:
                continue
            price = float(quote.price)
        occupied += quantity * price
    return round(max(0.0, budget - occupied), 2)


def _level(score: int) -> str:
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 45:
        return "C"
    return "D"


def _ratio_for_score(score: int, regime: str) -> float:
    if score < 45:
        return 0.0
    if regime == "panic":
        return 0.25 if score >= 70 else 0.1
    if regime == "weak":
        return 0.25 if score >= 65 else 0.15
    if regime == "strong":
        return 0.5 if score >= 70 else 0.25
    return 0.4 if score >= 70 else 0.2


def _event_weight(rule: StockRule, tag: Dict) -> int:
    strategy = rule.strategy or ""
    tag_name = tag.get("tag", "")
    direction = tag.get("direction", "neutral")
    base_weight = 0
    if tag_name == "war_risk":
        if "逆T" in strategy or rule.allow_market_panic_reverse_t:
            base_weight = -6 if direction == "negative" else 0
        else:
            base_weight = -8 if direction == "negative" else 0
    elif tag_name == "policy_support":
        if "顺T" in strategy or "箱体" in strategy:
            base_weight = 8 if direction == "positive" else 0
        else:
            base_weight = 4 if direction == "positive" else 0
    elif tag_name == "earnings_pressure":
        base_weight = -10 if direction == "negative" else 0
    elif tag_name == "order_growth":
        base_weight = 6 if direction == "positive" else 0
    elif tag_name == "sector_rotation":
        base_weight = 5 if direction == "positive" else -3
    if not base_weight:
        return 0
    stage = str(tag.get("stage", "")).strip()
    stage_multiplier = 1.0
    if stage == "active":
        stage_multiplier = 0.7
    elif stage == "cooling":
        stage_multiplier = 0.35
    elif stage == "stale":
        stage_multiplier = 0.0
    explicit_weight = float(tag.get("weight", stage_multiplier) or 0)
    return int(round(base_weight * explicit_weight))


def _decide_halfday_modes(rule: StockRule, market_regime: MarketRegimeReport, news: StockNewsSnapshot, score: int) -> tuple[str, str]:
    strategy = rule.strategy or ""
    if score < 45:
        return "observe_only", "observe_only"
    if market_regime.regime == "panic":
        if "逆T" in strategy or rule.allow_market_panic_reverse_t:
            return "reverse_t_only", "sell_only"
        return "sell_only", "sell_only"
    if news.stock_sentiment == "negative" and news.sector_sentiment == "negative":
        return "sell_only", "observe_only"
    if any(tag["tag"] == "war_risk" and tag["direction"] == "negative" for tag in news.event_tags):
        return "sell_only", "observe_only"
    if any(tag["tag"] == "policy_support" and tag["direction"] == "positive" for tag in news.event_tags):
        return "trend_t_only", "trend_t_only"
    if "顺T" in strategy:
        return "trend_t_only", "box_t_only"
    if "逆T" in strategy:
        return "reverse_t_only", "observe_only"
    if "箱体" in strategy:
        return "box_t_only", "box_t_only"
    return "observe_only", "observe_only"


def select_focus_list(
    rules: List[StockRule],
    data_snapshots: Dict[str, StockDataSnapshot],
    news_snapshots: Dict[str, StockNewsSnapshot],
    market_regime: MarketRegimeReport,
    floating_cash_budget: float = 100000.0,
    max_single_t_ratio_of_holding: float = 0.35,
    now: datetime | None = None,
) -> List[SelectionResult]:
    results: List[SelectionResult] = []
    concept_snapshots = build_concept_snapshots(rules, now=now)
    holding_values = {
        rule.code: float(rule.base_position) * float(data_snapshots[rule.code].price)
        for rule in rules
        if rule.code in data_snapshots
    }
    for rule in rules:
        data = data_snapshots[rule.code]
        news = news_snapshots[rule.code]
        concept = concept_snapshots.get(rule.code, {})
        score = 35
        reasons = []
        if data.amplitude_pct >= 4.0:
            score += 18
            reasons.append(f"振幅{data.amplitude_pct:.1f}%适合做T")
        elif data.amplitude_pct >= 2.0:
            score += 10
            reasons.append(f"振幅{data.amplitude_pct:.1f}%可做T")
        else:
            score -= 12
            reasons.append("振幅偏小")
        if data.main_net_inflow > 0:
            score += 10
            reasons.append(f"资金流入{data.main_net_inflow:.0f}万")
        elif data.main_net_inflow < 0:
            score -= 8
            reasons.append(f"资金流出{abs(data.main_net_inflow):.0f}万")
        score += news.stock_score * 3
        score += news.sector_score * 2
        score += news.macro_score
        for tag in news.event_tags:
            weighted = _event_weight(rule, tag)
            if weighted:
                score += weighted
                reasons.append(f"事件{tag.get('tag')}[{tag.get('stage', 'fresh')}]影响{weighted:+d}")
        if news.stock_sentiment == "positive":
            reasons.append("个股消息偏正面")
        elif news.stock_sentiment == "negative":
            reasons.append("个股消息偏负面")
        if news.sector_sentiment == "positive":
            reasons.append("板块消息偏正面")
        elif news.sector_sentiment == "negative":
            reasons.append("板块消息偏负面")
        if news.macro_sentiment == "positive":
            reasons.append("宏观事件偏正面")
        elif news.macro_sentiment == "negative":
            reasons.append("宏观事件偏负面")
        if news.overseas_peer_score:
            score += news.overseas_peer_score
            reasons.append(news.overseas_peer_block_reason or f"外盘同行压力{news.overseas_peer_score:+d}")
        if news.overseas_peer_sentiment == "negative":
            reasons.append("外盘同行偏弱")
        hot_matches = concept.get("hot_matches", [])
        if hot_matches:
            concept_weight = min(len(hot_matches), 3) * 6
            score += concept_weight
            reasons.append(f"命中热门题材{'/'.join(hot_matches[:3])}")
        elif concept.get("stock_concepts"):
            score -= 2
            reasons.append("未命中当日热门题材")
        hot_match_ranks = concept.get("hot_match_ranks", [])
        if hot_match_ranks:
            best_rank = min(int(item.get("rank", 99) or 99) for item in hot_match_ranks)
            if best_rank <= 3:
                score += 8
                reasons.append(f"命中前3热题材第{best_rank}名")
            elif best_rank <= 5:
                score += 5
                reasons.append(f"命中前5热题材第{best_rank}名")
            elif best_rank <= 10:
                score += 2
                reasons.append(f"命中前10热题材第{best_rank}名")
            best_stage = str(hot_match_ranks[0].get("stage", "")).strip()
            best_days = int(hot_match_ranks[0].get("days", 0) or 0)
            if best_stage == "fresh":
                score += 6
                reasons.append(f"题材处于起爆期{best_days}天")
            elif best_stage == "active":
                score += 3
                reasons.append(f"题材处于活跃期{best_days}天")
            elif best_stage == "cooling":
                score -= 4
                reasons.append(f"题材进入降温期{best_days}天")
            elif best_stage == "stale":
                score -= 8
                reasons.append(f"题材偏老化{best_days}天")
        hot_match_days = int(concept.get("hot_match_days", 0) or 0)
        if hot_match_days >= 2:
            persistence_weight = min(hot_match_days, 4) * 2
            score += persistence_weight
            reasons.append(f"近7天连续命中热题材{hot_match_days}天")
        if "顺T" in rule.strategy and market_regime.regime == "strong":
            score += 8
            reasons.append("顺T策略匹配强/震荡市")
        if "逆T" in rule.strategy and market_regime.allow_reverse_t:
            score += 12
            reasons.append("逆T策略匹配恐慌反抽")
        if market_regime.regime == "panic" and "顺T" in rule.strategy:
            score -= 10
            reasons.append("恐慌市顺T降级")
        score = max(0, min(score, 100))
        ratio = _ratio_for_score(score, market_regime.regime)
        suggested_shares = int(rule.per_trade_shares * ratio / 0.25) if ratio > 0 else 0
        am_mode, pm_mode = _decide_halfday_modes(rule, market_regime, news, score)
        buy_blocked = bool(rule.buy_blocked or news.overseas_peer_block_buy)
        buy_block_reason = str(rule.buy_block_reason or news.overseas_peer_block_reason or "").strip()
        if buy_blocked:
            am_mode = "sell_only"
            pm_mode = "observe_only" if pm_mode != "sell_only" else pm_mode
        if ratio >= 0.25:
            action = "focus"
        elif ratio > 0:
            action = "watch"
        else:
            action = "avoid"
        if buy_blocked and action == "focus":
            action = "watch"
        explanation_parts = []
        if news.event_tags:
            explanation_parts.append(
                "事件标签: " + ",".join(
                    f"{tag['tag']}:{tag['direction']}/{tag['horizon']}/{tag.get('stage', 'fresh')}/{tag.get('age_hours', 0)}h"
                    for tag in news.event_tags[:3]
                )
            )
        if news.stock_items:
            explanation_parts.append(f"个股: {_item_brief(news.stock_items[0])}")
        if news.sector_items:
            explanation_parts.append(f"板块: {_item_brief(news.sector_items[0])}")
        if news.macro_items:
            explanation_parts.append(f"宏观: {_item_brief(news.macro_items[0])}")
        if news.overseas_peer_items:
            first_peer = news.overseas_peer_items[0]
            explanation_parts.append(f"外盘同行: {_item_brief(first_peer)}")
            if news.overseas_peer_block_reason:
                explanation_parts.append(f"外盘风控: {news.overseas_peer_block_reason}")
        if hot_matches:
            explanation_parts.append("热门题材: " + ",".join(hot_matches[:3]))
        elif concept.get("top_hot_concepts"):
            explanation_parts.append("市场热题材: " + ",".join(concept.get("top_hot_concepts", [])[:3]))
        if hot_match_ranks:
            explanation_parts.append(
                "题材排名: " + ",".join(
                    f"{item.get('concept')}#{item.get('rank')}:{item.get('stage')}/{item.get('days')}d"
                    for item in hot_match_ranks[:3]
                )
            )
        if concept.get("recent_hot_matches"):
            latest_match = concept.get("recent_hot_matches", [])[0]
            explanation_parts.append(
                f"近7天题材命中: {latest_match.get('date', '')} {'/'.join(latest_match.get('concepts', [])[:3])}"
            )
        final_reason = " / ".join(reasons[:5])
        if buy_blocked and buy_block_reason and buy_block_reason not in final_reason:
            final_reason = f"{buy_block_reason} / {final_reason}" if final_reason else buy_block_reason
        results.append(SelectionResult(
            code=rule.code,
            name=rule.name,
            action=action,
            am_mode=am_mode,
            pm_mode=pm_mode,
            score=score,
            level=_level(score),
            buy_budget_amount=0.0,
            sell_budget_amount=0.0,
            suggested_t_ratio=round(ratio, 2),
            suggested_buy_shares=max(0, suggested_shares),
            suggested_sell_shares=max(0, suggested_shares),
            reason=final_reason,
            explanation=" | ".join(explanation_parts[:3]),
            regime=market_regime.regime,
            buy_blocked=buy_blocked,
            buy_block_reason=buy_block_reason,
        ))
    ranked = sorted(results, key=lambda item: item.score, reverse=True)
    remaining_budget = _remaining_cash_budget(floating_cash_budget, data_snapshots)
    return allocate_t_budget(
        ranked,
        data_snapshots,
        holding_values,
        floating_cash_budget=remaining_budget,
        max_single_t_ratio_of_holding=max_single_t_ratio_of_holding,
    )


def save_selection_outputs(results: List[SelectionResult], now: datetime | None = None) -> None:
    now_dt = now or datetime.now()
    now_text = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    weekly_hot_concepts = get_weekly_hot_concepts(now=now_dt)
    concept_summary = summarize_hot_concepts(weekly_hot_concepts)
    focus_payload = {
        "updated_at": now_text,
        "focus": [item.__dict__ for item in results if item.action == "focus"][:3],
        "watch": [item.__dict__ for item in results if item.action == "watch"][:5],
        "avoid": [item.__dict__ for item in results if item.action == "avoid"][:10],
    }
    universe_payload = {
        "updated_at": now_text,
        "stocks": [item.__dict__ for item in results],
    }
    atomic_write_json(FOCUS_LIST_PATH, focus_payload)
    atomic_write_json(UNIVERSE_STATE_PATH, universe_payload)
    review_payload = {
        "updated_at": now_text,
        "latest_hot_concepts_date": concept_summary.get("latest_date", ""),
        "latest_hot_concepts": [
            {
                "name": item.get("name", ""),
                "code": item.get("code", ""),
                "rank": index,
            }
            for index, item in enumerate(concept_summary.get("latest_hot_concepts", [])[:10], start=1)
        ],
        "weekly_hot_concepts": {
            date_str: [str(item.get("name", "")).strip() for item in items[:5] if str(item.get("name", "")).strip()]
            for date_str, items in weekly_hot_concepts.items()
        },
        "focus": [item.__dict__ for item in results if item.action == "focus"][:5],
        "watch": [item.__dict__ for item in results if item.action == "watch"][:8],
        "avoid": [item.__dict__ for item in results if item.action == "avoid"][:12],
        "peer_risk": [
            {
                "code": item.code,
                "name": item.name,
                "buy_blocked": item.buy_blocked,
                "buy_block_reason": item.buy_block_reason,
            }
            for item in results
            if item.buy_blocked
        ],
    }
    atomic_write_json(_selection_review_path(), review_payload)


def sync_selection_to_daily_plan(rules: List[StockRule], results: List[SelectionResult]) -> Dict:
    plan = load_daily_plan()
    by_code = {item.code: item for item in results}
    output = []
    for rule in rules:
        selection = by_code.get(rule.code)
        if not selection:
            continue
        watch_mode = ""
        enabled = rule.enabled
        if selection.action == "watch":
            watch_mode = "light"
        if selection.action == "avoid":
            watch_mode = "light"
        output.append({
            "code": rule.code,
            "name": rule.name,
            "enabled": enabled,
            "per_trade_shares": max(selection.suggested_buy_shares, selection.suggested_sell_shares, rule.per_trade_shares),
            "watch_mode": watch_mode,
            "buy_blocked": selection.buy_blocked,
            "buy_block_reason": selection.buy_block_reason,
            "avoid_reverse_t": selection.buy_blocked,
            "selection_action": selection.action,
            "selection_am_mode": selection.am_mode,
            "selection_pm_mode": selection.pm_mode,
            "selection_score": selection.score,
            "selection_buy_budget_amount": selection.buy_budget_amount,
            "selection_sell_budget_amount": selection.sell_budget_amount,
            "selection_ratio": selection.suggested_t_ratio,
            "selection_buy_shares": selection.suggested_buy_shares,
            "selection_sell_shares": selection.suggested_sell_shares,
            "selection_buy_blocked": selection.buy_blocked,
            "selection_buy_block_reason": selection.buy_block_reason,
            "selection_reason": selection.reason,
            "selection_explanation": selection.explanation,
            "note": f"selector:{selection.action}/{selection.am_mode}->{selection.pm_mode} {selection.reason}",
        })
    plan["stocks"] = output
    save_daily_plan(plan, source="selector-preopen")
    return plan


def load_selection_map() -> Dict[str, Dict]:
    data = load_json(UNIVERSE_STATE_PATH, {"updated_at": "", "stocks": []})
    result = {}
    for item in data.get("stocks", []):
        code = str(item.get("code", "")).strip()
        if code:
            result[code] = item
    return result


def _short_reason(text: str, limit: int = 18) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def format_selection_report(results: List[SelectionResult], title: str = "盘前持仓做T筛选") -> str:
    lines = [title]
    focus_count = sum(1 for item in results if item.action == "focus")
    watch_count = sum(1 for item in results if item.action == "watch")
    avoid_count = sum(1 for item in results if item.action == "avoid")
    lines.append(f"重点{focus_count} 观察{watch_count} 回避{avoid_count}")
    lines.append("")
    lines.append("| 代码 | 名称 | 分组 | 模式 | 分数 | 买预算 | 卖预算 | 禁买 | 原因 |")
    lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |")
    for item in results[:10]:
        lines.append(
            f"| {item.code} | {item.name} | {item.action} | {item.am_mode}->{item.pm_mode} | {item.score} | "
            f"{item.buy_budget_amount:.0f} | {item.sell_budget_amount:.0f} | "
            f"{'是' if item.buy_blocked else '否'} | {_short_reason(item.buy_block_reason or item.reason)} |"
        )
    return "\n".join(lines)

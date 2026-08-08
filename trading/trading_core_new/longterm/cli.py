from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
from shared_report_card_renderer import build_feishu_report_card

from .data_source import OpenClawChinaStockDataSource
from .engine import (
    apply_manual_executions,
    build_execution_records_from_plan,
    build_rebalance_plan,
    rebuild_portfolio_from_executions,
    _generate_exit_signals,
    _theme_based_weight_map,
)
from .llm_advisor import build_evening_decision, build_morning_decision, build_quality_improvement_advice
from .llm_runtime import chat_vision_json
from .models import LongTermSettings, ManualExecutionItem, PortfolioState, SimPosition, StockCandidate
from .notifier import push_feishu_rich
from .ninedb import build_trade_orders, load_9db_config, parse_9db_url, save_9db_config, submit_trade_orders
from .industry_normalizer import normalize_industry_name, normalize_industry_with_concepts
from .industry_policy import industry_cap_for, theme_cap_for
from .industry_normalizer import extract_themes
from .post_market_scanner import (
    build_candidates_from_portfolio,
    run_post_market_scan,
)
from .repository import LongTermRepository
from .trading_calendar import is_cn_trading_day, last_trading_day
from .intraday_monitor import IntradayMonitor, MonitorConfig

LONGTERM_MANUAL_COMMAND_RE = re.compile(r"^\s*/?(长线成交|长线执行|长线回填|线成交)\b", re.IGNORECASE)
LONGTERM_MANUAL_INLINE_RE = re.compile(r"^\s*/?长线\s+(?:\d{4}-\d{2}-\d{2}\s+)?(买|买入|卖|卖出|buy|sell|b|s)\b", re.IGNORECASE)
LONGTERM_MANUAL_HELP_RE = re.compile(r"^\s*/?(长线帮助|长线命令)\b", re.IGNORECASE)
LONGTERM_POSITION_SYNC_RE = re.compile(r"^\s*/?(更新持仓|同步持仓)\b", re.IGNORECASE)
TIME_ONLY_RE = re.compile(r"^\d{2}:\d{2}$")


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _configure_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    level_name = str((os.getenv("LONGTERM_LOG_LEVEL", "INFO")) or "INFO").upper().strip()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.setLevel(level)
    root.addHandler(stream_handler)
    log_candidates = []
    env_log_dir = str(os.getenv("LONGTERM_LOG_DIR", "") or "").strip()
    if env_log_dir:
        log_candidates.append(Path(env_log_dir).expanduser())
    log_candidates.append(Path(__file__).resolve().parents[2] / "trading_data" / "longterm" / "logs")
    log_candidates.append(Path(tempfile.gettempdir()) / "openclaw-longterm" / "logs")

    for log_dir in log_candidates:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_dir / "longterm.log", encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
            return
        except OSError:
            continue
    root.warning("longterm file logger disabled: no writable log directory found")


def _load_manual_file(path: Path) -> List[ManualExecutionItem]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload if isinstance(payload, list) else payload.get("items", [])
    result: List[ManualExecutionItem] = []
    for item in items:
        result.append(ManualExecutionItem(**item))
    return result


def is_feishu_manual_command(text: str) -> bool:
    raw = str(text or "").strip()
    return bool(
        LONGTERM_MANUAL_COMMAND_RE.match(raw)
        or LONGTERM_MANUAL_INLINE_RE.match(raw)
        or LONGTERM_MANUAL_HELP_RE.match(raw)
        or LONGTERM_POSITION_SYNC_RE.match(raw)
    )


def is_feishu_position_sync_command(text: str) -> bool:
    return bool(LONGTERM_POSITION_SYNC_RE.match(str(text or "").strip()))


def build_feishu_manual_command_help() -> str:
    return (
        "长线成交命令格式:\n"
        "/长线成交 [日期] 买入|卖出 代码 数量 价格 [名称] [fee=手续费] [note=备注]\n"
        "/长线 [日期] 买入|卖出 代码 数量 价格 [名称] [fee=手续费] [note=备注]\n"
        "可一条或多条，支持换行或分号分隔。\n"
        "示例:\n"
        "/长线成交 2026-04-28 卖出 688327 500 13.62 云从科技-U fee=2.1 note=减仓\n"
        "/长线 买入 600120 1000 5.69 浙江东方 fee=1.2\n"
        "/长线成交\n"
        "买入 600120 1000 5.69 浙江东方 fee=1.2\n"
        "卖出 688316 200 70 青云科技-U"
    )


def build_feishu_position_sync_help() -> str:
    return (
        "📸 更新持仓 — 上传券商持仓截图，自动同步持仓和现金\n\n"
        "用法: /更新持仓 [截图]  (或 /同步持仓)\n"
        "支持飞书直接上传图片后发命令，或先发命令再上传图片。\n\n"
        "截图应包含:\n"
        "  - 持仓列表（代码/名称/数量/成本价/现价）\n"
        "  - 可用资金 / 总资产\n\n"
        "系统将自动识别并以截图为权威直接覆盖同步长线组合持仓和现金。"
    )


def _is_iso_trade_date(text: str) -> bool:
    try:
        datetime.strptime(str(text).strip(), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _normalize_manual_side(token: str) -> str:
    raw = str(token or "").strip().lower()
    mapping = {
        "买": "buy",
        "买入": "buy",
        "b": "buy",
        "buy": "buy",
        "卖": "sell",
        "卖出": "sell",
        "s": "sell",
        "sell": "sell",
    }
    side = mapping.get(raw, "")
    if not side:
        raise ValueError(f"unknown_side:{token}")
    return side


def _parse_manual_command_record(line: str, *, default_date: str) -> ManualExecutionItem:
    tokens = [str(item).strip() for item in str(line or "").split() if str(item).strip()]
    if not tokens:
        raise ValueError("empty_record")
    trade_date = default_date
    trade_time = ""
    if _is_iso_trade_date(tokens[0]):
        trade_date = tokens.pop(0)
    if tokens and TIME_ONLY_RE.match(tokens[0]):
        trade_time = f"{trade_date} {tokens.pop(0)}"
    if len(tokens) < 4:
        raise ValueError(f"invalid_record:{line}")
    side = _normalize_manual_side(tokens[0])
    code = str(tokens[1]).upper()
    try:
        quantity = int(float(tokens[2]))
    except Exception as exc:
        raise ValueError(f"invalid_quantity:{tokens[2]}") from exc
    try:
        price = float(tokens[3])
    except Exception as exc:
        raise ValueError(f"invalid_price:{tokens[3]}") from exc
    fee = 0.0
    name = ""
    note_parts: List[str] = []
    for token in tokens[4:]:
        lower = token.lower()
        if lower.startswith(("fee=", "f=")):
            fee = float(token.split("=", 1)[1] or 0.0)
            continue
        if token.startswith("手续费="):
            fee = float(token.split("=", 1)[1] or 0.0)
            continue
        if lower.startswith(("note=", "n=")):
            note_parts.append(token.split("=", 1)[1].strip())
            continue
        if token.startswith("备注="):
            note_parts.append(token.split("=", 1)[1].strip())
            continue
        if not name:
            name = token
        else:
            note_parts.append(token)
    if quantity <= 0 or price <= 0:
        raise ValueError(f"non_positive_trade:{line}")
    return ManualExecutionItem(
        trade_date=trade_date,
        code=code,
        name=name or code,
        side=side,
        price=price,
        quantity=quantity,
        trade_time=trade_time,
        fee=fee,
        note=" ".join([x for x in note_parts if str(x).strip()]).strip(),
    )


def parse_feishu_manual_command(text: str, *, default_date: str) -> List[ManualExecutionItem]:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty_command")
    if LONGTERM_MANUAL_HELP_RE.match(raw):
        return []
    if LONGTERM_MANUAL_INLINE_RE.match(raw):
        body = re.sub(r"^\s*/?长线\b", "", raw, count=1).strip()
    else:
        body = LONGTERM_MANUAL_COMMAND_RE.sub("", raw, count=1).strip()
    if not body:
        raise ValueError("empty_command_body")
    records: List[ManualExecutionItem] = []
    for chunk in body.replace("；", "\n").replace(";", "\n").splitlines():
        line = str(chunk).strip()
        if not line:
            continue
        records.append(_parse_manual_command_record(line, default_date=default_date))
    if not records:
        raise ValueError("empty_command_body")
    return records


def _apply_manual_records(
    repo: LongTermRepository,
    *,
    records: List[ManualExecutionItem],
    as_of: str,
    source: str,
) -> dict:
    portfolio = repo.load_portfolio()
    next_state = apply_manual_executions(portfolio, records)
    next_state.as_of = as_of
    repo.append_manual_executions(records)
    repo.save_portfolio(next_state)
    snapshot_path = repo.save_investor_snapshot(_build_investor_snapshot(repo, source=source))
    ninedb_sync = _sync_manual_records_to_9db(repo, records)
    return {
        "records_count": len(records),
        "portfolio": next_state,
        "snapshot_path": snapshot_path,
        "ninedb_sync": ninedb_sync,
    }


def _build_review_plan(repo: LongTermRepository, trade_date: str) -> tuple:
    universe = repo.load_universe()
    portfolio = repo.load_portfolio()
    settings = repo.load_settings()
    data_source = OpenClawChinaStockDataSource()
    code_set = {item.code.upper() for item in universe} | {item.code.upper() for item in portfolio.positions}
    market_index = str(getattr(settings, "market_trend_index", "") or "").strip().upper()
    if market_index:
        code_set.add(market_index)
    codes = sorted(code_set)
    quotes = data_source.fetch_quotes(codes) if codes else {}
    rotation_scan = repo.load_latest_post_market_scan() if bool(getattr(settings, "rotation_mode", False)) else None
    history_by_code_arg = None
    if bool(getattr(settings, "high_tech_mode", False)) and codes:
        history_by_code_arg = data_source.fetch_batch_daily_history(
            codes, lookback_days=int(getattr(settings, "ht_lookback_days", 20) or 20) + 10,
        )
    plan, mtm_portfolio = build_rebalance_plan(
        trade_date=trade_date,
        candidates=universe,
        portfolio=portfolio,
        quotes=quotes,
        settings=settings,
        rotation_scan=rotation_scan,
        history_by_code=history_by_code_arg,
    )
    return plan, mtm_portfolio


def _sync_manual_records_to_9db(repo: LongTermRepository, records: List[ManualExecutionItem]) -> dict:
    filtered = [
        item for item in records
        if str(item.code or "").strip().upper() != "POS_SYNC"
        and int(item.quantity or 0) > 0
        and float(item.price or 0.0) > 0
    ]
    config = load_9db_config(repo.ninedb_config_file)
    if not config:
        return {"enabled": False, "ok": False, "submitted_count": 0, "error": "9db not configured"}
    orders = build_trade_orders(filtered)
    result = submit_trade_orders(config, orders)
    return {
        "enabled": True,
        "ok": bool(result.get("ok")),
        "submitted_count": int(result.get("submitted_count", 0) or 0),
        "error": str(result.get("error", "") or ""),
        "attempts": int(result.get("attempts", 0) or 0),
        "order_name": str(config.get("order_name", "") or ""),
    }


def handle_feishu_manual_command(text: str, repo: LongTermRepository | None = None) -> str:
    _configure_logging()
    raw = str(text or "").strip()
    if LONGTERM_MANUAL_HELP_RE.match(raw):
        return build_feishu_manual_command_help()
    if LONGTERM_POSITION_SYNC_RE.match(raw):
        return build_feishu_position_sync_help()
    repo = repo or LongTermRepository()
    repo.init_if_missing()
    latest_plan = repo.load_latest_plan() or {}
    default_date = str(latest_plan.get("trade_date", "") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    try:
        records = parse_feishu_manual_command(raw, default_date=default_date)
    except Exception as exc:
        return f"长线成交命令解析失败: {exc}\n\n{build_feishu_manual_command_help()}"
    as_of = max([str(item.trade_date or default_date).strip() for item in records] or [default_date])
    result = _apply_manual_records(repo, records=records, as_of=as_of, source="apply-manual-command")
    next_state = result["portfolio"]
    lines = [
        f"长线成交已回填 {int(result['records_count'])} 条",
        f"as_of: {as_of}",
        f"nav: {float(next_state.nav):.2f} cash: {float(next_state.cash):.2f} holdings: {len(next_state.positions)}",
    ]
    for item in records[:8]:
        side_text = "买入" if str(item.side) == "buy" else "卖出"
        note = f" note={item.note}" if str(item.note or "").strip() else ""
        fee = f" fee={float(item.fee):.2f}" if float(item.fee or 0.0) > 0 else ""
        lines.append(
            f"- {item.trade_date} {side_text} {item.code} qty={int(item.quantity)} px={float(item.price):.3f} {item.name}{fee}{note}"
        )
    lines.append(f"investor_snapshot: {result['snapshot_path']}")
    ninedb = result.get("ninedb_sync", {}) or {}
    if ninedb.get("enabled"):
        lines.append(
            f"9db_sync: {'ok' if ninedb.get('ok') else 'failed'} "
            f"submitted={int(ninedb.get('submitted_count', 0) or 0)} "
            f"attempts={int(ninedb.get('attempts', 0) or 0)} "
            f"error={str(ninedb.get('error', '') or '-')}"
        )
    return "\n".join(lines)


def _build_execution_monitor(repo: LongTermRepository, latest_plan: dict | None = None) -> dict:
    latest_plan = latest_plan or repo.load_latest_plan() or {}
    manual = repo.load_manual_executions()
    manual_dates = sorted(
        {str(item.trade_date or "").strip() for item in manual if str(item.trade_date or "").strip()},
        reverse=True,
    )
    latest_manual_trade_date = manual_dates[0] if manual_dates else ""
    latest_plan_trade_date = str(latest_plan.get("trade_date", "") or "").strip()
    latest_plan_actions = list(latest_plan.get("actions", []) or [])
    latest_plan_codes = {
        str(item.get("code", "") or "").strip().upper()
        for item in latest_plan_actions
        if isinstance(item, dict) and str(item.get("code", "") or "").strip()
    }
    manual_for_latest_plan = [
        item for item in manual if str(item.trade_date or "").strip() == latest_plan_trade_date
    ]
    filled_codes = {
        str(item.code or "").strip().upper()
        for item in manual_for_latest_plan
        if str(item.code or "").strip() and int(item.quantity or 0) > 0
    }
    matched_filled_codes = latest_plan_codes & filled_codes
    days_since_latest_manual = None
    if latest_manual_trade_date:
        try:
            delta = datetime.now().date() - datetime.strptime(latest_manual_trade_date, "%Y-%m-%d").date()
            days_since_latest_manual = int(delta.days)
        except ValueError:
            days_since_latest_manual = None
    status = "ok"
    if not manual:
        status = "no_manual_records"
    elif latest_plan_codes and not matched_filled_codes:
        status = "plan_missing_manual"
    elif latest_plan_codes and len(matched_filled_codes) < len(latest_plan_codes):
        status = "plan_partial_manual"
    return {
        "status": status,
        "manual_total_count": len(manual),
        "latest_manual_trade_date": latest_manual_trade_date,
        "days_since_latest_manual": days_since_latest_manual,
        "latest_plan_trade_date": latest_plan_trade_date,
        "latest_plan_actions_count": len(latest_plan_actions),
        "manual_for_latest_plan_count": len(manual_for_latest_plan),
        "filled_codes_count": len(matched_filled_codes),
        "unfilled_plan_actions_count": max(0, len(latest_plan_codes) - len(matched_filled_codes)),
    }


def _execution_monitor_note(execution_monitor: dict) -> str:
    status = str(execution_monitor.get("status", "") or "").strip()
    if status == "no_manual_records":
        return "未发现任何手工成交回填，系统仍停留在建议层。"
    if status == "plan_missing_manual":
        return (
            f"最新计划 {execution_monitor.get('latest_plan_trade_date', '-')}"
            " 没有对应成交回填，执行闭环缺失。"
        )
    if status == "plan_partial_manual":
        return (
            f"最新计划仅回填 {int(execution_monitor.get('filled_codes_count', 0) or 0)}/"
            f"{int(execution_monitor.get('latest_plan_actions_count', 0) or 0)} 个动作。"
        )
    latest_date = str(execution_monitor.get("latest_manual_trade_date", "") or "").strip() or "-"
    return (
        f"最近成交回填日 {latest_date}，"
        f"覆盖 {int(execution_monitor.get('filled_codes_count', 0) or 0)}/"
        f"{int(execution_monitor.get('latest_plan_actions_count', 0) or 0)} 个计划动作。"
    )


def _build_investor_snapshot(repo: LongTermRepository, *, source: str) -> dict:
    portfolio = repo.load_portfolio()
    latest_plan = repo.load_latest_plan() or {}
    latest_scan = repo.load_latest_post_market_scan() or {}
    universe = repo.load_universe()
    settings = repo.load_settings()
    execution_monitor = _build_execution_monitor(repo, latest_plan)
    nav = float(portfolio.nav)
    holdings = sorted(portfolio.positions, key=lambda x: x.market_value, reverse=True)
    industry_lookup = {
        str(item.code).upper(): normalize_industry_name(str(item.industry or "").strip())
        for item in universe
    }
    industry_value: dict = {}
    for item in holdings:
        code = str(item.code).upper()
        industry = industry_lookup.get(code, "UNKNOWN")
        industry_value[industry] = float(industry_value.get(industry, 0.0)) + float(item.market_value)
    industry_rows = []
    for industry, value in sorted(industry_value.items(), key=lambda kv: kv[1], reverse=True):
        weight = (float(value) / nav) if nav > 0 else 0.0
        cap_limit = industry_cap_for(industry, settings)
        industry_rows.append(
            {
                "industry": industry,
                "market_value": round(float(value), 2),
                "weight": round(weight, 6),
                "cap_limit": round(float(cap_limit), 6),
                "over_limit": bool(weight > cap_limit),
            }
        )
    theme_value: dict = {}
    for item in holdings:
        code = str(item.code).upper()
        candidate = next((x for x in universe if str(x.code).upper() == code), None)
        tags = [str(x).strip() for x in ((candidate.tags if candidate else []) or []) if str(x).strip()]
        themes = extract_themes(tags)
        if not themes:
            industry = industry_lookup.get(code, "")
            if "-" in industry:
                tail = industry.split("-", 1)[1].strip()
                if tail:
                    themes = [tail]
        for theme in themes:
            theme_value[theme] = float(theme_value.get(theme, 0.0)) + float(item.market_value)
    theme_rows = []
    for theme, value in sorted(theme_value.items(), key=lambda kv: kv[1], reverse=True):
        weight = (float(value) / nav) if nav > 0 else 0.0
        cap_limit = theme_cap_for(theme, settings)
        theme_rows.append(
            {
                "theme": theme,
                "market_value": round(float(value), 2),
                "weight": round(weight, 6),
                "cap_limit": round(float(cap_limit), 6),
                "over_limit": bool(weight > cap_limit),
            }
        )
    rejected_actions = latest_plan.get("rejected_actions", []) or []
    reject_reason_count = {}
    for item in rejected_actions:
        reason = str(item.get("reason", "unknown") or "unknown")
        reject_reason_count[reason] = int(reject_reason_count.get(reason, 0)) + 1
    reject_summary = [
        {"reason": reason, "count": count}
        for reason, count in sorted(reject_reason_count.items(), key=lambda kv: kv[1], reverse=True)
    ]
    snapshot = {
        "source": source,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": portfolio.as_of,
        "portfolio": {
            "initial_capital": float(portfolio.initial_capital),
            "nav": round(nav, 2),
            "cash": round(float(portfolio.cash), 2),
            "available_cash": round(float(portfolio.available_cash), 2),
            "frozen_cash": round(float(portfolio.frozen_cash), 2),
            "cash_ratio": round((float(portfolio.cash) / nav) if nav > 0 else 0.0, 6),
            "realized_pnl": round(float(portfolio.realized_pnl), 2),
            "unrealized_pnl": round(float(portfolio.unrealized_pnl), 2),
            "total_pnl": round(float(portfolio.total_pnl), 2),
            "total_return_pct": round(float(portfolio.total_return_pct), 6),
            "total_fees": round(float(portfolio.total_fees), 2),
            "holdings_count": len(holdings),
            "top_positions": [
                {
                    "code": item.code,
                    "name": item.name,
                    "quantity": int(item.quantity),
                    "cost_price": float(item.cost_price),
                    "last_price": float(item.last_price),
                    "market_value": round(float(item.market_value), 2),
                    "weight": round((float(item.market_value) / nav) if nav > 0 else 0.0, 6),
                    "unrealized_pnl": round(float(item.unrealized_pnl), 2),
                    "unrealized_pnl_pct": round(float(item.unrealized_pnl_pct), 6),
                    "entry_date": str(item.entry_date or ""),
                    "highest_price": round(float(item.highest_price or 0.0), 3),
                }
                for item in holdings[:15]
            ],
        },
        "universe": {
            "total": len(universe),
            "active": len([x for x in universe if x.status == "active"]),
            "watch": len([x for x in universe if x.status == "watch"]),
            "candidate": len([x for x in universe if x.status == "candidate"]),
        },
        "latest_plan": {
            "trade_date": latest_plan.get("trade_date", ""),
            "plan_id": latest_plan.get("plan_id", ""),
            "actions_count": len(latest_plan.get("actions", []) or []),
            "rejected_actions_count": len(rejected_actions),
            "rejected_reason_summary": reject_summary[:8],
            "constraints": latest_plan.get("constraints", {}) or {},
            "top_actions": (latest_plan.get("actions", []) or [])[:12],
        },
        "latest_post_market_scan": {
            "trade_date": latest_scan.get("trade_date", ""),
            "created_at": latest_scan.get("created_at", ""),
            "llm_enabled": bool(latest_scan.get("llm_enabled")),
            "top_k": int(latest_scan.get("top_k", 0) or 0),
            "suggested_watchlist_count": len(latest_scan.get("suggested_watchlist", []) or []),
            "top_watchlist": (latest_scan.get("suggested_watchlist", []) or [])[:8],
        },
        "latest_evening_decision": repo.load_latest_decision("evening") or {},
        "latest_morning_decision": repo.load_latest_decision("morning") or {},
        "settings": settings.__dict__,
        "execution_monitor": execution_monitor,
        "industry_exposure": {
            "count": len(industry_rows),
            "rows": industry_rows[:20],
            "over_limit_count": len([x for x in industry_rows if bool(x.get("over_limit"))]),
        },
        "theme_exposure": {
            "count": len(theme_rows),
            "rows": theme_rows[:20],
            "over_limit_count": len([x for x in theme_rows if bool(x.get("over_limit"))]),
        },
    }
    return snapshot


def cmd_init(args: argparse.Namespace) -> int:
    repo = LongTermRepository()
    repo.init_if_missing(initial_capital=float(args.initial_capital))
    if args.seed_universe:
        seed_path = Path(args.seed_universe).expanduser().resolve()
        payload = json.loads(seed_path.read_text(encoding="utf-8"))
        stocks = payload if isinstance(payload, list) else payload.get("stocks", [])
        universe = []
        for item in stocks:
            universe.append(StockCandidate(**item))
        repo.save_universe(universe)
    if args.settings:
        settings_path = Path(args.settings).expanduser().resolve()
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
        repo.save_settings(LongTermSettings(**payload))
    snapshot_path = repo.save_investor_snapshot(_build_investor_snapshot(repo, source="init"))
    print(f"initialized longterm sim repository: {repo.data_dir}")
    print(f"investor_snapshot: {snapshot_path}")
    return 0


def cmd_run_review(args: argparse.Namespace) -> int:
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    repo = LongTermRepository()
    repo.init_if_missing()
    universe = repo.load_universe()
    portfolio = repo.load_portfolio()

    if not universe and not portfolio.positions:
        print("no universe and no positions, nothing to review")
        return 0

    data_source = OpenClawChinaStockDataSource()
    codes = sorted({item.code.upper() for item in universe} | {item.code.upper() for item in portfolio.positions})
    quotes = data_source.fetch_quotes(codes)
    settings = repo.load_settings()
    if args.single_name_cap is not None:
        settings.single_name_cap = float(args.single_name_cap)
    if args.cash_buffer_ratio is not None:
        settings.cash_buffer_ratio = float(args.cash_buffer_ratio)
    if args.rebalance_threshold is not None:
        settings.rebalance_threshold = float(args.rebalance_threshold)
    if args.max_industry_weight is not None:
        settings.max_industry_weight = float(args.max_industry_weight)
    if getattr(args, "industry_cap_mode", None) is not None:
        settings.industry_cap_mode = str(getattr(args, "industry_cap_mode")).strip().lower()
    if getattr(args, "core_industry_cap", None) is not None:
        settings.core_industry_cap = float(getattr(args, "core_industry_cap"))
    if getattr(args, "satellite_industry_cap", None) is not None:
        settings.satellite_industry_cap = float(getattr(args, "satellite_industry_cap"))
    if getattr(args, "core_industries", None) is not None:
        settings.core_industries = [
            x.strip() for x in str(getattr(args, "core_industries")).replace(";", ",").split(",") if x.strip()
        ]
    if getattr(args, "max_theme_weight", None) is not None:
        settings.max_theme_weight = float(getattr(args, "max_theme_weight"))
    if getattr(args, "theme_cap_mode", None) is not None:
        settings.theme_cap_mode = str(getattr(args, "theme_cap_mode")).strip().lower()
    if getattr(args, "core_theme_cap", None) is not None:
        settings.core_theme_cap = float(getattr(args, "core_theme_cap"))
    if getattr(args, "satellite_theme_cap", None) is not None:
        settings.satellite_theme_cap = float(getattr(args, "satellite_theme_cap"))
    if getattr(args, "core_themes", None) is not None:
        settings.core_themes = [x.strip() for x in str(getattr(args, "core_themes")).replace(";", ",").split(",") if x.strip()]
    if args.min_trade_amount is not None:
        settings.min_trade_amount = float(args.min_trade_amount)
    if args.max_holdings is not None:
        settings.max_holdings = int(args.max_holdings)
    if args.max_portfolio_volatility is not None:
        settings.max_portfolio_volatility = float(args.max_portfolio_volatility)
    if args.max_portfolio_drawdown is not None:
        settings.max_portfolio_drawdown = float(args.max_portfolio_drawdown)

    rotation_scan = getattr(args, "rotation_scan", None)
    # High-tech mode: fetch history_by_code for momentum scoring
    history_by_code_arg = None
    if bool(getattr(settings, "high_tech_mode", False)):
        # 使用近期数据（不含 end_date），实时行情将补齐最新价格
        history_by_code_arg = data_source.fetch_batch_daily_history(
            codes, lookback_days=int(getattr(settings, "ht_lookback_days", 20) or 20) + 10,
        )
    plan, mtm_portfolio = build_rebalance_plan(
        trade_date=trade_date,
        candidates=universe,
        portfolio=portfolio,
        quotes=quotes,
        settings=settings,
        rotation_scan=rotation_scan,
        history_by_code=history_by_code_arg,
    )
    path = repo.save_plan(plan)
    repo.save_portfolio(mtm_portfolio)
    snapshot_path = repo.save_investor_snapshot(_build_investor_snapshot(repo, source="run-review"))

    print(f"trade_date: {trade_date}")
    print(f"plan_file: {path}")
    print(f"nav: {mtm_portfolio.nav:.2f} cash: {mtm_portfolio.cash:.2f} holdings: {len(mtm_portfolio.positions)}")
    print(f"actions: {len(plan.actions)}")
    for item in plan.actions[:20]:
        print(
            f"- {item.code} {item.action} shares={item.delta_shares} ref={item.reference_price:.3f} "
            f"current={item.current_weight:.2%} target={item.target_weight:.2%} amount={item.estimated_amount:.2f}"
        )
    print(f"rejected_actions: {len(plan.rejected_actions)}")
    for item in plan.rejected_actions[:20]:
        print(
            f"- {item.code} {item.action} shares={item.delta_shares} ref={item.reference_price:.3f} "
            f"amount={item.estimated_amount:.2f} reason={item.reason}"
        )
    print(f"investor_snapshot: {snapshot_path}")
    return 0


def _extract_trades_from_image(image_path: Path, trade_date: str) -> List[ManualExecutionItem]:
    """Use vision LLM to extract trade records from a settlement screenshot."""
    system_prompt = (
        "你是一个证券交割单信息提取工具。从用户提供的截图/图片中提取所有成交记录。\n"
        "返回严格的 JSON 格式，不要增加任何额外文字：\n"
        "{\"trades\": [{\"code\": \"6位代码\", \"side\": \"buy或sell\", \"quantity\": 整数股数, \"price\": 成交价}]}\n"
        "规则：\n"
        "1. code 必须是纯 6 位数字字符串，如 \"688327\"\n"
        "2. side 只能是 \"buy\" 或 \"sell\"\n"
        "3. quantity 必须是整数\n"
        "4. price 保留实际成交价精度\n"
        "5. 如果图片中找不到成交记录，返回 {\"trades\": []}\n"
        "6. 不需要推断缺失的数据，只提取图中明确显示的"
    )
    user_text = f"请提取图片中的成交记录，交易日期为 {trade_date}"
    result = chat_vision_json(image_path=image_path, system_prompt=system_prompt, user_text=user_text)
    if not result:
        return []
    trades_raw = result.get("trades", [])
    if not isinstance(trades_raw, list):
        return []
    items: List[ManualExecutionItem] = []
    for item in trades_raw:
        try:
            code = str(item.get("code", "") or "").strip()
            if not code or len(code) != 6 or not code.isdigit():
                continue
            side = str(item.get("side", "") or "").strip().lower()
            if side not in ("buy", "sell"):
                continue
            quantity = int(item.get("quantity", 0) or 0)
            price = float(item.get("price", 0) or 0)
            if quantity <= 0 or price <= 0:
                continue
            items.append(ManualExecutionItem(
                trade_date=trade_date,
                code=code,
                name=code,
                side=side,
                price=price,
                quantity=quantity,
                trade_time="",
                fee=0.0,
                note="image_ocr",
            ))
        except (TypeError, ValueError):
            continue
    return items


def cmd_apply_manual_from_image(args: argparse.Namespace) -> int:
    repo = LongTermRepository()
    repo.init_if_missing()
    image_path = Path(args.image).expanduser().resolve()
    if not image_path.exists():
        print(f"image not found: {image_path}")
        return 1
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    print(f"extracting trades from: {image_path}")
    print(f"trade date: {trade_date}")
    records = _extract_trades_from_image(image_path, trade_date)
    if not records:
        print("no trades extracted from image (try clearer screenshot)")
        return 1
    print(f"\nextracted {len(records)} trades:")
    total_buy = 0.0
    total_sell = 0.0
    for r in records:
        amt = r.price * r.quantity
        label = "买入" if r.side == "buy" else "卖出"
        print(f"  {label} {r.code} {r.quantity}股 @{r.price} = {amt:,.2f}")
        if r.side == "buy":
            total_buy += amt
        else:
            total_sell += amt
    print(f"  卖出总额: {total_sell:,.2f}  买入总额: {total_buy:,.2f}")
    if args.dry_run:
        print("\n[dry-run] not applying to portfolio")
        return 0
    result = _apply_manual_records(repo, records=records, as_of=trade_date, source="apply-manual-from-image")
    next_state = result["portfolio"]
    print(f"\napplied {int(result['records_count'])} records")
    print(f"nav: {next_state.nav:,.2f}  cash: {next_state.cash:,.2f}  holdings: {len(next_state.positions)}")
    ninedb = result.get("ninedb_sync", {}) or {}
    print(
        f"9db_sync: {'ok' if ninedb.get('ok') else 'skipped' if not ninedb.get('enabled') else 'failed'} "
        f"submitted={int(ninedb.get('submitted_count', 0) or 0)} "
        f"error={str(ninedb.get('error', '') or '-')}"
    )
    return 0


def _extract_positions_from_image(image_path: Path) -> dict:
    """Use vision LLM to extract holdings + cash from a brokerage position screenshot.

    After OCR extraction, validates stock codes by cross-checking with real-time quotes.
    If the quoted price differs from the OCR market_price by > 50% (or if the quote
    returns a wildly different price for the given code), the code is likely OCR-misread.
    The fix attempts a name-based code lookup via Tencent quotes.
    """
    system_prompt = (
        "你是一个证券持仓截图信息提取工具。从用户提供的截图/图片中提取持仓和资金信息。\n"
        "返回严格的 JSON 格式，不要增加任何额外文字：\n"
        "{\n"
        "  \"positions\": [\n"
        "    {\"code\": \"6位代码\", \"name\": \"股票名称\", \"quantity\": 整数股数, "
        "\"cost_price\": 成本价, \"market_price\": 现价}\n"
        "  ],\n"
        "  \"available_cash\": 可用资金,\n"
        "  \"total_assets\": 总资产,\n"
        "  \"frozen_cash\": 冻结资金(找不到填0)\n"
        "}\n"
        "规则：\n"
        "1. code 必须是纯 6 位数字字符串，如 \"300475\"\n"
        "2. quantity 必须是整数\n"
        "3. cost_price / market_price / available_cash / total_assets 保留小数精度\n"
        "4. 如果找不到某字段，填 0 或空数组 []\n"
        "5. 不需要推断缺失的数据，只提取图中明确显示的\n"
        "6. 注意区分\"可用资金\"和\"总资产\"，两者通常不同\n"
        "7. 持仓表格通常有：证券代码、证券名称、持仓数量、成本价、现价等列\n"
        "8. ⚠️ 代码识别准确率至关重要！注意：\n"
        "   - 科创板(688xxx)和创业板(300xxx/301xxx)的代码容易混淆\n"
        "   - 请逐位核对数字，特别是第1-3位（市场前缀）\n"
        "   - 香农芯创是 300475（创业板），不是 688327（科创板）\n"
        "   - 天孚通信是 300394（创业板），不是 603025（沪市）\n"
        "   - 如果不确定，优先检查截图上是否标注了\"创\"或\"科创\"字样"
    )
    user_text = "请提取截图中的持仓列表和资金信息"
    result = chat_vision_json(image_path=image_path, system_prompt=system_prompt, user_text=user_text)
    if not result:
        return {"positions": [], "available_cash": 0.0, "total_assets": 0.0, "frozen_cash": 0.0}
    positions_raw = result.get("positions", [])
    if not isinstance(positions_raw, list):
        positions_raw = []
    positions: list[dict] = []
    for item in positions_raw:
        try:
            code = str(item.get("code", "") or "").strip()
            if not code or len(code) != 6 or not code.isdigit():
                continue
            quantity = int(item.get("quantity", 0) or 0)
            if quantity <= 0:
                continue
            name = str(item.get("name", "") or "").strip() or code
            cost_price = float(item.get("cost_price", 0) or 0)
            market_price = float(item.get("market_price", 0) or 0)
            if cost_price <= 0 and market_price <= 0:
                continue
            if cost_price <= 0:
                cost_price = market_price
            if market_price <= 0:
                market_price = cost_price
            positions.append({
                "code": code,
                "name": name,
                "quantity": quantity,
                "cost_price": cost_price,
                "market_price": market_price,
            })
        except (TypeError, ValueError):
            continue

    # ── Code validation: cross-check with real-time quotes ──────────────
    try:
        from .data_source import OpenClawChinaStockDataSource
        ds = OpenClawChinaStockDataSource()
        ocr_codes = [p["code"] for p in positions]
        if ocr_codes:
            live_quotes = ds.fetch_quotes(ocr_codes)
            for idx, pos in enumerate(positions):
                code = pos["code"]
                q = live_quotes.get(code, {}) or {}
                live_price = float(q.get("price", 0) or 0)
                live_name = str(q.get("name", "") or "").strip()
                ocr_price = pos["market_price"]
                ocr_name = pos["name"]

                # If live price is available, validate it against OCR market_price
                if live_price > 0 and ocr_price > 0:
                    ratio = ocr_price / max(live_price, 0.01)
                    # If the prices differ by more than 50% (ratio < 0.5 or > 2), the code may be OCR-misread
                    if not (0.5 <= ratio <= 2.0):
                        logger.warning(
                            "position_sync: OCR code %s (name=%s) price mismatch: "
                            "ocr=%.2f vs live=%.2f (ratio=%.2f), attempting correction",
                            code, ocr_name, ocr_price, live_price, ratio,
                        )
                        corrected_code = _correct_stock_code(code, ocr_price, ocr_name, ds)
                        if corrected_code and corrected_code != code:
                            logger.info("position_sync: corrected code %s→%s", code, corrected_code)
                            positions[idx]["code"] = corrected_code
                            # Re-fetch quote for corrected code to update market_price
                            new_q = ds.fetch_quotes([corrected_code]).get(corrected_code, {}) or {}
                            new_px = float(new_q.get("price", 0) or 0)
                            new_name = str(new_q.get("name", "") or "").strip()
                            if new_px > 0:
                                positions[idx]["market_price"] = round(new_px, 3)
                            if new_name:
                                positions[idx]["name"] = new_name
                elif live_price > 0 and ocr_price <= 0:
                    # OCR didn't get market price, fill from live quote
                    positions[idx]["market_price"] = round(live_price, 3)
    except Exception:
        logger.warning("position_sync: code validation failed (non-critical)")

    try:
        available_cash = float(result.get("available_cash", 0) or 0)
    except (TypeError, ValueError):
        available_cash = 0.0
    try:
        total_assets = float(result.get("total_assets", 0) or 0)
    except (TypeError, ValueError):
        total_assets = 0.0
    try:
        frozen_cash = float(result.get("frozen_cash", 0) or 0)
    except (TypeError, ValueError):
        frozen_cash = 0.0
    # If total_assets given but available_cash not, infer cash from total minus holdings
    holdings_value = sum(
        float(p["quantity"]) * float(p["market_price"] or p["cost_price"])
        for p in positions
    )
    if total_assets > 0 and available_cash <= 0:
        available_cash = max(0.0, total_assets - holdings_value - frozen_cash)
    # If available_cash exceeds total_assets, clamp
    if total_assets > 0 and available_cash > total_assets:
        available_cash = max(0.0, total_assets - holdings_value - frozen_cash)
    return {
        "positions": positions,
        "available_cash": round(available_cash, 2),
        "total_assets": round(total_assets, 2),
        "frozen_cash": round(frozen_cash, 2),
    }


def _correct_stock_code(
    ocr_code: str,
    ocr_price: float,
    ocr_name: str,
    ds: "OpenClawChinaStockDataSource",
) -> str | None:
    """Try to correct an OCR-misread stock code by matching name/price.

    Strategy:
    1. Query Tencent quotes for same-suffix variants (688↔300↔603 prefix swaps)
    2. Match by price range (ratio within 0.5-2.0) AND name match
    3. Fallback: if suffix-based candidates fail and OCR name is useful,
       search Tencent by stock name to find the real code
    """
    import urllib.request

    _logger = logging.getLogger(__name__)

    # ── Step 1: suffix-based candidates ──────────────────────────────
    candidates_map: dict[str, list[str]] = {}
    suffix = ocr_code[3:]  # last 3 digits
    if ocr_code.startswith("688"):
        candidates_map["300" + suffix] = f"sz300{suffix}"
    elif ocr_code.startswith(("603", "600")):
        candidates_map["300" + suffix] = f"sz300{suffix}"
        candidates_map["688" + suffix] = f"sh688{suffix}"
    elif ocr_code.startswith("300"):
        candidates_map["688" + suffix] = f"sh688{suffix}"
        candidates_map["603" + suffix] = f"sh603{suffix}"
    elif ocr_code.startswith("301"):
        candidates_map["300" + suffix] = f"sz300{suffix}"

    if candidates_map:
        tencent_codes = list(candidates_map.values())
        url = f"https://qt.gtimg.cn/q={','.join(tencent_codes)}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
            try:
                text = raw.decode("gbk", errors="replace")
            except Exception:
                text = raw.decode("utf-8", errors="replace")
        except Exception as e:
            _logger.warning("code_correction: Tencent query failed: %s", e)
            text = ""

        for candidate_code, tencent_symbol in candidates_map.items():
            pattern = f"v_{tencent_symbol}="
            for line in text.split(";"):
                if not line.startswith(pattern):
                    continue
                try:
                    _, data_part = line.split("=", 1)
                    parts = data_part.strip('"').strip("'\n").split("~")
                    if len(parts) < 5:
                        continue
                    live_name = str(parts[1] if len(parts) > 1 else "").strip()
                    live_price = float(parts[3] if len(parts) > 3 else 0)
                    if live_price <= 0:
                        continue
                    ratio = ocr_price / live_price if live_price > 0 else 0
                    price_ok = 0.5 <= ratio <= 2.0
                    name_ok = bool(ocr_name and live_name) and (
                        ocr_name in live_name or live_name in ocr_name
                    )
                    if price_ok and name_ok:
                        _logger.info(
                            "code_correction: suffix perfect match %s→%s (name=%s, price=%.2f)",
                            ocr_code, candidate_code, live_name, live_price,
                        )
                        return candidate_code
                except (IndexError, ValueError, TypeError):
                    continue

    # ── Step 2: name-based search via Sina suggest API ─────────────
    if ocr_name:
        name = ocr_name.strip()
        import urllib.parse
        try:
            quoted = urllib.parse.quote(name)
            search_url = f"https://suggest3.sinajs.cn/suggest/type=11,12,13&key={quoted}"
            req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
            try:
                text = raw.decode("gbk", errors="replace")
            except Exception:
                text = raw.decode("utf-8", errors="replace")
        except Exception as e:
            _logger.warning("code_correction: Sina suggest failed: %s", e)
            text = ""

        if text and "=" in text and "pv_none" not in text:
            try:
                # Format: var suggestvalue="name,type,code,market_code,...";
                eq_idx = text.index("=")
                raw_val = text[eq_idx + 1:].strip().strip('"').strip("'\n; ")
                for part in raw_val.split(","):
                    part = part.strip()
                segments = raw_val.split(",")
                if len(segments) >= 3:
                    live_code = str(segments[2]).strip()
                    live_name_result = str(segments[0]).strip()
                    if len(live_code) == 6 and live_code.isdigit():
                        _logger.info(
                            "code_correction: Sina suggest match %s→%s (name=%s)",
                            ocr_code, live_code, live_name_result,
                        )
                        return live_code
            except (IndexError, ValueError, TypeError):
                pass

    return None


def _apply_position_sync(repo: LongTermRepository, extracted: dict, as_of: str) -> dict:
    """Apply position sync: replace portfolio positions/cash with extracted data."""
    old_portfolio = repo.load_portfolio()

    new_positions = [
        SimPosition(
            code=p["code"],
            name=p["name"],
            quantity=p["quantity"],
            cost_price=p["cost_price"],
            last_price=p["market_price"],
        )
        for p in extracted["positions"]
    ]
    new_positions.sort(key=lambda x: x.market_value, reverse=True)

    available_cash = extracted["available_cash"]
    frozen_cash = extracted.get("frozen_cash", 0.0)
    total_cash = available_cash + frozen_cash

    new_portfolio = PortfolioState(
        as_of=as_of,
        initial_capital=old_portfolio.initial_capital,
        cash=round(total_cash, 2),
        available_cash=round(available_cash, 2),
        frozen_cash=round(frozen_cash, 2),
        positions=new_positions,
        target_weights=dict(old_portfolio.target_weights),
        realized_pnl=float(old_portfolio.realized_pnl),
        total_fees=float(old_portfolio.total_fees),
    )

    # Build diff summary for audit
    old_by_code = {p.code: p for p in old_portfolio.positions}
    new_by_code = {p.code: p for p in new_positions}
    all_codes = set(old_by_code) | set(new_by_code)

    diffs: list[str] = []
    for code in sorted(all_codes):
        old_pos = old_by_code.get(code)
        new_pos = new_by_code.get(code)
        if old_pos and new_pos:
            if old_pos.quantity != new_pos.quantity:
                diffs.append(
                    f"{code} qty: {old_pos.quantity}→{new_pos.quantity} "
                    f"cost: {old_pos.cost_price:.3f}→{new_pos.cost_price:.3f}"
                )
            elif abs(old_pos.cost_price - new_pos.cost_price) > 0.01:
                diffs.append(
                    f"{code} cost: {old_pos.cost_price:.3f}→{new_pos.cost_price:.3f} (qty unchanged)"
                )
        elif new_pos and not old_pos:
            diffs.append(f"{code} NEW: {new_pos.quantity}股 cost={new_pos.cost_price:.3f}")
        elif old_pos and not new_pos:
            diffs.append(f"{code} REMOVED: was {old_pos.quantity}股")

    cash_diff = total_cash - old_portfolio.cash
    if abs(cash_diff) > 0.01:
        diffs.append(f"cash: {old_portfolio.cash:.2f}→{total_cash:.2f} (Δ{cash_diff:+.2f})")

    # Save
    repo.save_portfolio(new_portfolio)

    # Record sync event in manual execution log (for audit trail)
    sync_note = f"position_sync from image: {'; '.join(diffs) if diffs else 'no changes'}"
    repo.append_manual_executions([
        ManualExecutionItem(
            trade_date=as_of,
            code="POS_SYNC",
            name="持仓同步",
            side="buy" if cash_diff >= 0 else "sell",
            price=abs(cash_diff) if cash_diff != 0 else 0.01,
            quantity=len(new_positions),
            trade_time="",
            fee=0.0,
            note=sync_note[:200],
        )
    ])

    snapshot_path = repo.save_investor_snapshot(
        _build_investor_snapshot(repo, source="position-sync-image")
    )

    return {
        "old_portfolio": old_portfolio,
        "new_portfolio": new_portfolio,
        "diffs": diffs,
        "snapshot_path": snapshot_path,
        "holdings_value": sum(p.market_value for p in new_positions),
    }


def cmd_sync_positions_from_image(args: argparse.Namespace) -> int:
    repo = LongTermRepository()
    repo.init_if_missing()
    image_path = Path(args.image).expanduser().resolve()
    if not image_path.exists():
        print(f"image not found: {image_path}")
        return 1
    as_of = args.date or datetime.now().strftime("%Y-%m-%d")
    print(f"extracting positions from: {image_path}")
    extracted = _extract_positions_from_image(image_path)
    positions = extracted.get("positions", [])
    available_cash = extracted.get("available_cash", 0.0)
    total_assets = extracted.get("total_assets", 0.0)
    frozen_cash = extracted.get("frozen_cash", 0.0)

    if not positions and available_cash <= 0:
        print("no positions or cash extracted from image (try clearer screenshot)")
        return 1

    print(f"\nextracted {len(positions)} holdings:")
    holdings_value = 0.0
    for p in positions:
        mv = float(p["quantity"]) * float(p["market_price"] or p["cost_price"])
        holdings_value += mv
        print(
            f"  {p['code']} {p['name']} qty={p['quantity']} "
            f"cost={p['cost_price']:.3f} px={p['market_price']:.3f} mv={mv:,.2f}"
        )
    print(f"  持仓市值: {holdings_value:,.2f}")
    print(f"  可用资金: {available_cash:,.2f}")
    print(f"  冻结资金: {frozen_cash:,.2f}")
    print(f"  总资产: {total_assets:,.2f}")
    if total_assets > 0:
        implied_nav = holdings_value + available_cash + frozen_cash
        print(f"  推算净资产: {implied_nav:,.2f} (vs 截图总资产 {total_assets:,.2f})")

    if args.dry_run:
        print("\n[dry-run] not syncing to portfolio")
        return 0

    result = _apply_position_sync(repo, extracted, as_of)
    old = result["old_portfolio"]
    new = result["new_portfolio"]
    diffs = result["diffs"]
    print(f"\nsynced to portfolio:")
    print(f"  nav: {old.nav:,.2f} → {new.nav:,.2f}")
    print(f"  cash: {old.cash:,.2f} → {new.cash:,.2f}")
    print(f"  holdings: {len(old.positions)} → {len(new.positions)}")
    if diffs:
        print(f"  changes:")
        for d in diffs:
            print(f"    {d}")
    else:
        print("  (no changes)")
    print(f"  snapshot: {result['snapshot_path']}")
    return 0


def handle_feishu_position_sync(image_path: Path, repo: LongTermRepository | None = None) -> str:
    """Handle /更新持仓 command: extract positions from image and sync to portfolio."""
    _configure_logging()
    repo = repo or LongTermRepository()
    repo.init_if_missing()
    as_of = datetime.now().strftime("%Y-%m-%d")

    extracted = _extract_positions_from_image(image_path)
    positions = extracted.get("positions", [])
    available_cash = extracted.get("available_cash", 0.0)
    total_assets = extracted.get("total_assets", 0.0)
    frozen_cash = extracted.get("frozen_cash", 0.0)

    if not positions and available_cash <= 0:
        return "未能从截图中识别出持仓信息，请确认截图清晰并包含完整持仓列表（代码、数量、成本价、现价）和可用资金"

    lines = [f"📸 从持仓截图中识别到 {len(positions)} 只股票:"]
    holdings_value = 0.0
    for p in positions:
        mv = float(p["quantity"]) * float(p["market_price"] or p["cost_price"])
        holdings_value += mv
        lines.append(
            f"  {p['code']} {p['name']} {p['quantity']}股 "
            f"成本{p['cost_price']:.3f} 现价{p['market_price']:.3f} 市值{mv:,.2f}"
        )
    lines.append(f"持仓市值: {holdings_value:,.2f}")
    lines.append(f"可用资金: {available_cash:,.2f}")
    if frozen_cash > 0:
        lines.append(f"冻结资金: {frozen_cash:,.2f}")
    if total_assets > 0:
        lines.append(f"截图总资产: {total_assets:,.2f}")

    result = _apply_position_sync(repo, extracted, as_of)
    old = result["old_portfolio"]
    new = result["new_portfolio"]
    diffs = result["diffs"]

    lines.append(f"\n✅ 持仓已同步:")
    lines.append(f"  NAV: {old.nav:,.2f} → {new.nav:,.2f}")
    lines.append(f"  现金: {old.cash:,.2f} → {new.cash:,.2f}")
    lines.append(f"  持仓数: {len(old.positions)} → {len(new.positions)}")
    if diffs:
        lines.append(f"  变更:")
        for d in diffs[:10]:
            lines.append(f"    {d}")
        if len(diffs) > 10:
            lines.append(f"    ... 共 {len(diffs)} 项变更")
    return "\n".join(lines)


def cmd_apply_manual(args: argparse.Namespace) -> int:
    repo = LongTermRepository()
    repo.init_if_missing()
    if getattr(args, "text", None):
        default_date = args.date or str((repo.load_latest_plan() or {}).get("trade_date", "") or datetime.now().strftime("%Y-%m-%d"))
        records = parse_feishu_manual_command(str(args.text), default_date=default_date)
        as_of = args.date or max([str(item.trade_date or default_date).strip() for item in records] or [default_date])
        source = "apply-manual-command"
    else:
        records = _load_manual_file(Path(args.file).expanduser().resolve())
        as_of = args.date or datetime.now().strftime("%Y-%m-%d")
        source = "apply-manual"
    result = _apply_manual_records(repo, records=records, as_of=as_of, source=source)
    next_state = result["portfolio"]
    print(f"applied manual executions: {int(result['records_count'])}")
    print(f"nav: {next_state.nav:.2f} cash: {next_state.cash:.2f} holdings: {len(next_state.positions)}")
    ninedb = result.get("ninedb_sync", {}) or {}
    print(
        f"9db_sync: {'ok' if ninedb.get('ok') else 'skipped' if not ninedb.get('enabled') else 'failed'} "
        f"submitted={int(ninedb.get('submitted_count', 0) or 0)} "
        f"error={str(ninedb.get('error', '') or '-')}"
    )
    print(f"investor_snapshot: {result['snapshot_path']}")
    return 0


def cmd_rebuild_portfolio(args: argparse.Namespace) -> int:
    repo = LongTermRepository()
    repo.init_if_missing()
    current = repo.load_portfolio()
    as_of = args.date or datetime.now().strftime("%Y-%m-%d")
    rebuilt = rebuild_portfolio_from_executions(
        initial_capital=current.initial_capital,
        records=repo.load_manual_executions(),
        as_of=as_of,
        target_weights=current.target_weights,
    )
    repo.save_portfolio(rebuilt)
    snapshot_path = repo.save_investor_snapshot(_build_investor_snapshot(repo, source="rebuild-portfolio"))
    print("portfolio rebuilt from execution ledger")
    print(f"as_of: {rebuilt.as_of}")
    print(f"nav: {rebuilt.nav:.2f} cash: {rebuilt.cash:.2f} holdings: {len(rebuilt.positions)}")
    print(
        f"realized_pnl: {rebuilt.realized_pnl:.2f} "
        f"unrealized_pnl: {rebuilt.unrealized_pnl:.2f} "
        f"total_pnl: {rebuilt.total_pnl:.2f} return={rebuilt.total_return_pct:.2%}"
    )
    print(f"investor_snapshot: {snapshot_path}")
    return 0


def _format_auto_rebalance_notice(
    *,
    trade_date: str,
    plan_path: Path,
    records: List[ManualExecutionItem],
    portfolio: PortfolioState,
    snapshot_path: Path,
) -> tuple[str, dict]:
    buy_records = [item for item in records if item.side == "buy"]
    sell_records = [item for item in records if item.side == "sell"]
    buy_amount = sum(float(item.price) * int(item.quantity) for item in buy_records)
    sell_amount = sum(float(item.price) * int(item.quantity) for item in sell_records)

    def _rows(items: List[ManualExecutionItem]) -> str:
        if not items:
            return "（无）"
        lines = []
        for item in items[:15]:
            amount = float(item.price) * int(item.quantity)
            display = f"{item.name}（{item.code}）" if item.name and item.name != item.code else item.code
            lines.append(
                f"- {display}：{int(item.quantity)} 股，模拟价 {float(item.price):.3f} 元，金额 {amount:,.0f} 元"
            )
        if len(items) > 15:
            lines.append(f"... 另 {len(items) - 15} 笔")
        return "\n".join(lines)

    summary = (
        f"净值：{portfolio.nav:,.2f} 元\n"
        f"现金：{portfolio.cash:,.2f} 元｜可用资金：{portfolio.available_cash:,.2f} 元\n"
        f"持仓：{len(portfolio.positions)} 只\n"
        f"已实现收益：{portfolio.realized_pnl:,.2f} 元\n"
        f"浮动收益：{portfolio.unrealized_pnl:,.2f} 元\n"
        f"总收益：{portfolio.total_pnl:,.2f} 元（{portfolio.total_return_pct:.2%}）"
    )
    trade_summary = (
        f"模拟买入：{len(buy_records)} 笔，合计 {buy_amount:,.0f} 元\n"
        f"模拟卖出：{len(sell_records)} 笔，合计 {sell_amount:,.0f} 元"
    )
    text = (
        f"【长线模拟组合调仓记录】{trade_date}\n"
        "本次仅更新模拟组合账本，不代表实盘成交，不会向券商自动下单。\n"
        f"{summary}\n"
        f"{trade_summary}\n\n"
        f"模拟买入明细：\n{_rows(buy_records)}\n\n"
        f"模拟卖出明细：\n{_rows(sell_records)}"
    )
    card = _build_feishu_card(
        f"长线模拟组合调仓记录 {trade_date}",
        [
            {"title": "性质说明", "content": "仅更新模拟组合账本，不代表实盘成交，不会向券商自动下单。"},
            {"title": "组合状态", "content": summary},
            {"title": "模拟调仓汇总", "content": trade_summary},
            {"title": "模拟买入明细", "content": _rows(buy_records)},
            {"title": "模拟卖出明细", "content": _rows(sell_records)},
        ],
        template="green",
    )
    return text, card


def _format_portfolio_return_summary(portfolio: PortfolioState) -> str:
    nav = float(portfolio.nav)
    cash_pct = float(portfolio.cash) / nav * 100 if nav > 0 else 0.0
    initial = float(portfolio.initial_capital or 0.0)
    nav_change = nav - initial if initial > 0 else 0.0
    ledger_pnl = float(portfolio.realized_pnl) + float(portfolio.unrealized_pnl)
    lines = [
        f"净值：{nav:,.0f} 元｜现金：{float(portfolio.cash):,.0f} 元（{cash_pct:.1f}%）\n"
        f"净值较初始：{nav_change:,.0f} 元（{float(portfolio.total_return_pct):.2%}）\n"
        f"交易损益记录：已实现 {float(portfolio.realized_pnl):,.0f} 元｜"
        f"浮动 {float(portfolio.unrealized_pnl):,.0f} 元"
    ]
    reconciliation_gap = nav_change - ledger_pnl
    tolerance = max(1.0, abs(initial) * 0.001)
    if initial > 0 and abs(reconciliation_gap) > tolerance:
        lines.append(
            f"口径提示：净值变动与交易损益记录相差 {reconciliation_gap:,.0f} 元；"
            "可能包含资金调整或历史账本未衔接，收益率以净值口径为准。"
        )
    return "\n".join(lines)


def cmd_auto_rebalance(args: argparse.Namespace) -> int:
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    if not bool(getattr(args, "ignore_trading_calendar", False)):
        is_open, source = is_cn_trading_day(trade_date)
        if not is_open:
            print(f"skip auto-rebalance: {trade_date} non_trading_day source={source}")
            return 0
    repo = LongTermRepository()
    repo.init_if_missing()
    plan, mtm_portfolio = _build_review_plan(repo, trade_date)
    plan_path = repo.save_plan(plan)
    records = build_execution_records_from_plan(plan)
    print(f"trade_date: {trade_date}")
    print(f"plan_file: {plan_path}")
    print(f"planned_actions: {len(plan.actions)} executable_fills: {len(records)}")
    if getattr(args, "dry_run", False):
        repo.save_portfolio(mtm_portfolio)
        snapshot_path = repo.save_investor_snapshot(_build_investor_snapshot(repo, source="auto-rebalance-dry-run"))
        print(f"dry_run: true")
        print(f"nav: {mtm_portfolio.nav:.2f} cash: {mtm_portfolio.cash:.2f} holdings: {len(mtm_portfolio.positions)}")
        print(f"investor_snapshot: {snapshot_path}")
        return 0
    if not records:
        repo.save_portfolio(mtm_portfolio)
        snapshot_path = repo.save_investor_snapshot(_build_investor_snapshot(repo, source="auto-rebalance-noop"))
        print("auto_rebalance: no executable actions")
        print(f"nav: {mtm_portfolio.nav:.2f} cash: {mtm_portfolio.cash:.2f} holdings: {len(mtm_portfolio.positions)}")
        print(f"investor_snapshot: {snapshot_path}")
        return 0
    next_state = apply_manual_executions(mtm_portfolio, records)
    next_state.as_of = trade_date
    repo.append_manual_executions(records)
    repo.save_portfolio(next_state)
    snapshot_path = repo.save_investor_snapshot(_build_investor_snapshot(repo, source="auto-rebalance"))
    print("auto_rebalance: applied")
    print(f"nav: {next_state.nav:.2f} cash: {next_state.cash:.2f} holdings: {len(next_state.positions)}")
    print(
        f"realized_pnl: {next_state.realized_pnl:.2f} "
        f"unrealized_pnl: {next_state.unrealized_pnl:.2f} "
        f"total_pnl: {next_state.total_pnl:.2f} return={next_state.total_return_pct:.2%}"
    )
    for rec in records[:20]:
        side_text = "buy" if rec.side == "buy" else "sell"
        print(f"- {side_text} {rec.code} qty={rec.quantity} px={rec.price:.3f} {rec.name}")
    print(f"investor_snapshot: {snapshot_path}")
    notice_text, notice_card = _format_auto_rebalance_notice(
        trade_date=trade_date,
        plan_path=plan_path,
        records=records,
        portfolio=next_state,
        snapshot_path=snapshot_path,
    )
    pushed = False if bool(getattr(args, "no_push", False)) else push_feishu_rich(notice_text, card=notice_card)
    print(f"feishu_pushed: {pushed}")
    return 0


def cmd_configure_9db(args: argparse.Namespace) -> int:
    repo = LongTermRepository()
    repo.init_if_missing()
    payload = parse_9db_url(str(args.url or "").strip())
    config_path = save_9db_config(repo.ninedb_config_file, payload)
    print(f"configured_9db: {config_path}")
    print(f"order_id: {payload.get('order_id')}")
    print(f"order_name: {payload.get('order_name')}")
    print(f"token_prefix: {str(payload.get('token', ''))[:8]}...")
    return 0


def cmd_show_9db(args: argparse.Namespace) -> int:
    repo = LongTermRepository()
    repo.init_if_missing()
    payload = load_9db_config(repo.ninedb_config_file)
    if not payload:
        print("9db not configured")
        return 1
    masked = dict(payload)
    token = str(masked.get("token", "") or "")
    masked["token"] = f"{token[:8]}..." if token else ""
    print(json.dumps(masked, ensure_ascii=False, indent=2))
    return 0


def cmd_apply_manual_command(args: argparse.Namespace) -> int:
    print(handle_feishu_manual_command(str(args.text or "")))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    repo = LongTermRepository()
    repo.init_if_missing()
    portfolio = repo.load_portfolio()
    latest_plan = repo.load_latest_plan()
    settings = repo.load_settings()
    print(f"as_of: {portfolio.as_of}")
    print(f"initial_capital: {portfolio.initial_capital:.2f}")
    print(f"nav: {portfolio.nav:.2f}")
    print(f"cash: {portfolio.cash:.2f}")
    print(f"available_cash: {portfolio.available_cash:.2f}")
    print(f"frozen_cash: {portfolio.frozen_cash:.2f}")
    print(f"realized_pnl: {portfolio.realized_pnl:.2f}")
    print(f"unrealized_pnl: {portfolio.unrealized_pnl:.2f}")
    print(f"total_pnl: {portfolio.total_pnl:.2f}")
    print(f"total_return: {portfolio.total_return_pct:.2%}")
    print(f"total_fees: {portfolio.total_fees:.2f}")
    print(f"holdings: {len(portfolio.positions)}")
    for item in sorted(portfolio.positions, key=lambda x: x.market_value, reverse=True):
        weight = item.market_value / portfolio.nav if portfolio.nav > 0 else 0.0
        print(
            f"- {item.code} {item.name} qty={item.quantity} cost={item.cost_price:.3f} "
            f"last={item.last_price:.3f} mv={item.market_value:.2f} w={weight:.2%} "
            f"upnl={item.unrealized_pnl:.2f} upnl_pct={item.unrealized_pnl_pct:.2%}"
        )
    if latest_plan:
        print(
            "latest_plan: "
            f"{latest_plan.get('trade_date', '')} {latest_plan.get('plan_id', '')} "
            f"actions={len(latest_plan.get('actions', []))} "
            f"rejected={len(latest_plan.get('rejected_actions', []))}"
        )
    print(
        "settings: "
        f"cap={settings.single_name_cap:.2%} "
        f"industry_cap={settings.max_industry_weight:.2%} "
        f"industry_mode={settings.industry_cap_mode} "
        f"core_cap={settings.core_industry_cap:.2%} "
        f"satellite_cap={settings.satellite_industry_cap:.2%} "
        f"theme_cap={settings.max_theme_weight:.2%} "
        f"theme_mode={settings.theme_cap_mode} "
        f"core_theme_cap={settings.core_theme_cap:.2%} "
        f"sat_theme_cap={settings.satellite_theme_cap:.2%} "
        f"cash_buffer={settings.cash_buffer_ratio:.2%} "
        f"min_trade_amount={settings.min_trade_amount:.0f} "
        f"max_holdings={settings.max_holdings} "
        f"max_portfolio_volatility={settings.max_portfolio_volatility:.2%} "
        f"max_portfolio_drawdown={settings.max_portfolio_drawdown:.2%} "
        f"retention(plan/scan/decision)={settings.retention_plan_days}/{settings.retention_scan_days}/{settings.retention_decision_days}d "
        f"retention_keep_min_files={settings.retention_keep_min_files}"
    )
    snapshot_path = repo.save_investor_snapshot(_build_investor_snapshot(repo, source="summary"))
    print(f"investor_snapshot: {snapshot_path}")
    return 0


def cmd_show_settings(args: argparse.Namespace) -> int:
    repo = LongTermRepository()
    repo.init_if_missing()
    settings = repo.load_settings()
    print(json.dumps(settings.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_update_settings(args: argparse.Namespace) -> int:
    repo = LongTermRepository()
    repo.init_if_missing()
    settings = repo.load_settings()
    if args.file:
        payload = json.loads(Path(args.file).expanduser().resolve().read_text(encoding="utf-8"))
        for key, value in payload.items():
            if hasattr(settings, key):
                setattr(settings, key, value)
    else:
        updates = {
            "single_name_cap": args.single_name_cap,
            "cash_buffer_ratio": args.cash_buffer_ratio,
            "rebalance_threshold": args.rebalance_threshold,
            "max_industry_weight": args.max_industry_weight,
            "industry_cap_mode": args.industry_cap_mode,
            "core_industry_cap": args.core_industry_cap,
            "satellite_industry_cap": args.satellite_industry_cap,
            "core_industries": args.core_industries,
            "max_theme_weight": args.max_theme_weight,
            "theme_cap_mode": args.theme_cap_mode,
            "core_theme_cap": args.core_theme_cap,
            "satellite_theme_cap": args.satellite_theme_cap,
            "core_themes": args.core_themes,
            "min_trade_amount": args.min_trade_amount,
            "max_holdings": args.max_holdings,
            "score_value_weight": args.score_value_weight,
            "score_quality_weight": args.score_quality_weight,
            "score_growth_weight": args.score_growth_weight,
            "score_risk_weight": args.score_risk_weight,
            "max_portfolio_volatility": args.max_portfolio_volatility,
            "max_portfolio_drawdown": args.max_portfolio_drawdown,
            "scan_atr_min": args.scan_atr_min,
            "scan_rsi_min": args.scan_rsi_min,
            "scan_rsi_max": args.scan_rsi_max,
            "scan_turnover_min": args.scan_turnover_min,
            "scan_turnover_max": args.scan_turnover_max,
            "scan_fallback_amount_divisor": args.scan_fallback_amount_divisor,
            "scan_fallback_amount_bonus_cap": args.scan_fallback_amount_bonus_cap,
            "scan_fallback_volume_divisor": args.scan_fallback_volume_divisor,
            "scan_fallback_volume_bonus_cap": args.scan_fallback_volume_bonus_cap,
            "ths_heat_limit_up_weight": args.ths_heat_limit_up_weight,
            "ths_heat_change_weight": args.ths_heat_change_weight,
            "retention_plan_days": args.retention_plan_days,
            "retention_scan_days": args.retention_scan_days,
            "retention_decision_days": args.retention_decision_days,
            "retention_keep_min_files": args.retention_keep_min_files,
            "retention_delete_tmp_older_than_hours": args.retention_delete_tmp_older_than_hours,
        }
        for key, value in updates.items():
            if value is not None and hasattr(settings, key):
                if key == "core_industries":
                    value = [x.strip() for x in str(value).replace(";", ",").split(",") if x.strip()]
                if key == "core_themes":
                    value = [x.strip() for x in str(value).replace(";", ",").split(",") if x.strip()]
                setattr(settings, key, value)
    repo.save_settings(settings)
    print("settings updated")
    print(json.dumps(settings.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_cleanup_data(args: argparse.Namespace) -> int:
    repo = LongTermRepository()
    repo.init_if_missing()
    settings = repo.load_settings()
    keep_days_plan = int(
        args.keep_days_plan
        if args.keep_days_plan is not None
        else int(getattr(settings, "retention_plan_days", 180))
    )
    keep_days_scan = int(
        args.keep_days_scan
        if args.keep_days_scan is not None
        else int(getattr(settings, "retention_scan_days", 120))
    )
    keep_days_decision = int(
        args.keep_days_decision
        if args.keep_days_decision is not None
        else int(getattr(settings, "retention_decision_days", 180))
    )
    keep_min_files = int(
        args.keep_min_files
        if args.keep_min_files is not None
        else int(getattr(settings, "retention_keep_min_files", 30))
    )
    delete_tmp_hours = int(
        args.delete_tmp_older_than_hours
        if args.delete_tmp_older_than_hours is not None
        else int(getattr(settings, "retention_delete_tmp_older_than_hours", 24))
    )
    result = repo.cleanup_history(
        keep_days_plan=max(0, keep_days_plan),
        keep_days_scan=max(0, keep_days_scan),
        keep_days_decision=max(0, keep_days_decision),
        keep_min_files=max(0, keep_min_files),
        delete_tmp_older_than_hours=max(1, delete_tmp_hours),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_decision_quality_report(args: argparse.Namespace) -> int:
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    repo = LongTermRepository()
    repo.init_if_missing()
    latest_plan = repo.load_latest_plan() or {}
    manual_all = repo.load_manual_executions()
    manual = [x for x in manual_all if str(x.trade_date or "").strip() == trade_date]

    plan_actions = list(latest_plan.get("actions", []) or [])
    planned_direction: dict = {}
    for item in plan_actions:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "") or "").strip().upper()
        action = str(item.get("action", "") or "").strip().lower()
        if code and action in {"buy", "sell"}:
            planned_direction[code] = action

    net_qty = defaultdict(int)
    for rec in manual:
        code = str(rec.code or "").strip().upper()
        side = str(rec.side or "").strip().lower()
        qty = int(rec.quantity or 0)
        if not code or qty <= 0:
            continue
        if side == "buy":
            net_qty[code] += qty
        elif side == "sell":
            net_qty[code] -= qty

    executed_direction: dict = {}
    for code, qty in net_qty.items():
        if qty > 0:
            executed_direction[code] = "buy"
        elif qty < 0:
            executed_direction[code] = "sell"

    matched = []
    mismatched = []
    unexpected = []
    for code, side in executed_direction.items():
        planned = planned_direction.get(code, "")
        if not planned:
            unexpected.append({"code": code, "executed": side, "reason": "not_in_plan"})
            continue
        if planned == side:
            matched.append({"code": code, "planned": planned, "executed": side})
        else:
            mismatched.append({"code": code, "planned": planned, "executed": side})
    missed = []
    for code, planned in planned_direction.items():
        if code not in executed_direction:
            missed.append({"code": code, "planned": planned, "reason": "no_manual_fill"})

    compared_total = len(matched) + len(mismatched)
    mismatch_rate = (len(mismatched) / compared_total) if compared_total > 0 else 0.0
    match_rate = (len(matched) / compared_total) if compared_total > 0 else 0.0
    report = {
        "trade_date": trade_date,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "plan": {
            "plan_id": str(latest_plan.get("plan_id", "") or ""),
            "trade_date": str(latest_plan.get("trade_date", "") or ""),
            "actions_count": len(plan_actions),
            "planned_direction_count": len(planned_direction),
        },
        "manual": {
            "fills_count": len(manual),
            "executed_direction_count": len(executed_direction),
        },
        "quality": {
            "match_count": len(matched),
            "mismatch_count": len(mismatched),
            "unexpected_count": len(unexpected),
            "missed_count": len(missed),
            "compared_total": compared_total,
            "mismatch_rate": round(mismatch_rate, 6),
            "match_rate": round(match_rate, 6),
        },
        "details": {
            "matched": matched[:100],
            "mismatched": mismatched[:100],
            "unexpected": unexpected[:100],
            "missed": missed[:100],
        },
    }
    reports_dir = repo.data_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"decision_quality_{trade_date}.json"
    latest = reports_dir / "latest_decision_quality.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # Rolling trend statistics and alert rules.
    lookback_days = max(1, int(getattr(args, "lookback_days", 20) or 20))
    match_alert = float(getattr(args, "match_rate_alert_threshold", 0.6) or 0.6)
    mismatch_alert = float(getattr(args, "mismatch_rate_alert_threshold", 0.3) or 0.3)
    date_cutoff = datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=lookback_days)
    series = []
    for fp in sorted(reports_dir.glob("decision_quality_*.json")):
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        d = str(raw.get("trade_date", "") or "").strip()
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except Exception:
            continue
        if dt < date_cutoff:
            continue
        q = raw.get("quality", {}) or {}
        series.append(
            {
                "trade_date": d,
                "match_rate": float(q.get("match_rate", 0) or 0),
                "mismatch_rate": float(q.get("mismatch_rate", 0) or 0),
                "compared_total": int(q.get("compared_total", 0) or 0),
            }
        )
    series.sort(key=lambda x: str(x.get("trade_date", "")))
    nonzero = [x for x in series if int(x.get("compared_total", 0) or 0) > 0]
    avg_match = round(sum(float(x.get("match_rate", 0) or 0) for x in nonzero) / max(1, len(nonzero)), 6) if nonzero else 0.0
    avg_mismatch = (
        round(sum(float(x.get("mismatch_rate", 0) or 0) for x in nonzero) / max(1, len(nonzero)), 6) if nonzero else 0.0
    )
    worst_match = round(min((float(x.get("match_rate", 0) or 0) for x in nonzero), default=0.0), 6)
    alerts = []
    if compared_total > 0 and float(match_rate) < match_alert:
        alerts.append(
            {
                "level": "warning",
                "rule": "current_match_rate_low",
                "value": round(float(match_rate), 6),
                "threshold": round(float(match_alert), 6),
            }
        )
    if compared_total > 0 and float(mismatch_rate) > mismatch_alert:
        alerts.append(
            {
                "level": "warning",
                "rule": "current_mismatch_rate_high",
                "value": round(float(mismatch_rate), 6),
                "threshold": round(float(mismatch_alert), 6),
            }
        )
    if nonzero and float(avg_match) < match_alert:
        alerts.append(
            {
                "level": "warning",
                "rule": "rolling_match_rate_low",
                "value": round(float(avg_match), 6),
                "threshold": round(float(match_alert), 6),
                "window_days": int(lookback_days),
            }
        )
    report["quality_trend"] = {
        "window_days": int(lookback_days),
        "sample_days": len(series),
        "effective_days": len(nonzero),
        "avg_match_rate": avg_match,
        "avg_mismatch_rate": avg_mismatch,
        "worst_match_rate": worst_match,
        "series": series[-max(1, min(lookback_days, 60)):],
    }
    report["alerts"] = alerts

    use_llm_optimize = not bool(getattr(args, "no_llm_optimize", False))
    if use_llm_optimize:
        llm_payload = {
            "trade_date": trade_date,
            "quality": report.get("quality", {}) or {},
            "quality_trend": report.get("quality_trend", {}) or {},
            "alerts": report.get("alerts", []) or [],
            "details": {
                "mismatched": (report.get("details", {}) or {}).get("mismatched", [])[:20],
                "unexpected": (report.get("details", {}) or {}).get("unexpected", [])[:20],
                "missed": (report.get("details", {}) or {}).get("missed", [])[:20],
            },
            "latest_plan": {
                "plan_id": str(latest_plan.get("plan_id", "") or ""),
                "constraints": latest_plan.get("constraints", {}) or {},
            },
        }
        report["llm_improvement"] = build_quality_improvement_advice(llm_payload)
    else:
        report["llm_improvement"] = {"summary": "disabled", "root_causes": [], "actions": [], "parameter_tuning": []}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report_file: {path}")
    return 0


def _sync_universe_core(
    *,
    repo: LongTermRepository,
    trade_date: str,
    refresh_industry: bool = False,
    refresh_ths_sector: bool = False,
) -> dict:
    portfolio = repo.load_portfolio()
    previous_universe = repo.load_universe()
    settings = repo.load_settings()
    gem_mode = bool(getattr(settings, "gem_universe", False))
    gem_prefix = str(getattr(settings, "gem_board_prefix", "30") or "30")
    previous = {
        str(item.code).upper(): normalize_industry_name(str(item.industry or "").strip())
        for item in previous_universe
    }
    data_source = OpenClawChinaStockDataSource()
    codes = [str(item.code).upper() for item in portfolio.positions]
    fetched_industry = data_source.fetch_industries(codes, refresh=bool(refresh_industry))
    fetched_ths_sectors = data_source.fetch_ths_sectors(codes, refresh=bool(refresh_ths_sector))
    merged_industry: dict = {}
    for code in codes:
        industry = str(fetched_industry.get(code, "") or "").strip()
        if not industry:
            industry = str(previous.get(code, "") or "").strip()
        concepts = list(fetched_ths_sectors.get(code, []) or [])
        if industry or concepts:
            merged_industry[code] = normalize_industry_with_concepts(industry, concepts)
    candidates = build_candidates_from_portfolio(
        portfolio,
        updated_at=trade_date,
        industry_map=merged_industry,
        sector_map=fetched_ths_sectors,
        existing_candidates=previous_universe,
    )

    def _is_gem_small_cap_candidate(item: StockCandidate) -> bool:
        code = str(item.code or "").upper()
        if not code.startswith(gem_prefix):
            return False
        thesis = str(item.thesis or "").strip().lower()
        tags = {str(x).strip() for x in (item.tags or []) if str(x).strip()}
        return thesis == "gem_small_cap" or {"创业板", "小市值"}.issubset(tags)

    # --- 科技动量模式：固定科技股池替代大范围扫描 ---
    high_tech_mode = bool(getattr(settings, "high_tech_mode", False))
    if high_tech_mode:
        from .high_tech_strategy import STOCK_POOL_NAMES, get_tech_pool_code_set
        tech_codes = get_tech_pool_code_set()
        existing_codes = {str(c.code).upper() for c in candidates}
        new_tech_codes = tech_codes - existing_codes
        for code in sorted(new_tech_codes):
            candidates.append(
                StockCandidate(
                    code=code,
                    name=str(STOCK_POOL_NAMES.get(code, code)),
                    status="candidate",
                    value_score=50.0,
                    quality_score=50.0,
                    growth_score=50.0,
                    risk_score=50.0,
                    industry="科技",
                    thesis="high_tech_pool",
                    tags=["科技"],
                    updated_at=trade_date,
                )
            )
        print(f"[HT] High-tech pool: {len(tech_codes)} stocks, {len(new_tech_codes)} new to universe")
        # 跳过板块轮动和GEM选股路径
    # --- 科技动量模式结束 ---
    # --- 外部选股：板块轮动预判选股（涨停前埋伏） ---
    # GEM mode is a separate small-cap universe. Do not leak legacy heat-rotation
    # names into scans or reports once it is enabled.
    if not high_tech_mode and not gem_mode:
        rotation_candidates = data_source.fetch_sector_rotation_candidates(
            top_sectors=8, max_per_sector=5, max_total=30,
        )
        rotation_codes = {str(item["code"]).upper() for item in rotation_candidates}
        # 获取新增股票的行业（仅对不在持仓中的）
        new_codes_for_industry = [c for c in rotation_codes if c not in codes]
        rotation_industry = data_source.fetch_industries(new_codes_for_industry) if new_codes_for_industry else {}
        rotation_sectors = data_source.fetch_ths_sectors(new_codes_for_industry) if new_codes_for_industry else {}
        # 合并外部候选到candidates列表
        for rc in rotation_candidates:
            code = str(rc["code"]).upper()
            # 已由持仓衍生，跳过
            if code in {str(c.code).upper() for c in candidates}:
                continue
            score = float(rc.get("momentum_score", 0) or 0)
            industry = str(rc.get("industry") or rotation_industry.get(code, "") or "").strip()
            if not industry:
                industry = "UNKNOWN"
            tags = [str(x).strip() for x in (rotation_sectors.get(code, []) or []) if str(x).strip()]
            if not tags:
                matched = str(rc.get("matched_sector") or "")
                if matched:
                    tags = [matched]
            score_scale = score / 100.0
            candidates.append(
                StockCandidate(
                    code=code,
                    name=str(rc.get("name") or code),
                    status="candidate",
                    value_score=round(50.0 + score_scale * 15.0, 2),
                    quality_score=round(50.0 + score_scale * 10.0, 2),
                    growth_score=round(55.0 + score_scale * 20.0, 2),
                    risk_score=round(60.0 - score_scale * 10.0, 2),
                    industry=normalize_industry_name(industry),
                    thesis=f"sector_rotation_{score:.0f}",
                    tags=tags[:12],
                    updated_at=trade_date,
                )
            )
    # --- 外部选股结束 ---
    # --- GEM小市值选股（创业板基本面筛选） ---
    if not high_tech_mode and gem_mode:
        try:
            gem_candidates = data_source.fetch_gem_candidates(settings)
            if gem_candidates:
                for gc in gem_candidates:
                    gcode = str(gc["code"]).upper()
                    if gcode in {str(c.code).upper() for c in candidates}:
                        continue
                    # Distribute GEM stocks across pseudo-industries to avoid cap
                    gem_industries = ["电子", "机械设备", "计算机", "医药生物", "电力设备", "基础化工"]
                    gem_idx = len([c for c in candidates if str(getattr(c, 'industry', '')).startswith("GEM_")])
                    candidates.append(StockCandidate(
                        code=gcode,
                        name=str(gc.get("name", gcode)),
                        status="watch",
                        value_score=50.0,
                        quality_score=50.0,
                        growth_score=50.0,
                        risk_score=60.0,
                        industry=gem_industries[gem_idx % len(gem_industries)],
                        thesis=f"gem_small_cap",
                        tags=["创业板", "小市值"],
                        updated_at=trade_date,
                    ))
                print(f"[GEM] Added {len(gem_candidates)} small-cap candidates to universe")
        except Exception as e:
            print(f"[GEM] Candidate fetch failed: {e}")
    # --- GEM选股结束 ---
    current_codes = {str(item.code).upper() for item in portfolio.positions}
    merged_candidates = {str(item.code).upper(): item for item in candidates}
    retained_count = 0
    for item in previous_universe:
        code = str(item.code).upper()
        if code in merged_candidates:
            continue
        if gem_mode and code not in current_codes and not _is_gem_small_cap_candidate(item):
            continue
        retained_count += 1
        status = str(item.status or "candidate").strip() or "candidate"
        if status == "active" and code not in current_codes:
            status = "watch"
        merged_candidates[code] = StockCandidate(
            code=code,
            name=item.name,
            status=status,
            value_score=float(item.value_score),
            quality_score=float(item.quality_score),
            growth_score=float(item.growth_score),
            risk_score=float(item.risk_score),
            industry=str(item.industry or "").strip() or "UNKNOWN",
            thesis=str(item.thesis or "").strip(),
            tags=list(item.tags or [])[:12],
            updated_at=item.updated_at or trade_date,
        )
    status_rank = {"active": 0, "watch": 1, "candidate": 2, "cooldown": 3, "exit": 4}
    merged_list = sorted(
        merged_candidates.values(),
        key=lambda item: (status_rank.get(str(item.status or ""), 9), str(item.code)),
    )
    repo.save_universe(merged_list)
    snapshot_path = repo.save_investor_snapshot(_build_investor_snapshot(repo, source="sync-universe"))
    known_industry = len(
        [
            x
            for x in merged_list
            if str(x.industry or "").strip() not in {"", "UNKNOWN", "其他"}
            and str(x.industry or "").strip().upper() != "UNKNOWN"
        ]
    )
    return {
        "candidates_count": len(merged_list),
        "known_industry_count": known_industry,
        "retained_count": retained_count,
        "snapshot_path": snapshot_path,
    }


def cmd_sync_universe_from_portfolio(args: argparse.Namespace) -> int:
    repo = LongTermRepository()
    repo.init_if_missing()
    result = _sync_universe_core(
        repo=repo,
        trade_date=args.date or datetime.now().strftime("%Y-%m-%d"),
        refresh_industry=bool(getattr(args, "refresh_industry", False)),
        refresh_ths_sector=bool(getattr(args, "refresh_ths_sector", False)),
    )
    print(f"synced universe from portfolio: {int(result.get('candidates_count', 0) or 0)}")
    print(
        "industry_enriched: "
        f"{int(result.get('known_industry_count', 0) or 0)}/{int(result.get('candidates_count', 0) or 0)}"
    )
    print(f"retained_non_position: {int(result.get('retained_count', 0) or 0)}")
    print(f"investor_snapshot: {result.get('snapshot_path')}")
    return 0


def _run_post_market_scan_core(
    *,
    repo: LongTermRepository,
    trade_date: str,
    top_k: int,
    use_llm: bool,
) -> dict:
    universe = repo.load_universe()
    portfolio = repo.load_portfolio()
    if not universe:
        return {"error": "universe_empty"}
    data_source = OpenClawChinaStockDataSource()
    settings = repo.load_settings()
    codes = sorted({item.code.upper() for item in universe})
    quotes = data_source.fetch_quotes(codes)
    history_by_code = data_source.fetch_batch_daily_history(codes, lookback_days=60, end_date=trade_date)
    heat_by_code = data_source.fetch_ths_hotness(
        codes,
        trade_date=trade_date,
        limit_up_weight=float(getattr(settings, "ths_heat_limit_up_weight", 2.0)),
        change_weight=float(getattr(settings, "ths_heat_change_weight", 18.0)),
    )
    payload = run_post_market_scan(
        trade_date=trade_date,
        universe=universe,
        portfolio=portfolio,
        quotes=quotes,
        history_by_code=history_by_code,
        heat_by_code=heat_by_code,
        settings=settings,
        top_k=int(top_k),
        use_llm=bool(use_llm),
    )
    report_path = repo.save_post_market_scan(payload)
    snapshot_path = repo.save_investor_snapshot(_build_investor_snapshot(repo, source="post-market-scan"))
    return {
        "payload": payload,
        "report_path": report_path,
        "snapshot_path": snapshot_path,
    }


def cmd_post_market_scan(args: argparse.Namespace) -> int:
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    repo = LongTermRepository()
    repo.init_if_missing()
    result = _run_post_market_scan_core(
        repo=repo,
        trade_date=trade_date,
        top_k=int(args.top_k),
        use_llm=not bool(args.no_llm),
    )
    if result.get("error") == "universe_empty":
        print("universe is empty, run sync-universe-from-portfolio or init --seed-universe first")
        return 1
    payload = result.get("payload", {}) or {}
    report_path = result.get("report_path")
    snapshot_path = result.get("snapshot_path")
    print(f"trade_date: {trade_date}")
    print(f"scan_file: {report_path}")
    print(
        f"universe: {payload.get('universe_count', 0)} top_k: {payload.get('top_k', 0)} "
        f"llm: {payload.get('llm_enabled')} fallback={payload.get('fallback_count', 0)} "
        f"heat_coverage={payload.get('heat_coverage_count', 0)}"
    )
    print(f"suggested_watchlist: {len(payload.get('suggested_watchlist', []) or [])}")
    for item in (payload.get("suggested_watchlist", []) or [])[:20]:
        llm = item.get("llm", {}) or {}
        verdict = str(llm.get("verdict", "") or "heuristic")
        print(
            f"- {item.get('code')} {item.get('name')} score={float(item.get('score', 0)):.2f} "
            f"chg={float(item.get('change_percent', 0)):.2f}% "
            f"heat={float(item.get('ths_heat_score', 0) or 0):.1f} verdict={verdict}"
        )
    print(f"investor_snapshot: {snapshot_path}")
    return 0


def cmd_check_trading_day(args: argparse.Namespace) -> int:
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    is_open, source = is_cn_trading_day(trade_date)
    status = "trading_day" if is_open else "non_trading_day"
    print(f"{trade_date} {status} source={source}")
    return 0 if is_open else 1


def _build_market_snapshot(codes: List[str], quotes: dict) -> dict:
    rows = []
    for code in codes:
        q = quotes.get(code, {}) or {}
        rows.append(
            {
                "code": code,
                "price": float(q.get("price", 0) or 0),
                "change_percent": float(q.get("change_percent", 0) or 0),
                "amount": float(q.get("amount", 0) or 0),
            }
        )
    valid = [x for x in rows if x["price"] > 0]
    if not valid:
        return {
            "data_available": False,
            "sample_count": len(rows),
            "up_count": 0,
            "down_count": 0,
            "avg_change_percent": 0.0,
            "amount_percentiles": {"p50": 0.0, "p80": 0.0, "p95": 0.0},
            "rows": rows,
        }
    amounts = sorted([float(x.get("amount", 0) or 0) for x in valid if float(x.get("amount", 0) or 0) > 0])

    def _quantile(q: float) -> float:
        if not amounts:
            return 0.0
        idx = int((len(amounts) - 1) * q)
        return round(float(amounts[idx]), 2)

    for row in valid:
        amt = float(row.get("amount", 0) or 0)
        if not amounts or amt <= 0:
            row["amount_percentile"] = 0.0
            continue
        le = len([x for x in amounts if x <= amt])
        row["amount_percentile"] = round(le / max(1, len(amounts)), 4)
    up_count = len([x for x in valid if x["change_percent"] > 0])
    down_count = len([x for x in valid if x["change_percent"] < 0])
    avg_chg = sum(x["change_percent"] for x in valid) / max(1, len(valid))
    return {
        "data_available": True,
        "sample_count": len(valid),
        "up_count": up_count,
        "down_count": down_count,
        "avg_change_percent": round(avg_chg, 4),
        "amount_percentiles": {"p50": _quantile(0.50), "p80": _quantile(0.80), "p95": _quantile(0.95)},
        "rows": valid,
    }


def _build_index_snapshot(data_source: OpenClawChinaStockDataSource) -> dict:
    proxies = {
        "510300": "沪深300ETF",
        "510500": "中证500ETF",
        "159915": "创业板ETF",
    }
    quotes = data_source.fetch_quotes(list(proxies.keys()))
    rows = []
    for code, name in proxies.items():
        q = quotes.get(code, {}) or {}
        rows.append(
            {
                "code": code,
                "name": name,
                "price": float(q.get("price", 0) or 0),
                "change_percent": float(q.get("change_percent", 0) or 0),
                "amount": float(q.get("amount", 0) or 0),
            }
        )
    valid = [x for x in rows if x["price"] > 0]
    return {
        "data_available": bool(valid),
        "sample_count": len(valid),
        "rows": valid if valid else rows,
    }


def _build_industry_breadth(universe: List[StockCandidate], quotes: dict) -> dict:
    buckets: dict = {}
    for item in universe:
        code = item.code.upper()
        q = quotes.get(code, {}) or {}
        chg = float(q.get("change_percent", 0) or 0)
        industry = str(item.industry or "UNKNOWN").strip() or "UNKNOWN"
        stat = buckets.setdefault(industry, {"industry": industry, "count": 0, "up": 0, "down": 0, "flat": 0})
        stat["count"] += 1
        if chg > 0:
            stat["up"] += 1
        elif chg < 0:
            stat["down"] += 1
        else:
            stat["flat"] += 1
    rows = []
    for stat in buckets.values():
        count = int(stat["count"])
        rows.append(
            {
                **stat,
                "up_ratio": round(float(stat["up"]) / count if count > 0 else 0.0, 4),
                "down_ratio": round(float(stat["down"]) / count if count > 0 else 0.0, 4),
            }
        )
    rows.sort(key=lambda x: (x["count"], x["up_ratio"]), reverse=True)
    return {"industry_count": len(rows), "rows": rows[:12]}


def _load_external_market_inputs(trade_date: str) -> dict:
    candidate_paths = []
    env_path = str(os.getenv("LONGTERM_MARKET_STRUCTURE_FILE", "") or "").strip()
    if env_path:
        candidate_paths.append(Path(env_path).expanduser().resolve())
    candidate_paths.append(Path(__file__).resolve().parents[2] / "trading_data" / "longterm" / "market_structure_inputs.json")

    for fp in candidate_paths:
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        if trade_date in raw and isinstance(raw.get(trade_date), dict):
            payload = dict(raw.get(trade_date) or {})
        else:
            payload = dict(raw)
        out = {
            "northbound_net_inflow": float(payload.get("northbound_net_inflow", 0) or 0),
            "if_main_basis": float(payload.get("if_main_basis", 0) or 0),
            "ih_main_basis": float(payload.get("ih_main_basis", 0) or 0),
            "ic_main_basis": float(payload.get("ic_main_basis", 0) or 0),
            "im_main_basis": float(payload.get("im_main_basis", 0) or 0),
        }
        out["source"] = str(fp)
        return out
    return {
        "northbound_net_inflow": 0.0,
        "if_main_basis": 0.0,
        "ih_main_basis": 0.0,
        "ic_main_basis": 0.0,
        "im_main_basis": 0.0,
        "source": "",
    }


def _build_market_structure_signals(
    market_snapshot: dict,
    index_snapshot: dict,
    industry_breadth: dict,
    *,
    external_inputs: dict | None = None,
) -> dict:
    rows = list((market_snapshot.get("rows", []) or []))
    valid = [x for x in rows if float(x.get("price", 0) or 0) > 0]
    up_count = int(market_snapshot.get("up_count", 0) or 0)
    down_count = int(market_snapshot.get("down_count", 0) or 0)
    adv_dec = round(up_count / max(1, down_count), 4)
    breadth = round(up_count / max(1, up_count + down_count), 4)

    limit_up_like = len([x for x in valid if float(x.get("change_percent", 0) or 0) >= 9.5])
    limit_down_like = len([x for x in valid if float(x.get("change_percent", 0) or 0) <= -9.5])

    total_amount = sum(float(x.get("amount", 0) or 0) for x in valid if float(x.get("amount", 0) or 0) > 0)
    top_amount = sorted([float(x.get("amount", 0) or 0) for x in valid if float(x.get("amount", 0) or 0) > 0], reverse=True)[:10]
    concentration_top10 = round((sum(top_amount) / total_amount), 4) if total_amount > 0 else 0.0

    idx_rows = list((index_snapshot.get("rows", []) or []))
    idx_valid = [x for x in idx_rows if float(x.get("price", 0) or 0) > 0]
    idx_chg = [float(x.get("change_percent", 0) or 0) for x in idx_valid]
    idx_avg = sum(idx_chg) / max(1, len(idx_chg))
    idx_disp = 0.0
    if idx_chg:
        idx_disp = (sum((x - idx_avg) ** 2 for x in idx_chg) / max(1, len(idx_chg))) ** 0.5
    growth_proxy = next((x for x in idx_valid if str(x.get("code")) == "159915"), {})
    large_proxy = next((x for x in idx_valid if str(x.get("code")) == "510300"), {})
    style_spread = round(
        float(growth_proxy.get("change_percent", 0) or 0) - float(large_proxy.get("change_percent", 0) or 0),
        4,
    )

    ind_rows = list((industry_breadth.get("rows", []) or []))
    industry_up_median = 0.0
    if ind_rows:
        ratios = sorted([float(x.get("up_ratio", 0) or 0) for x in ind_rows])
        industry_up_median = round(ratios[len(ratios) // 2], 4)

    ext = dict(external_inputs or {})
    northbound_net = float(ext.get("northbound_net_inflow", 0) or 0)
    basis_if = float(ext.get("if_main_basis", 0) or 0)
    basis_ih = float(ext.get("ih_main_basis", 0) or 0)
    basis_ic = float(ext.get("ic_main_basis", 0) or 0)
    basis_im = float(ext.get("im_main_basis", 0) or 0)
    basis_avg = (basis_if + basis_ih + basis_ic + basis_im) / 4.0

    risk_on_score = round(
        breadth * 45
        + max(-1.0, min(1.0, float(market_snapshot.get("avg_change_percent", 0) or 0) / 2.0)) * 15
        + max(-1.0, min(1.0, style_spread / 1.5)) * 15
        + max(-1.0, min(1.0, (industry_up_median - 0.5) * 2.0)) * 15
        + max(-1.0, min(1.0, northbound_net / 3_000_000_000.0)) * 10
        + max(-1.0, min(1.0, basis_avg / 10.0)) * 10
        - max(0.0, min(1.0, concentration_top10)) * 10,
        4,
    )
    regime = "neutral"
    if risk_on_score >= 22:
        regime = "risk_on"
    elif risk_on_score <= -22:
        regime = "risk_off"

    return {
        "adv_dec_ratio": adv_dec,
        "breadth_ratio": breadth,
        "limit_up_like_count": int(limit_up_like),
        "limit_down_like_count": int(limit_down_like),
        "amount_concentration_top10": concentration_top10,
        "index_dispersion": round(float(idx_disp), 4),
        "style_spread_growth_minus_large": style_spread,
        "industry_up_ratio_median": industry_up_median,
        "northbound_net_inflow": round(northbound_net, 2),
        "if_main_basis": round(basis_if, 4),
        "ih_main_basis": round(basis_ih, 4),
        "ic_main_basis": round(basis_ic, 4),
        "im_main_basis": round(basis_im, 4),
        "market_inputs_source": str(ext.get("source", "") or ""),
        "risk_on_score": risk_on_score,
        "regime_hint": regime,
    }


def _build_scan_heat_summary(latest_scan: dict) -> dict:
    candidates = (latest_scan.get("top_candidates", []) or [])[:20]
    rows = []
    concept_count: dict = {}
    for item in candidates:
        code = str(item.get("code", "") or "").strip().upper()
        name = str(item.get("name", "") or "").strip()
        heat = float(item.get("ths_heat_score", 0) or 0)
        concepts = [str(x).strip() for x in (item.get("ths_hot_concepts", []) or []) if str(x).strip()]
        if heat <= 0 and not concepts:
            continue
        rows.append(
            {
                "code": code,
                "name": name,
                "heat_score": round(heat, 3),
                "hot_concepts": concepts[:3],
                "score": float(item.get("score", 0) or 0),
            }
        )
        for concept in concepts:
            concept_count[concept] = int(concept_count.get(concept, 0)) + 1
    rows.sort(key=lambda x: (x["heat_score"], x["score"]), reverse=True)
    top_concepts = [
        {"concept": k, "count": v}
        for k, v in sorted(concept_count.items(), key=lambda kv: kv[1], reverse=True)[:8]
    ]
    return {
        "coverage_count": len(rows),
        "top_heat_candidates": rows[:8],
        "top_hot_concepts": top_concepts,
    }


def _compact_join(items: List[str], *, max_items: int = 4, empty_text: str = "无") -> str:
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    if not cleaned:
        return empty_text
    clipped = cleaned[: max(1, int(max_items or 1))]
    text = "；".join(clipped)
    if len(cleaned) > len(clipped):
        text += f"；等{len(cleaned)}项"
    return text


def _format_decision_adjustments(llm_decision: dict, *, max_items: int = 4) -> str:
    rows = []
    for item in (llm_decision.get("adjustments", []) or [])[: max(1, int(max_items or 1))]:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "") or "").strip().upper()
        action = str(item.get("action", "") or "").strip().lower()
        note = str(item.get("note", "") or "").strip()
        core = f"{code}:{action}" if code else action
        if note:
            core = f"{core}({note})" if core else note
        if core:
            rows.append(core)
    return _compact_join(rows, max_items=max_items, empty_text="无")


def _strategy_profile(settings: LongTermSettings) -> dict:
    gradual = bool(getattr(settings, "gradual_rotation", True))
    high_tech_mode = bool(getattr(settings, "high_tech_mode", False))
    gem_mode = bool(getattr(settings, "gem_universe", False))
    if high_tech_mode:
        ht_n = int(getattr(settings, "ht_holdings_num", 5) or 5)
        name = "科技动量策略"
        focus = (
            "固定科技股池（光模块/CPO/存储芯片/航天等32只），"
            f"动量评分（年化收益率×R²×加速度×量比），选Top{ht_n}等权分配；"
            "建议尾盘14:50-15:00通过飞书命令手动执行调仓"
        )
        return {
            "name": name,
            "report_title": "科技动量盘后建议",
            "focus": focus,
        }
    if gem_mode:
        name = "创业板小市值组合管理" if gradual else "创业板小市值策略"
        focus = (
            "组合管理模式：综合评估每只持仓质量（PnL/主题/热度/趋势），"
            "按去留优先级渐进换仓（非一键清仓），"
            "卖出释放资金后再规划新买入（单次最多3只），"
            "非核心持仓分2-3周期逐步退出。"
            if gradual
            else (
                "仅以创业板30开头股票为新买入池，优先小市值、基本面增长和可交易性；"
                "当前非创业板持仓只作为退出/过渡仓位处理"
            )
        )
        return {
            "name": name,
            "report_title": "创业板小市值盘后建议",
            "focus": focus,
        }
    return {
        "name": "长线组合",
        "report_title": "长线盘后建议",
        "focus": "中长期组合质量、行业/主题集中度、交易闭环和风险预算",
    }


def _format_plan_action_details(latest_plan: dict, *, max_items: int = 0) -> str:
    """格式化为飞书可读的调仓明细。max_items=0 表示全部输出。"""
    actions = latest_plan.get("actions", []) or []
    rejected = latest_plan.get("rejected_actions", []) or []
    limit = int(max_items) if max_items and max_items > 0 else max(len(actions), 1)
    lines = ["", "**买入**  "]
    buy_count = 0
    total_buy = 0.0
    for item in actions:
        if item.get("action", "") != "buy":
            continue
        buy_count += 1
        if buy_count > limit:
            break
        code = str(item.get("code", "") or "").strip().upper()
        name = str(item.get("name", "") or "").strip()
        qty = abs(int(item.get("delta_shares", 0) or 0))
        px = float(item.get("reference_price", 0) or 0)
        amt = float(item.get("estimated_amount", 0) or 0)
        total_buy += amt
        weight = float(item.get("target_weight", 0) or 0)
        concepts = _extract_item_concepts(code, latest_plan)
        tag = _concept_tag(concepts)
        display = f"{name}（{code}）" if name and name != code else code
        theme_note = f"；主题：{tag}" if tag != "-" else ""
        lines.append(f"- {display}：买入 {qty} 股，参考 {px:.2f} 元，预计 {amt:,.0f} 元，目标权重 {weight:.1%}{theme_note}")
    if buy_count == 0:
        lines.append("（无）")
    lines.append(f"　买入合计: **{buy_count} 笔 / {total_buy:,.0f} 元**")
    lines.append("")
    lines.append("**卖出**  ")
    sell_count = 0
    total_sell = 0.0
    for item in actions:
        if item.get("action", "") != "sell":
            continue
        sell_count += 1
        if sell_count > limit:
            break
        code = str(item.get("code", "") or "").strip().upper()
        name = str(item.get("name", "") or "").strip()
        qty = abs(int(item.get("delta_shares", 0) or 0))
        px = float(item.get("reference_price", 0) or 0)
        amt = float(item.get("estimated_amount", 0) or 0)
        total_sell += amt
        reason = _human_action_reason(item.get("reason"))
        display = f"{name}（{code}）" if name and name != code else code
        lines.append(f"- {display}：卖出 {qty} 股，参考 {px:.2f} 元，预计 {amt:,.0f} 元；原因：{reason}")
    if sell_count == 0:
        lines.append("（无）")
    lines.append(f"　卖出合计: **{sell_count} 笔 / {total_sell:,.0f} 元**")
    if rejected:
        lines.append("")
        lines.append("**被拒**  ")
        for r in rejected[:6]:
            code = str(r.get("code", "") or "").strip().upper()
            name = str(r.get("name", "") or "").strip()
            reason = _human_action_reason(r.get("reason"))
            display = f"{name}（{code}）" if name and name != code else code
            lines.append(f"- {display}：{reason}")
    lines.append("")
    return "\n".join(lines)


def _decision_label(value: object) -> str:
    raw = str(value or "").strip().lower()
    return {
        "pause": "暂停执行",
        "hold": "持有观察",
        "proceed": "按计划观察",
        "execute": "可进入人工执行核验",
        "adjust": "调整计划",
        "buy": "买入候选",
        "sell": "卖出候选",
    }.get(raw, str(value or "待人工确认"))


def _market_regime_label(value: object) -> str:
    raw = str(value or "").strip().lower()
    return {
        "risk_on": "偏进攻",
        "risk_off": "偏防守",
        "neutral": "中性",
    }.get(raw, "状态待确认")


def _confidence_pct(value: object) -> float:
    number = _float(value)
    return max(0.0, min(100.0, number * 100 if number <= 1 else number))


def _human_action_reason(value: object) -> str:
    reason = str(value or "").strip()
    lowered = reason.lower()
    matched = re.search(r"stop_loss\(px=([0-9.]+)", lowered)
    if matched:
        return f"跌破止损位 {float(matched.group(1)):.2f} 元"
    if "not_in_target_universe" in lowered:
        return "已调出当前候选池"
    if "theme_cap_violation" in lowered:
        return "主题集中度超过上限"
    if "industry_cap" in lowered:
        return "行业集中度超过上限"
    if "min_trade" in lowered:
        return "交易金额低于最小执行标准"
    if "target_weight" in lowered or "current_weight" in lowered:
        return "按目标权重调整"
    cleaned = reason.replace("_", " ").strip()
    return cleaned[:60] if cleaned else "策略风险控制"


def _extract_item_concepts(code: str, latest_plan: dict) -> List[str]:
    """从计划的 rejected_actions 中反推股票的关联概念（主题cap信息）。"""
    concepts: List[str] = []
    for r in (latest_plan.get("rejected_actions", []) or []):
        if str(r.get("code", "") or "").strip().upper() != code.upper():
            continue
        reason = str(r.get("reason", "") or "")
        # theme_cap_violation(机器人:38.76%>35.00%)
        import re
        m = re.search(r"theme_cap_violation\((.+?):", reason)
        if m:
            concepts.append(m.group(1).strip())
    return concepts


def _concept_tag(concepts: List[str]) -> str:
    if not concepts:
        return "-"
    return "/".join(concepts[:2])


def _format_heat_candidate_details(scan_heat_summary: dict, *, max_items: int = 0) -> str:
    """格式化为飞书 Markdown 表格。"""
    rows = []
    for item in (scan_heat_summary.get("top_heat_candidates", []) or []):
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "") or "").strip().upper()
        heat = float(item.get("heat_score", 0) or 0)
        score = float(item.get("score", 0) or 0)
        concepts = [str(x).strip() for x in (item.get("hot_concepts", []) or []) if str(x).strip()]
        tag = concepts[0] if concepts else "-"
        rows.append((code, heat, score, tag))
    if not rows:
        return "（无）"
    limit = int(max_items) if max_items and max_items > 0 else len(rows)
    lines = [""]
    for code, heat, score, tag in rows[:limit]:
        lines.append(f"- {code}：市场热度 {heat:.1f}，综合评分 {score:.0f}，主题 {tag}")
    lines.append("")
    return "\n".join(lines)


def _build_feishu_card(title: str, sections: List[dict], *, template: str = "blue") -> dict:
    return build_feishu_report_card(title=title or "长线决策", sections=sections, template=template)


def _compute_sl_tp_levels(
    portfolio: PortfolioState,
    history_by_code: Dict[str, Dict[str, List[float]]],
    settings: LongTermSettings,
) -> Dict[str, Dict]:
    """计算每只持仓的止损价和止盈价。

    基于 ATR（平均真实波幅）计算动态止损/止盈。
    """
    import math
    levels: Dict[str, Dict] = {}
    for pos in portfolio.positions:
        if pos.quantity <= 0:
            continue
        code = str(pos.code).upper()
        code_key = code.split(".")[0]
        history = history_by_code.get(code, history_by_code.get(code_key, {})) or {}
        closes = [x for x in history.get("close", []) if _float(x) > 0]
        highs = [x for x in history.get("high", []) if _float(x) > 0]
        lows = [x for x in history.get("low", []) if _float(x) > 0]
        px = float(pos.last_price or pos.cost_price)
        if px <= 0:
            px = closes[-1] if closes else 0.0
        if px <= 0:
            continue

        # 计算 ATR% (14日)
        atr_pct = 0.03  # 默认3%
        n = min(len(closes), len(highs), len(lows))
        if n >= 15:
            tr_list = []
            for i in range(1, n):
                h, l, pc = highs[i], lows[i], closes[i - 1]
                tr = max(h - l, abs(h - pc), abs(l - pc)) if pc > 0 else h - l
                if math.isfinite(tr) and tr > 0:
                    tr_list.append(tr / max(closes[i], 1.0))
            if len(tr_list) >= 14:
                atr_pct = sum(sorted(tr_list)[-14:]) / 14

        # 动态止损：ATR × 2.5，至少 5%，最多 12%
        stop_loss_pct = max(0.05, min(0.12, atr_pct * 2.5))
        take_profit_pct = max(0.08, min(0.25, atr_pct * 5.0))

        cost = float(pos.cost_price)
        levels[code] = {
            "close": round(px, 2),
            "atr_pct": round(atr_pct * 100, 2),
            "stop_loss_price": round(px * (1 - stop_loss_pct), 2),
            "stop_loss_pct": round(-stop_loss_pct * 100, 1),
            "take_profit_price": round(px * (1 + take_profit_pct), 2),
            "take_profit_pct": round(take_profit_pct * 100, 1),
            "cost_stop_loss": round(cost * 0.92, 2) if cost > 0 else 0.0,
        }
    return levels


def cmd_evening_decision(args: argparse.Namespace) -> int:
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    if not bool(args.ignore_trading_calendar):
        is_open, source = is_cn_trading_day(trade_date)
        if not is_open:
            print(f"skip evening-decision: {trade_date} non_trading_day source={source}")
            return 0
    else:
        # When ignoring trading calendar, auto-resolve to last trading day
        is_open, _ = is_cn_trading_day(trade_date)
        if not is_open:
            resolved = last_trading_day(trade_date)
            if resolved != trade_date:
                print(f"[WARN] {trade_date} is non-trading day, using last trading day: {resolved}")
                trade_date = resolved
    repo = LongTermRepository()
    repo.init_if_missing()
    settings_for_review = repo.load_settings()

    # --- 科技动量模式：复盘午后计划 → 止损止盈建议 ---
    if bool(getattr(settings_for_review, "high_tech_mode", False)):
        latest_plan = repo.load_latest_plan() or {}
        portfolio = repo.load_portfolio()
        need_refresh_plan = (
            not latest_plan.get("actions")
            or str(latest_plan.get("trade_date", "") or "").strip() != trade_date
            or str(latest_plan.get("source", "") or "").strip() != "high-tech-momentum"
        )
        if need_refresh_plan:
            print("[HT evening] stale/missing plan, rebuilding high-tech plan before review")
            sync_result = _sync_universe_core(
                repo=repo,
                trade_date=trade_date,
                refresh_industry=False,
                refresh_ths_sector=False,
            )
            print(f"[HT evening] universe synced: {int(sync_result.get('candidates_count', 0) or 0)}")
            universe = repo.load_universe()
            data_source = OpenClawChinaStockDataSource()
            codes = sorted({item.code.upper() for item in universe} | {item.code.upper() for item in portfolio.positions})
            quotes = data_source.fetch_quotes(codes)
            history_by_code = data_source.fetch_batch_daily_history(
                codes,
                lookback_days=int(getattr(settings_for_review, "ht_lookback_days", 20) or 20) + 10,
            )
            plan, mtm_portfolio = build_rebalance_plan(
                trade_date=trade_date,
                candidates=universe,
                portfolio=portfolio,
                quotes=quotes,
                settings=settings_for_review,
                history_by_code=history_by_code,
            )
            repo.save_plan(plan)
            repo.save_portfolio(mtm_portfolio)
            latest_plan = repo.load_latest_plan() or {}
            portfolio = repo.load_portfolio()
        if not latest_plan.get("actions"):
            print("[HT evening] no valid high-tech plan generated")
            return 0

        # 获取盘后行情数据
        data_source = OpenClawChinaStockDataSource()
        codes = sorted({item.code.upper() for item in portfolio.positions})
        quotes = data_source.fetch_quotes(codes)

        # 获取历史数据用于 ATR 计算
        history_by_code = data_source.fetch_batch_daily_history(codes, lookback_days=30)

        # 计算止损止盈
        sl_tp_levels = _compute_sl_tp_levels(portfolio, history_by_code, settings_for_review)

        # LLM 复盘
        execution_monitor = _build_execution_monitor(repo, latest_plan)
        execution_note = _execution_monitor_note(execution_monitor)
        strategy = _strategy_profile(settings_for_review)
        decision_payload = {
            "trade_date": trade_date,
            "stage": "evening_review",
            "strategy": {"name": strategy["name"], "focus": "盘后复盘：回顾午后调仓计划，提供次日止损止盈建议"},
            "portfolio": {
                "nav": portfolio.nav,
                "cash": portfolio.cash,
                "available_cash": portfolio.available_cash,
                "realized_pnl": portfolio.realized_pnl,
                "unrealized_pnl": portfolio.unrealized_pnl,
                "total_pnl": portfolio.total_pnl,
                "total_return_pct": portfolio.total_return_pct,
                "holdings_count": len(portfolio.positions),
            },
            "afternoon_plan": {
                "actions": latest_plan.get("actions", []) or [],
                "rejected_actions": latest_plan.get("rejected_actions", []) or [],
            },
            "sl_tp_levels": sl_tp_levels,
            "execution_monitor": execution_monitor,
        }
        llm_decision = build_evening_decision(decision_payload)
        key_risks = [str(x).strip() for x in (llm_decision.get("key_risks", []) or []) if str(x).strip()]
        llm_decision["key_risks"] = key_risks

        # 保存决策记录
        record = {
            "stage": "evening_review",
            "trade_date": trade_date,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "llm_decision": llm_decision,
            "plan_id": latest_plan.get("plan_id", ""),
            "sl_tp_levels": sl_tp_levels,
            "execution_monitor": execution_monitor,
        }
        repo.save_decision("evening", record)
        snapshot_path = repo.save_investor_snapshot(
            _build_investor_snapshot(repo, source="evening-review-ht")
        )

        # 格式化输出
        decision_risks_compact = _compact_join(
            [str(x).strip() for x in (llm_decision.get("key_risks", []) or []) if str(x).strip()],
            max_items=6, empty_text="无",
        )
        plan_action_table = _format_plan_action_details(latest_plan)
        portfolio_return_summary = _format_portfolio_return_summary(portfolio)

        # 止损止盈表格
        sl_tp_lines = []
        for code, info in sorted(sl_tp_levels.items()):
            sl_tp_lines.append(
                f"- {code}：收盘 {info['close']:.2f} 元；"
                f"止损 {info['stop_loss_price']:.2f} 元（{info['stop_loss_pct']}%）；"
                f"止盈 {info['take_profit_price']:.2f} 元（{info['take_profit_pct']}%）；"
                f"近期波动 {info['atr_pct']:.1f}%"
            )
        sl_tp_table = "\n".join(sl_tp_lines) if sl_tp_lines else "- 当前空仓，无需设置止损止盈。"

        text = (
            f"【长线科技动量收盘复盘】{trade_date}\n"
            f"{portfolio_return_summary}\n"
            f"核心结论：{llm_decision.get('summary', '')}\n"
            f"关键风险：{decision_risks_compact}\n"
            f"执行核验：{execution_note}\n"
            f"调仓建议：\n{plan_action_table}"
            f"次日止损止盈参考：\n{sl_tp_table}\n"
            "风险边界：这是模拟组合的人工决策建议，不代表券商委托或成交；不会自动下单。\n"
        )
        card = _build_feishu_card(
            f"长线科技动量收盘复盘 {trade_date}",
            [
                {"title": "组合收益", "content": portfolio_return_summary},
                {"title": "核心结论", "content": llm_decision.get("summary", "")},
                {"title": "关键风险", "content": decision_risks_compact},
                {"title": "执行核验", "content": execution_note},
                {"title": "调仓建议", "content": plan_action_table},
                {"title": "次日止损止盈参考", "content": sl_tp_table},
                {
                    "title": "风险边界",
                    "content": (
                        "🔴 跌破止损价 → 进入人工卖出核验\n"
                        "🟢 触及止盈价 → 进入人工分批止盈核验\n"
                        "这是模拟组合建议，不代表券商委托或成交；不会自动下单。"
                    ),
                },
            ],
            template="purple",
        )
        print(text)
        pushed = False if bool(args.no_push) else push_feishu_rich(text, card=card)
        print(f"[HT evening] Feishu pushed: {pushed}")
        print(f"  snapshot: {snapshot_path}")
        return 0
    # --- 科技动量模式结束 ---

    if not bool(args.skip_sync_universe):
        sync_result = _sync_universe_core(repo=repo, trade_date=trade_date, refresh_industry=False, refresh_ths_sector=False)
        print(f"synced universe from portfolio: {int(sync_result.get('candidates_count', 0) or 0)}")
        print(
            "industry_enriched: "
            f"{int(sync_result.get('known_industry_count', 0) or 0)}/{int(sync_result.get('candidates_count', 0) or 0)}"
        )
        print(f"investor_snapshot: {sync_result.get('snapshot_path')}")

    scan_result = _run_post_market_scan_core(
        repo=repo,
        trade_date=trade_date,
        top_k=int(args.top_k),
        use_llm=not bool(args.no_llm_scan),
    )
    if scan_result.get("error") == "universe_empty":
        print("universe is empty, run sync-universe-from-portfolio or init --seed-universe first")
        return 1
    scan_payload = scan_result.get("payload", {}) or {}
    print(f"trade_date: {trade_date}")
    print(f"scan_file: {scan_result.get('report_path')}")
    print(
        f"universe: {scan_payload.get('universe_count', 0)} top_k: {scan_payload.get('top_k', 0)} "
        f"llm: {scan_payload.get('llm_enabled')} fallback={scan_payload.get('fallback_count', 0)} "
        f"heat_coverage={scan_payload.get('heat_coverage_count', 0)}"
    )
    print(f"suggested_watchlist: {len(scan_payload.get('suggested_watchlist', []) or [])}")
    for item in (scan_payload.get("suggested_watchlist", []) or [])[:20]:
        llm = item.get("llm", {}) or {}
        verdict = str(llm.get("verdict", "") or "heuristic")
        print(
            f"- {item.get('code')} {item.get('name')} score={float(item.get('score', 0)):.2f} "
            f"chg={float(item.get('change_percent', 0)):.2f}% "
            f"heat={float(item.get('ths_heat_score', 0) or 0):.1f} verdict={verdict}"
        )
    print(f"investor_snapshot: {scan_result.get('snapshot_path')}")
    # Pass rotation_scan if rotation_mode is enabled
    run_review_args = dict(
        date=trade_date,
        single_name_cap=None,
        cash_buffer_ratio=None,
        rebalance_threshold=None,
        max_industry_weight=None,
        min_trade_amount=None,
        max_holdings=None,
        max_portfolio_volatility=None,
        max_portfolio_drawdown=None,
    )
    ns = argparse.Namespace(**run_review_args)
    # Inject rotation_scan payload onto the namespace for build_rebalance_plan
    settings_for_review = repo.load_settings()
    if getattr(settings_for_review, "rotation_mode", False):
        ns.rotation_scan = scan_payload
    cmd_run_review(ns)
    latest_scan = repo.load_latest_post_market_scan() or {}
    latest_plan = repo.load_latest_plan() or {}
    portfolio = repo.load_portfolio()
    strategy = _strategy_profile(settings_for_review)
    execution_monitor = _build_execution_monitor(repo, latest_plan)
    execution_note = _execution_monitor_note(execution_monitor)
    scan_heat_summary = _build_scan_heat_summary(latest_scan)
    # Determine decision mode: --final overrides default "draft"
    decision_mode = "final" if bool(getattr(args, "final", False)) else str(
        getattr(settings_for_review, "evening_decision_mode", "draft") or "draft"
    ).strip().lower()
    if decision_mode not in ("draft", "final"):
        decision_mode = "draft"

    decision_payload = {
        "trade_date": trade_date,
        "strategy": {
            "name": strategy["name"],
            "focus": strategy["focus"],
            "gem_universe": bool(getattr(settings_for_review, "gem_universe", False)),
            "gem_board_prefix": str(getattr(settings_for_review, "gem_board_prefix", "30") or "30"),
        },
        "portfolio": {
            "nav": portfolio.nav,
            "cash": portfolio.cash,
            "available_cash": portfolio.available_cash,
            "frozen_cash": portfolio.frozen_cash,
            "realized_pnl": portfolio.realized_pnl,
            "unrealized_pnl": portfolio.unrealized_pnl,
            "total_pnl": portfolio.total_pnl,
            "total_return_pct": portfolio.total_return_pct,
            "holdings_count": len(portfolio.positions),
        },
        "scan": {
            "top_candidates": (latest_scan.get("top_candidates", []) or [])[:10],
            "suggested_watchlist": (latest_scan.get("suggested_watchlist", []) or [])[:10],
            "llm_enabled": bool(latest_scan.get("llm_enabled")),
            "heat_summary": scan_heat_summary,
        },
        "plan": {
            "actions": latest_plan.get("actions", []) or [],
            "rejected_actions": latest_plan.get("rejected_actions", []) or [],
            "constraints": latest_plan.get("constraints", {}) or {},
        },
        "execution_monitor": execution_monitor,
    }
    llm_decision = build_evening_decision(decision_payload)
    key_risks = [str(x).strip() for x in (llm_decision.get("key_risks", []) or []) if str(x).strip()]
    if execution_monitor.get("status") != "ok" and execution_note not in key_risks:
        key_risks.append(execution_note)
    llm_decision["key_risks"] = key_risks
    record = {
        "stage": "evening",
        "trade_date": trade_date,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "llm_decision": llm_decision,
        "plan_id": latest_plan.get("plan_id", ""),
        "execution_monitor": execution_monitor,
        "decision_mode": decision_mode,
    }
    target = repo.save_decision("evening", record)
    snapshot_payload = _build_investor_snapshot(repo, source="evening-decision")
    snapshot_path = repo.save_investor_snapshot(snapshot_payload)
    industry_rows = ((snapshot_payload.get("industry_exposure", {}) or {}).get("rows", []) or [])
    top_industry = (industry_rows[0] if industry_rows else {})
    heat_cov = int(scan_heat_summary.get("coverage_count", 0) or 0)
    heat_leader = ((scan_heat_summary.get("top_heat_candidates", []) or [{}])[0] or {})
    decision_risks_compact = _compact_join(
        [str(x).strip() for x in (llm_decision.get("key_risks", []) or []) if str(x).strip()],
        max_items=6,
        empty_text="无",
    )
    decision_adjustments = _format_decision_adjustments(llm_decision, max_items=6)
    plan_action_table = _format_plan_action_details(latest_plan)
    heat_table = _format_heat_candidate_details(scan_heat_summary)
    portfolio_return_summary = _format_portfolio_return_summary(portfolio)
    decision_text = _decision_label(llm_decision.get("decision"))
    confidence_pct = _confidence_pct(llm_decision.get("confidence"))
    mode_text = "草稿" if decision_mode == "draft" else "定稿"
    draft_note = ""
    if decision_mode == "draft":
        draft_note = "⚠️ 未确认今日交割，此为草稿 — 明早确认交割单后定稿\n"
    timing_note = ""
    if bool(getattr(settings_for_review, "high_tech_mode", False)):
        timing_note = "⏰ 建议尾盘14:50-15:00执行调仓\n"
    text = (
        f"【{strategy['report_title']}】{trade_date}\n"
        f"{draft_note}"
        f"{timing_note}"
        f"决策：{decision_text}｜置信度：{confidence_pct:.0f}%｜版本：{mode_text}\n"
        f"{portfolio_return_summary}\n"
        f"核心结论：{llm_decision.get('summary', '')}\n"
        f"关键风险：{decision_risks_compact}\n"
        f"复核建议：{decision_adjustments}\n"
        f"执行闭环：{execution_note}\n"
        f"调整计划：{len((latest_plan.get('actions', []) or []))} 笔，被风控拒绝 {len((latest_plan.get('rejected_actions', []) or []))} 笔\n"
        f"{plan_action_table}"
        "说明：仅供人工核验，不会自动向券商下单。"
    )
    card_title = f"{strategy['report_title']} {trade_date}"
    if decision_mode == "draft":
        card_title += " [草稿]"
    card = _build_feishu_card(
        card_title,
        [
            {
                "title": "决策结论",
                "content": (
                    f"**{decision_text}**｜置信度 {confidence_pct:.0f}%｜{mode_text}\n"
                    + ("⚠️ 未确认今日交割，此为草稿\n" if decision_mode == "draft" else "")
                    + f"{portfolio_return_summary}\n"
                    f"{llm_decision.get('summary', '')}"
                ),
            },
            {"title": "关键风险", "content": decision_risks_compact},
            {"title": "复核建议", "content": decision_adjustments if decision_adjustments != "无" else "无调整"},
            {"title": "执行闭环", "content": execution_note},
            {"title": "调仓计划", "content": plan_action_table},
            {"title": "热点候选（前5）", "content": heat_table},
            {"title": "执行边界", "content": "仅供人工核验，不会自动向券商下单。"},
        ],
        template="red" if str(llm_decision.get("decision", "")).strip().lower() == "pause" else "purple",
    )
    pushed = False if bool(args.no_push) else push_feishu_rich(text, card=card)
    print(text)
    print(f"feishu_pushed: {pushed}")
    print(f"investor_snapshot: {snapshot_path}")
    return 0


def _opening_gap_from_quote(quote: dict, trade_date: str) -> dict:
    q = quote or {}
    open_price = _float(q.get("open", 0) or 0)
    prev_close = _float(q.get("pre_close", 0) or 0)
    as_of = str(q.get("as_of") or "").strip()
    source = str(q.get("source") or "").strip()
    active = _float(q.get("volume", 0) or 0) > 0 or _float(q.get("amount", 0) or 0) > 0
    if not as_of.startswith(str(trade_date or "")):
        reason = "行情日期与交易日不一致"
    elif not active:
        reason = "尚无可验证成交量或成交额"
    elif open_price <= 0 or prev_close <= 0:
        reason = "开盘价或昨收价缺失"
    else:
        reason = ""
    available = not reason
    gap_pct = ((open_price - prev_close) / prev_close * 100.0) if available else 0.0
    return {
        "available": available,
        "reason": reason,
        "open_price": open_price if available else 0.0,
        "prev_close": prev_close if available else 0.0,
        "gap_pct": round(gap_pct, 2),
        "as_of": as_of,
        "source": source,
    }


def cmd_morning_decision(args: argparse.Namespace) -> int:
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    if not bool(args.ignore_trading_calendar):
        is_open, source = is_cn_trading_day(trade_date)
        if not is_open:
            print(f"skip morning-decision: {trade_date} non_trading_day source={source}")
            return 0
    repo = LongTermRepository()
    repo.init_if_missing()
    portfolio = repo.load_portfolio()
    universe = repo.load_universe()
    latest_evening = repo.load_latest_decision("evening") or {}
    latest_plan = repo.load_latest_plan() or {}
    execution_monitor = _build_execution_monitor(repo, latest_plan)
    execution_note = _execution_monitor_note(execution_monitor)
    settings = repo.load_settings()

    # --- 科技动量模式：开盘修正午后计划 ---
    if bool(getattr(settings, "high_tech_mode", False)):
        data_source = OpenClawChinaStockDataSource()
        codes = sorted({item.code.upper() for item in portfolio.positions})
        for action in (latest_plan.get("actions", []) or []):
            c = str(action.get("code", "")).upper()
            if c not in codes:
                codes.append(c)
        # 获取开盘行情
        quotes = data_source.fetch_quotes(codes)
        # 计算开盘相对昨收的跳空幅度
        gaps: Dict[str, Dict] = {}
        for code in codes:
            q = quotes.get(code, quotes.get(code.split(".")[0], {})) or {}
            gaps[code] = _opening_gap_from_quote(q, trade_date)

        valid_gaps = [item for item in gaps.values() if item.get("available")]

        # 加载晚间复盘中的止损止盈
        sl_tp_info = latest_evening.get("sl_tp_levels", {}) if latest_evening else {}
        # 构建开盘修正 payload
        llm_payload = {
            "trade_date": trade_date,
            "stage": "morning_adjust",
            "portfolio": {
                "nav": portfolio.nav,
                "cash": portfolio.cash,
                "holdings_count": len(portfolio.positions),
            },
            "afternoon_plan": {
                "actions": latest_plan.get("actions", []) or [],
            },
            "evening_review": {
                "summary": (latest_evening.get("llm_decision", {}) or {}).get("summary", ""),
                "key_risks": (latest_evening.get("llm_decision", {}) or {}).get("key_risks", []),
                "sl_tp_levels": sl_tp_info,
            },
            "opening_gaps": gaps,
            "execution_monitor": {
                "status": execution_monitor.get("status", "?"),
                "note": execution_note,
            },
        }
        if valid_gaps:
            llm_decision = build_morning_decision(llm_payload)
        else:
            llm_decision = {
                "decision": "pause",
                "confidence": 0,
                "summary": "开盘行情未通过新鲜度与成交活跃度检查，本次不修正调仓计划。",
                "adjustments": [],
                "execution_notes": ["数据不可用时不以 0 值代替开盘行情。"],
            }
        execution_notes = [str(x).strip() for x in (llm_decision.get("execution_notes", []) or []) if str(x).strip()]
        if execution_monitor.get("status") != "ok" and execution_note not in execution_notes:
            execution_notes.append(execution_note)
        llm_decision["execution_notes"] = execution_notes

        record = {
            "stage": "morning_adjust",
            "trade_date": trade_date,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "llm_decision": llm_decision,
            "opening_gaps": gaps,
            "execution_monitor": execution_monitor,
        }
        repo.save_decision("morning", record)
        snapshot_path = repo.save_investor_snapshot(
            _build_investor_snapshot(repo, source="morning-adjust-ht")
        )

        plan_action_table = _format_plan_action_details(latest_plan)
        sources = sorted({str(item.get("source") or "") for item in valid_gaps if item.get("source")})
        as_of_values = sorted({str(item.get("as_of") or "") for item in valid_gaps if item.get("as_of")})
        gap_lines = [
            f"数据来源：{'、'.join(sources) or '不可用'}"
            + (f"｜采集时间：{as_of_values[-1]}" if as_of_values else "")
        ]
        for code, info in sorted(gaps.items()):
            if not info.get("available"):
                gap_lines.append(f"⚠️ {code}：开盘行情不可用（{info.get('reason') or '未知原因'}）")
                continue
            arrow = "🔺" if info["gap_pct"] > 1.5 else ("🔻" if info["gap_pct"] < -1.5 else "➡️")
            gap_lines.append(f"{arrow} {code}: 开盘{info['open_price']:.2f} 昨收{info['prev_close']:.2f} 跳空{info['gap_pct']:+.2f}%")
        gap_table = "\n".join(gap_lines)

        decision_adjustments = _format_decision_adjustments(llm_decision, max_items=6)
        execution_notes_str = _compact_join(
            [str(x).strip() for x in (llm_decision.get("execution_notes", []) or []) if str(x).strip()],
            max_items=6, empty_text="无",
        )
        nav = float(portfolio.nav)
        text = (
            f"【科技动量开盘修正】{trade_date}\n"
            f"净值：{nav:,.0f} 元｜持仓：{len(portfolio.positions)} 只｜现金：{portfolio.cash:,.0f} 元\n"
            f"开盘行情：\n{gap_table}\n"
            f"策略修正：{decision_adjustments}\n"
            f"执行备注：{execution_notes_str}\n"
            f"人工复核计划：\n{plan_action_table}"
            "风险边界：这是模拟组合的人工决策建议，不代表券商委托或成交；不会自动下单。"
        )
        card = _build_feishu_card(
            f"科技动量开盘修正 {trade_date}",
            [
                {"title": "开盘行情", "content": gap_table},
                {"title": "策略修正建议", "content": decision_adjustments if decision_adjustments != "无" else "无修正，按原计划执行"},
                {"title": "执行备注", "content": execution_notes_str},
                {"title": "人工复核计划", "content": plan_action_table},
                {
                    "title": "风险边界",
                    "content": "这是模拟组合的人工决策建议，不代表券商委托或成交；不会自动下单。",
                },
            ],
            template="orange",
        )
        print(text)
        pushed = False if bool(args.no_push) else push_feishu_rich(text, card=card)
        print(f"[HT morning] Feishu pushed: {pushed}")
        print(f"  snapshot: {snapshot_path}")
        return 0
    # --- 科技动量开盘修正结束 ---

    # Check settlement / draft status
    require_confirmed = bool(getattr(settings, "require_trades_confirmed", True))
    evening_was_draft = str(latest_evening.get("decision_mode", "") or "").strip().lower() == "draft"
    trades_confirmed = execution_monitor.get("status", "") == "ok"
    settlement_note = ""
    if require_confirmed:
        if trades_confirmed:
            settlement_note = "已确认交割 — 调仓建议可执行"
        elif evening_was_draft:
            settlement_note = "⚠️ 交割待确认 — 请先更新今日交割单再定稿"
        else:
            settlement_note = "⚠️ 交割状态待核查"

    data_source = OpenClawChinaStockDataSource()
    codes = sorted({item.code.upper() for item in universe} | {item.code.upper() for item in portfolio.positions})
    quotes = data_source.fetch_quotes(codes)
    market_snapshot = _build_market_snapshot(codes, quotes)
    index_snapshot = _build_index_snapshot(data_source)
    industry_breadth = _build_industry_breadth(universe, quotes)
    external_inputs = _load_external_market_inputs(trade_date)
    market_structure = _build_market_structure_signals(
        market_snapshot,
        index_snapshot,
        industry_breadth,
        external_inputs=external_inputs,
    )

    llm_payload = {
        "trade_date": trade_date,
        "latest_evening_decision": latest_evening,
        "settlement": {
            "confirmed": trades_confirmed,
            "require_confirmed": require_confirmed,
            "evening_was_draft": evening_was_draft,
            "note": settlement_note,
        },
        "latest_plan": {
            "actions": latest_plan.get("actions", []) or [],
            "rejected_actions": latest_plan.get("rejected_actions", []) or [],
            "constraints": latest_plan.get("constraints", {}) or {},
        },
        "market_snapshot": market_snapshot,
        "index_snapshot": index_snapshot,
        "industry_breadth": industry_breadth,
        "market_structure": market_structure,
        "execution_monitor": execution_monitor,
    }
    llm_decision = build_morning_decision(llm_payload)
    execution_notes = [str(x).strip() for x in (llm_decision.get("execution_notes", []) or []) if str(x).strip()]
    if execution_monitor.get("status") != "ok" and execution_note not in execution_notes:
        execution_notes.append(execution_note)
    llm_decision["execution_notes"] = execution_notes
    record = {
        "stage": "morning",
        "trade_date": trade_date,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "llm_decision": llm_decision,
        "market_snapshot": market_snapshot,
        "index_snapshot": index_snapshot,
        "industry_breadth": industry_breadth,
        "market_structure": market_structure,
        "execution_monitor": execution_monitor,
        "linked_evening_trade_date": (latest_evening or {}).get("trade_date", ""),
        "settlement": {
            "confirmed": trades_confirmed,
            "evening_was_draft": evening_was_draft,
            "note": settlement_note,
        },
    }
    target = repo.save_decision("morning", record)
    snapshot_payload = _build_investor_snapshot(repo, source="morning-decision")
    snapshot_path = repo.save_investor_snapshot(snapshot_payload)
    industry_rows = ((snapshot_payload.get("industry_exposure", {}) or {}).get("rows", []) or [])
    top_industry = (industry_rows[0] if industry_rows else {})
    decision_adjustments = _format_decision_adjustments(llm_decision, max_items=6)
    execution_notes = _compact_join(
        [str(x).strip() for x in (llm_decision.get("execution_notes", []) or []) if str(x).strip()],
        max_items=6,
        empty_text="无",
    )
    plan_action_table = _format_plan_action_details(latest_plan)
    regime = _market_regime_label(market_structure.get("regime_hint"))
    adv_dec = market_structure.get("adv_dec_ratio", 0)
    nav = float(portfolio.nav)
    total_mkt = sum(p.last_price * p.quantity for p in portfolio.positions)
    decision_text = _decision_label(llm_decision.get("decision"))
    confidence_pct = _confidence_pct(llm_decision.get("confidence"))
    text = (
        f"【长线组合 09:35 复核】{trade_date}\n"
        f"交割：{settlement_note}\n"
        f"决策：{decision_text}｜置信度：{confidence_pct:.0f}%\n"
        f"市场状态：{regime}｜上涨/下跌比 {adv_dec}\n"
        f"核心结论：{llm_decision.get('summary', '')}\n"
        f"执行备注：{execution_notes}\n"
        f"执行闭环：{execution_note}\n"
        f"修正建议：{decision_adjustments}\n"
        f"{plan_action_table}"
        "说明：仅供人工核验，不会自动向券商下单。"
    )
    card = _build_feishu_card(
        f"长线组合 09:35 复核 {trade_date}",
        [
            {
                "title": "交割确认",
                "content": settlement_note if settlement_note else "无需确认",
            },
            {
                "title": "复核结论",
                "content": (
                    f"**{decision_text}**｜置信度 {confidence_pct:.0f}%\n"
                    f"市场状态：{regime}｜上涨/下跌比 {adv_dec}\n"
                    f"{llm_decision.get('summary', '')}"
                ),
            },
            {"title": "执行备注", "content": execution_notes if execution_notes != "无" else "无特别备注"},
            {"title": "执行闭环", "content": execution_note},
            {"title": "修正建议", "content": decision_adjustments if decision_adjustments != "无" else "无修正"},
            {"title": "待执行计划", "content": plan_action_table},
            {"title": "执行边界", "content": "仅供人工核验，不会自动向券商下单。"},
        ],
        template="red" if str(llm_decision.get("decision", "")).strip().lower() == "pause" else "orange",
    )
    pushed = False if bool(args.no_push) else push_feishu_rich(text, card=card)
    print(text)
    print(f"feishu_pushed: {pushed}")
    print(f"investor_snapshot: {snapshot_path}")
    return 0


# ---------------------------------------------------------------------------
# Rotation commands
# ---------------------------------------------------------------------------


def _run_rotation_scan_core(
    *,
    repo: LongTermRepository,
    trade_date: str,
    top_k: int,
    use_llm: bool,
) -> dict:
    """Core logic shared by rotation-scan and rotation-exit-check."""
    universe = repo.load_universe()
    portfolio = repo.load_portfolio()
    if not universe:
        return {"error": "universe_empty"}
    data_source = OpenClawChinaStockDataSource()
    settings = repo.load_settings()
    # Force rotation mode for these commands
    settings.rotation_mode = True
    codes = sorted({item.code.upper() for item in universe} | {item.code.upper() for item in portfolio.positions})
    quotes = data_source.fetch_quotes(codes)
    history_by_code = data_source.fetch_batch_daily_history(codes, lookback_days=60, end_date=trade_date)
    heat_by_code = data_source.fetch_ths_hotness(
        codes,
        trade_date=trade_date,
        limit_up_weight=float(getattr(settings, "ths_heat_limit_up_weight", 2.0)),
        change_weight=float(getattr(settings, "ths_heat_change_weight", 18.0)),
    )
    scan_payload = run_post_market_scan(
        trade_date=trade_date,
        universe=universe,
        portfolio=portfolio,
        quotes=quotes,
        history_by_code=history_by_code,
        heat_by_code=heat_by_code,
        settings=settings,
        top_k=int(top_k),
        use_llm=bool(use_llm),
    )
    plan, mtm_portfolio = build_rebalance_plan(
        trade_date=trade_date,
        candidates=universe,
        portfolio=portfolio,
        quotes=quotes,
        settings=settings,
        rotation_scan=scan_payload,
        history_by_code=history_by_code,
    )
    scan_path = repo.save_post_market_scan(scan_payload)
    plan_path = repo.save_plan(plan)
    return {
        "scan_payload": scan_payload,
        "plan": plan,
        "portfolio": mtm_portfolio,
        "scan_path": scan_path,
        "plan_path": plan_path,
    }


def cmd_rotation_scan(args: argparse.Namespace) -> int:
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    repo = LongTermRepository()
    repo.init_if_missing()
    result = _run_rotation_scan_core(
        repo=repo,
        trade_date=trade_date,
        top_k=int(args.top_k),
        use_llm=not bool(args.no_llm),
    )
    if result.get("error") == "universe_empty":
        print("universe is empty, run sync-universe-from-portfolio first")
        return 1

    scan = result["scan_payload"]
    plan = result["plan"]
    pf = result["portfolio"]

    print(f"=== Heat Rotation Scan — {trade_date} ===")
    print(f"scan_file: {result['scan_path']}")
    print(f"plan_file: {result['plan_path']}")
    print(f"nav: {pf.nav:.2f} cash: {pf.cash:.2f} holdings: {len(pf.positions)}")
    print(f"universe: {scan.get('universe_count', 0)} top_k: {scan.get('top_k', 0)}")

    # Print top candidates by rotation score
    ranked = scan.get("ranked", []) or []
    print(f"\n--- Top Rotation Candidates ---")
    for item in ranked[:20]:
        hrs = _compute_hrs_from_scan_row(item, repo.load_settings())
        print(
            f"  {item.get('code')} {item.get('name','')} "
            f"score={float(item.get('score',0)):.1f} "
            f"HRS={hrs:.1f} "
            f"heat={float(item.get('ths_heat_score',0) or 0):.1f} "
            f"accel={float(item.get('heat_accel',0)):.3f} "
            f"sector={float(item.get('sector_momentum',0)):.1f} "
            f"trend={float(item.get('price_trend',0)):.1f} "
            f"liquidity={float(item.get('liquidity_score',0)):.1f}"
        )

    # Print theme allocation
    print(f"\n--- Theme Allocation ---")
    scan_rows = ranked
    themes = _summarize_theme_allocation(scan_rows)
    for theme, info in themes.items():
        print(f"  {theme}: weight={info['weight']:.3f} candidates={info['count']} strength={info['strength']:.1f}")

    # Print actions
    print(f"\n--- Rebalance Plan (source={plan.source}) ---")
    print(f"actions: {len(plan.actions)}")
    for item in plan.actions[:20]:
        print(
            f"  - {item.code} {item.name} {item.action} "
            f"shares={item.delta_shares} ref={item.reference_price:.3f} "
            f"current={item.current_weight:.2%} target={item.target_weight:.2%} "
            f"amount={item.estimated_amount:.2f} reason={item.reason[:80]}"
        )
    print(f"rejected: {len(plan.rejected_actions)}")
    for item in plan.rejected_actions[:10]:
        print(f"  - {item.code} {item.action} amount={item.estimated_amount:.2f} reason={item.reason}")
    return 0


def cmd_rotation_exit_check(args: argparse.Namespace) -> int:
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    repo = LongTermRepository()
    repo.init_if_missing()
    result = _run_rotation_scan_core(
        repo=repo,
        trade_date=trade_date,
        top_k=int(args.top_k),
        use_llm=False,  # No LLM needed for exit check
    )
    if result.get("error") == "universe_empty":
        print("universe is empty, run sync-universe-from-portfolio first")
        return 1

    plan = result["plan"]
    scan = result["scan_payload"]

    print(f"=== Rotation Exit Check — {trade_date} ===")
    print(f"holdings: {len(result['portfolio'].positions)}")
    print(f"total_actions: {len(plan.actions)}")

    # Separate exit signals from regular rebalance
    exit_signals = [a for a in plan.actions if a.action == "sell" and "|" in (a.reason or "")]
    regular_sells = [a for a in plan.actions if a.action == "sell" and "|" not in (a.reason or "")]
    buys = [a for a in plan.actions if a.action == "buy"]

    print(f"\n--- Exit Signals ({len(exit_signals)}) ---")
    if exit_signals:
        for item in exit_signals:
            print(f"  [EXIT] {item.code} {item.name} reason={item.reason}")
    else:
        print("  (no exit signals triggered)")

    print(f"\n--- Regular Rebalance ({len(regular_sells)} sells, {len(buys)} buys) ---")
    for item in regular_sells[:10]:
        print(f"  [SELL] {item.code} {item.name} amount={item.estimated_amount:.2f} reason={item.reason}")
    for item in buys[:10]:
        print(f"  [BUY] {item.code} {item.name} amount={item.estimated_amount:.2f} target={item.target_weight:.2%}")

    return 0


def _compute_hrs_from_scan_row(scan_row: dict, settings: LongTermSettings) -> float:
    """Compute HRS from settings, used for display in CLI."""
    ha = max(0.0, float(getattr(settings, "hrs_heat_accel_w", 0.35) or 0.35))
    sm = max(0.0, float(getattr(settings, "hrs_sector_mom_w", 0.25) or 0.25))
    pt = max(0.0, float(getattr(settings, "hrs_price_trend_w", 0.20) or 0.20))
    lq = max(0.0, float(getattr(settings, "hrs_liquidity_w", 0.20) or 0.20))
    total_w = ha + sm + pt + lq
    if total_w <= 0:
        ha, sm, pt, lq = 0.35, 0.25, 0.20, 0.20
    else:
        ha, sm, pt, lq = ha / total_w, sm / total_w, pt / total_w, lq / total_w

    heat_accel = float(scan_row.get("heat_accel", 0.0) or 0.0)
    sector_mom = float(scan_row.get("sector_momentum", 0.0) or 0.0)
    price_trend = float(scan_row.get("price_trend", 0.0) or 0.0)
    liquidity = float(scan_row.get("liquidity_score", 0.0) or 0.0)

    heat_accel_scaled = max(0.0, min(100.0, 50.0 + heat_accel * 25.0))
    return round(
        max(0.0, min(100.0,
            heat_accel_scaled * ha + sector_mom * sm + price_trend * pt + liquidity * lq
        )), 3
    )


def _summarize_theme_allocation(scan_rows: list) -> dict:
    """Summarize theme allocation from scan rows for display."""
    theme_info: dict = {}
    for row in scan_rows:
        for theme in [str(c).strip() for c in (row.get("ths_hot_concepts", []) or []) if str(c).strip()]:
            if theme not in theme_info:
                theme_info[theme] = {"count": 0, "strength": 0.0, "weight": 0.0}
            theme_info[theme]["count"] += 1
            theme_info[theme]["strength"] += float(row.get("score", 0.0) or 0.0)
    total_strength = sum(v["strength"] for v in theme_info.values()) or 1.0
    for theme in theme_info:
        theme_info[theme]["strength"] = round(theme_info[theme]["strength"], 1)
        theme_info[theme]["weight"] = round(theme_info[theme]["strength"] / total_strength, 4)
    return dict(sorted(theme_info.items(), key=lambda x: x[1]["strength"], reverse=True))


# ---------------------------------------------------------------------------
# Intraday monitor command
# ---------------------------------------------------------------------------


def cmd_intraday_monitor(args: argparse.Namespace) -> int:
    """Run intraday real-time monitoring: once or as daemon.

    Fetches index / sector / holdings quotes every N minutes, sends to LLM
    for action recommendations (add/reduce/clear), and pushes Feishu alerts.
    """
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    interval = max(30, int(args.interval or 300))
    no_push = bool(args.no_push)

    if bool(args.once):
        # Single check
        if not bool(args.ignore_trading_calendar):
            is_open, source = is_cn_trading_day(trade_date)
            if not is_open:
                print(f"skip intraday-monitor: {trade_date} non_trading_day source={source}")
                return 0
        config = MonitorConfig(interval_seconds=interval)
        monitor = IntradayMonitor(config)
        print(f"intraday check: {trade_date} interval={interval}s")
        decision = monitor.run_check(trade_date, force_push=not no_push)
        print(f"alert_level: {decision.get('alert_level', '?')}")
        print(f"market_risk: {decision.get('market_risk', '?')}")
        print(f"summary: {decision.get('summary', '')}")
        holdings_actions = decision.get("holdings_actions", []) or []
        for ha in holdings_actions:
            print(
                f"  {ha.get('code','?')}: {ha.get('action','?')} "
                f"urgency={ha.get('urgency','?')} reason={ha.get('reason','?')}"
            )
        opportunities = decision.get("new_opportunities", []) or []
        for opp in opportunities:
            print(f"  opportunity: {opp.get('code','?')} {opp.get('reason','?')}")
        signals = decision.get("key_signals", []) or []
        for sig in signals:
            print(f"  signal: {sig}")
        return 0

    # Daemon mode
    print(f"starting intraday daemon: interval={interval}s")
    if bool(args.no_push):
        print("push disabled")
    monitor = IntradayMonitor(MonitorConfig(interval_seconds=interval))
    monitor.run_daemon()
    return 0


def cmd_ht_afternoon_check(args: argparse.Namespace) -> int:
    """14:50 盘中再平衡 — 科技动量策略专用。

    使用实时行情计算动量评分 → 生成目标权重 → 对比当前持仓 → 飞书推送买卖清单。
    用户收到后可在尾盘(14:50-15:00)通过飞书命令 /长线成交 手动执行。
    """
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    if not bool(args.ignore_trading_calendar):
        is_open, source = is_cn_trading_day(trade_date)
        if not is_open:
            print(f"skip ht-afternoon-check: {trade_date} non_trading_day source={source}")
            return 0

    repo = LongTermRepository()
    repo.init_if_missing()
    settings = repo.load_settings()

    if not bool(getattr(settings, "high_tech_mode", False)):
        print("[HT] high_tech_mode is disabled in settings, skipping")
        return 0

    # 1. Sync universe (inject tech pool)
    print(f"[HT] syncing tech universe for {trade_date}...")
    sync_result = _sync_universe_core(repo=repo, trade_date=trade_date, refresh_industry=False, refresh_ths_sector=False)
    print(f"  universe: {int(sync_result.get('candidates_count', 0) or 0)} stocks")
    universe = repo.load_universe()
    portfolio = repo.load_portfolio()

    if not universe:
        print("[HT] empty universe, aborting")
        return 1

    # 2. Fetch real-time quotes + history (14:50 盘中数据)
    data_source = OpenClawChinaStockDataSource()
    codes = sorted({item.code.upper() for item in universe} | {item.code.upper() for item in portfolio.positions})
    print(f"[HT] fetching real-time data for {len(codes)} codes...")
    quotes = data_source.fetch_quotes(codes)
    lookback = int(getattr(settings, "ht_lookback_days", 20) or 20) + 10
    # 14:50 实时检查：不传 end_date，用最新数据
    # 实时行情 + 历史日线拼接成完整价格序列用于动量计算
    history_by_code = data_source.fetch_batch_daily_history(codes, lookback_days=lookback)
    print(f"  quotes: {len(quotes)} codes")
    print(f"  history: {len(history_by_code)} codes")

    # 3. Build rebalance plan using high-tech momentum strategy
    rotation_scan = getattr(args, "rotation_scan", None)
    plan, mtm_portfolio = build_rebalance_plan(
        trade_date=trade_date,
        candidates=universe,
        portfolio=portfolio,
        quotes=quotes,
        settings=settings,
        rotation_scan=rotation_scan,
        history_by_code=history_by_code,
    )
    repo.save_plan(plan)
    repo.save_portfolio(mtm_portfolio)
    snapshot_path = repo.save_investor_snapshot(_build_investor_snapshot(repo, source="ht-afternoon-check"))

    # 4. Print summary
    print(f"\n[HT] === 科技动量盘中再平衡 — {trade_date} ===")
    print(f"  NAV: {mtm_portfolio.nav:,.0f} | 持仓: {len(mtm_portfolio.positions)} | 现金: {mtm_portfolio.cash:,.0f}")
    print(f"  操作: {len(plan.actions)} 笔 | 被拒: {len(plan.rejected_actions)} 笔")
    for item in plan.actions:
        tag = "BUY" if item.action == "buy" else "SELL"
        print(f"  [{tag}] {item.code} {item.name}: {abs(item.delta_shares)}股 @{item.reference_price:.2f} "
              f"金额={item.estimated_amount:,.0f} →{item.target_weight:.1%}")

    # 5. Push Feishu card
    no_push = bool(args.no_push)
    if no_push:
        print("[HT] Feishu push disabled (--no-push)")
        return 0

    # Format plan for display
    plan_table = _format_plan_action_details(
        {
            "actions": [asdict(a) for a in plan.actions],
            "rejected_actions": [asdict(r) for r in plan.rejected_actions],
            "constraints": {},
        }
    )
    buy_count = sum(1 for a in plan.actions if a.action == "buy")
    sell_count = sum(1 for a in plan.actions if a.action == "sell")
    total_buy = sum(a.estimated_amount for a in plan.actions if a.action == "buy")
    total_sell = sum(a.estimated_amount for a in plan.actions if a.action == "sell")

    nav = mtm_portfolio.nav
    cash = mtm_portfolio.cash
    holdings_cnt = len(mtm_portfolio.positions)

    text = (
        f"【科技动量尾盘再平衡】{trade_date}\n"
        f"⏰ 仅在尾盘14:50-15:00窗口内人工核验调仓，错过窗口不追单\n"
        f"净值 {nav:,.0f} 元｜现金 {cash:,.0f} 元｜持仓 {holdings_cnt} 只\n"
        f"买入候选 {buy_count} 笔，预计 {total_buy:,.0f} 元｜卖出候选 {sell_count} 笔，预计 {total_sell:,.0f} 元\n"
        f"{plan_table}"
    )
    card = _build_feishu_card(
        f"科技动量尾盘调仓建议 {trade_date}",
        [
            {
                "title": "人工调仓建议",
                "content": (
                    f"⏰ 仅在尾盘 **14:50-15:00** 窗口内人工核验执行，错过不追单\n"
                    f"净值 {nav:,.0f} 元｜现金 {cash:,.0f} 元｜持仓 {holdings_cnt} 只\n"
                    f"买入候选 {buy_count} 笔，预计 {total_buy:,.0f} 元｜卖出候选 {sell_count} 笔，预计 {total_sell:,.0f} 元"
                ),
            },
            {"title": "调仓明细", "content": plan_table},
            {"title": "操作方式", "content": "通过飞书命令 `/长线成交` 手动执行调仓"},
        ],
        template="orange",
    )
    pushed = push_feishu_rich(text, card=card)
    print(f"[HT] Feishu pushed: {pushed}")
    print(f"  snapshot: {snapshot_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Long-term portfolio simulation CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="initialize repository")
    p_init.add_argument("--initial-capital", type=float, default=1_000_000)
    p_init.add_argument("--seed-universe", type=str, default="")
    p_init.add_argument("--settings", type=str, default="")
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run-review", help="generate rebalance suggestions")
    p_run.add_argument("--date", type=str, default="")
    p_run.add_argument("--single-name-cap", type=float, default=None)
    p_run.add_argument("--cash-buffer-ratio", type=float, default=None)
    p_run.add_argument("--rebalance-threshold", type=float, default=None)
    p_run.add_argument("--max-industry-weight", type=float, default=None)
    p_run.add_argument("--industry-cap-mode", type=str, default=None)
    p_run.add_argument("--core-industry-cap", type=float, default=None)
    p_run.add_argument("--satellite-industry-cap", type=float, default=None)
    p_run.add_argument("--core-industries", type=str, default=None)
    p_run.add_argument("--max-theme-weight", type=float, default=None)
    p_run.add_argument("--theme-cap-mode", type=str, default=None)
    p_run.add_argument("--core-theme-cap", type=float, default=None)
    p_run.add_argument("--satellite-theme-cap", type=float, default=None)
    p_run.add_argument("--core-themes", type=str, default=None)
    p_run.add_argument("--min-trade-amount", type=float, default=None)
    p_run.add_argument("--max-holdings", type=int, default=None)
    p_run.add_argument("--max-portfolio-volatility", type=float, default=None)
    p_run.add_argument("--max-portfolio-drawdown", type=float, default=None)
    p_run.set_defaults(func=cmd_run_review)

    p_apply = sub.add_parser("apply-manual", help="apply manual execution fills")
    p_apply_group = p_apply.add_mutually_exclusive_group(required=True)
    p_apply_group.add_argument("--file", type=str)
    p_apply_group.add_argument("--text", type=str)
    p_apply.add_argument("--date", type=str, default="")
    p_apply.set_defaults(func=cmd_apply_manual)

    p_auto = sub.add_parser("auto-rebalance", help="generate and apply strategy rebalance internally")
    p_auto.add_argument("--date", type=str, default="")
    p_auto.add_argument("--dry-run", action="store_true", help="save plan and mark-to-market only, do not apply fills")
    p_auto.add_argument("--no-push", action="store_true", help="do not push Feishu notification after applying fills")
    p_auto.add_argument("--ignore-trading-calendar", action="store_true")
    p_auto.set_defaults(func=cmd_auto_rebalance)

    p_rebuild = sub.add_parser("rebuild-portfolio", help="rebuild portfolio from execution ledger")
    p_rebuild.add_argument("--date", type=str, default="")
    p_rebuild.set_defaults(func=cmd_rebuild_portfolio)

    p_apply_cmd = sub.add_parser("apply-manual-command", help="apply manual fills from feishu command text")
    p_apply_cmd.add_argument("--text", type=str, required=True)
    p_apply_cmd.set_defaults(func=cmd_apply_manual_command)

    p_image = sub.add_parser("apply-manual-from-image", help="apply manual fills from settlement screenshot")
    p_image.add_argument("--image", type=str, required=True, help="path to settlement screenshot (png/jpg)")
    p_image.add_argument("--date", type=str, default="", help="trade date (default: today)")
    p_image.add_argument("--dry-run", action="store_true", help="extract only, do not apply")
    p_image.set_defaults(func=cmd_apply_manual_from_image)

    p_pos_sync = sub.add_parser("sync-positions-from-image", help="sync portfolio positions/cash from brokerage position screenshot")
    p_pos_sync.add_argument("--image", type=str, required=True, help="path to position screenshot (png/jpg)")
    p_pos_sync.add_argument("--date", type=str, default="", help="sync date (default: today)")
    p_pos_sync.add_argument("--dry-run", action="store_true", help="extract only, do not sync")
    p_pos_sync.set_defaults(func=cmd_sync_positions_from_image)

    p_9db_cfg = sub.add_parser("configure-9db", help="configure 9db arena credentials from markdown URL")
    p_9db_cfg.add_argument("--url", type=str, required=True, help="9db markdown URL with token/order_id/order_name")
    p_9db_cfg.set_defaults(func=cmd_configure_9db)

    p_9db_show = sub.add_parser("show-9db", help="show current 9db arena configuration")
    p_9db_show.set_defaults(func=cmd_show_9db)

    p_summary = sub.add_parser("summary", help="show portfolio summary")
    p_summary.set_defaults(func=cmd_summary)

    p_show = sub.add_parser("show-settings", help="show longterm settings")
    p_show.set_defaults(func=cmd_show_settings)

    p_update = sub.add_parser("update-settings", help="update longterm settings")
    p_update.add_argument("--file", type=str, default="")
    p_update.add_argument("--single-name-cap", type=float, default=None)
    p_update.add_argument("--cash-buffer-ratio", type=float, default=None)
    p_update.add_argument("--rebalance-threshold", type=float, default=None)
    p_update.add_argument("--max-industry-weight", type=float, default=None)
    p_update.add_argument("--industry-cap-mode", type=str, default=None)
    p_update.add_argument("--core-industry-cap", type=float, default=None)
    p_update.add_argument("--satellite-industry-cap", type=float, default=None)
    p_update.add_argument("--core-industries", type=str, default=None)
    p_update.add_argument("--max-theme-weight", type=float, default=None)
    p_update.add_argument("--theme-cap-mode", type=str, default=None)
    p_update.add_argument("--core-theme-cap", type=float, default=None)
    p_update.add_argument("--satellite-theme-cap", type=float, default=None)
    p_update.add_argument("--core-themes", type=str, default=None)
    p_update.add_argument("--min-trade-amount", type=float, default=None)
    p_update.add_argument("--max-holdings", type=int, default=None)
    p_update.add_argument("--score-value-weight", type=float, default=None)
    p_update.add_argument("--score-quality-weight", type=float, default=None)
    p_update.add_argument("--score-growth-weight", type=float, default=None)
    p_update.add_argument("--score-risk-weight", type=float, default=None)
    p_update.add_argument("--max-portfolio-volatility", type=float, default=None)
    p_update.add_argument("--max-portfolio-drawdown", type=float, default=None)
    p_update.add_argument("--scan-atr-min", type=float, default=None)
    p_update.add_argument("--scan-rsi-min", type=float, default=None)
    p_update.add_argument("--scan-rsi-max", type=float, default=None)
    p_update.add_argument("--scan-turnover-min", type=float, default=None)
    p_update.add_argument("--scan-turnover-max", type=float, default=None)
    p_update.add_argument("--scan-fallback-amount-divisor", type=float, default=None)
    p_update.add_argument("--scan-fallback-amount-bonus-cap", type=float, default=None)
    p_update.add_argument("--scan-fallback-volume-divisor", type=float, default=None)
    p_update.add_argument("--scan-fallback-volume-bonus-cap", type=float, default=None)
    p_update.add_argument("--ths-heat-limit-up-weight", type=float, default=None)
    p_update.add_argument("--ths-heat-change-weight", type=float, default=None)
    p_update.add_argument("--retention-plan-days", type=int, default=None)
    p_update.add_argument("--retention-scan-days", type=int, default=None)
    p_update.add_argument("--retention-decision-days", type=int, default=None)
    p_update.add_argument("--retention-keep-min-files", type=int, default=None)
    p_update.add_argument("--retention-delete-tmp-older-than-hours", type=int, default=None)
    p_update.set_defaults(func=cmd_update_settings)

    p_sync_uni = sub.add_parser("sync-universe-from-portfolio", help="sync universe using current holdings")
    p_sync_uni.add_argument("--date", type=str, default="")
    p_sync_uni.add_argument("--refresh-industry", action="store_true")
    p_sync_uni.add_argument("--refresh-ths-sector", action="store_true")
    p_sync_uni.set_defaults(func=cmd_sync_universe_from_portfolio)

    p_scan = sub.add_parser("post-market-scan", help="run post-market alpha scan")
    p_scan.add_argument("--date", type=str, default="")
    p_scan.add_argument("--top-k", type=int, default=15)
    p_scan.add_argument("--no-llm", action="store_true")
    p_scan.set_defaults(func=cmd_post_market_scan)

    p_trade_day = sub.add_parser("check-trading-day", help="check whether date is CN trading day")
    p_trade_day.add_argument("--date", type=str, default="")
    p_trade_day.set_defaults(func=cmd_check_trading_day)

    p_evening = sub.add_parser("evening-decision", help="post-market decision with LLM and Feishu push")
    p_evening.add_argument("--date", type=str, default="")
    p_evening.add_argument("--top-k", type=int, default=15)
    p_evening.add_argument("--no-llm-scan", action="store_true")
    p_evening.add_argument("--skip-sync-universe", action="store_true")
    p_evening.add_argument("--ignore-trading-calendar", action="store_true")
    p_evening.add_argument("--no-push", action="store_true")
    p_evening.add_argument("--final", action="store_true", help="generate final decision instead of draft")
    p_evening.set_defaults(func=cmd_evening_decision)

    p_morning = sub.add_parser("morning-decision", help="09:35 decision review with LLM and Feishu push")
    p_morning.add_argument("--date", type=str, default="")
    p_morning.add_argument("--ignore-trading-calendar", action="store_true")
    p_morning.add_argument("--no-push", action="store_true")
    p_morning.set_defaults(func=cmd_morning_decision)

    p_intraday = sub.add_parser("intraday-monitor", help="real-time intraday market monitoring with LLM alerts")
    p_intraday.add_argument("--date", type=str, default="")
    p_intraday.add_argument("--interval", type=int, default=300, help="polling interval in seconds (default 300)")
    p_intraday.add_argument("--once", action="store_true", help="run a single check (default: daemon mode)")
    p_intraday.add_argument("--no-push", action="store_true", help="suppress Feishu push")
    p_intraday.add_argument("--ignore-trading-calendar", action="store_true")
    p_intraday.set_defaults(func=cmd_intraday_monitor)

    p_ht = sub.add_parser("ht-afternoon-check", help="14:50 intraday rebalance check for high-tech momentum strategy")
    p_ht.add_argument("--date", type=str, default="")
    p_ht.add_argument("--ignore-trading-calendar", action="store_true")
    p_ht.add_argument("--no-push", action="store_true")
    p_ht.set_defaults(func=cmd_ht_afternoon_check)

    p_cleanup = sub.add_parser("cleanup-data", help="cleanup longterm data files by retention policy")
    p_cleanup.add_argument("--keep-days-plan", type=int, default=None)
    p_cleanup.add_argument("--keep-days-scan", type=int, default=None)
    p_cleanup.add_argument("--keep-days-decision", type=int, default=None)
    p_cleanup.add_argument("--keep-min-files", type=int, default=None)
    p_cleanup.add_argument("--delete-tmp-older-than-hours", type=int, default=None)
    p_cleanup.add_argument("--dry-run", action="store_true")
    p_cleanup.set_defaults(func=cmd_cleanup_data)

    p_quality = sub.add_parser("decision-quality-report", help="evaluate plan vs manual executions for a trade date")
    p_quality.add_argument("--date", type=str, default="")
    p_quality.add_argument("--lookback-days", type=int, default=20)
    p_quality.add_argument("--match-rate-alert-threshold", type=float, default=0.6)
    p_quality.add_argument("--mismatch-rate-alert-threshold", type=float, default=0.3)
    p_quality.add_argument("--no-llm-optimize", action="store_true")
    p_quality.set_defaults(func=cmd_decision_quality_report)

    p_rot_scan = sub.add_parser("rotation-scan", help="run heat rotation scan and generate rotation plan")
    p_rot_scan.add_argument("--date", type=str, default="")
    p_rot_scan.add_argument("--top-k", type=int, default=30)
    p_rot_scan.add_argument("--no-llm", action="store_true")
    p_rot_scan.set_defaults(func=cmd_rotation_scan)

    p_rot_exit = sub.add_parser("rotation-exit-check", help="check holdings for rotation exit signals")
    p_rot_exit.add_argument("--date", type=str, default="")
    p_rot_exit.add_argument("--top-k", type=int, default=30)
    p_rot_exit.set_defaults(func=cmd_rotation_exit_check)

    return parser


def main() -> int:
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

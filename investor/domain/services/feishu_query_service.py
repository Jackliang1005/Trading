#!/usr/bin/env python3
"""Feishu query service for live qmt2http account data."""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from domain.services.assistant_menu_service import format_assistant_menu_text
from domain.services.assistant_status_service import build_assistant_status
from domain.services.watchlist_report_service import build_watchlist_report

from domain.services.live_monitor_view_service import format_today_summary_text
from domain.services.longterm_portfolio_service import (
    build_longterm_snapshot_text,
    load_longterm_snapshot,
    summarize_longterm_snapshot,
)
from workflows.scheduled_briefings import run_scheduled_briefing
from domain.services.event_service import build_global_event_brief, format_global_event_brief
from domain.services.weekly_report_service import build_weekly_report
from domain.services.risk_report_service import build_risk_report
from domain.services.morning_brief_service import build_morning_brief
from domain.services.closing_brief_service import build_closing_brief
from domain.services.global_impact_service import build_global_impact_brief
from domain.services.qmt_strategy_control_service import handle_strategy_control_query, is_strategy_control_query


DEFAULT_BASE_URLS = {
    "guojin": "http://39.105.48.176:8085",
    "dongguan": "http://150.158.31.115:8085",
}

TOKEN_FILES = (
    "/root/qmt2http/qmt2http_main.env",
    "/root/qmt2http/.env",
    "/root/.openclaw/workspace/investor/.env",
)

ACCOUNT_ALIASES = {
    "guojin": "国金",
    "dongguan": "东莞",
}


def _read_token_from_file(path: str) -> str:
    file_path = Path(path)
    if not file_path.exists():
        return ""
    try:
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return ""
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        if key.strip() != "QMT2HTTP_API_TOKEN":
            continue
        token = value.strip().strip('"').strip("'")
        if token:
            return token
    return ""


def _resolve_token() -> str:
    token = os.getenv("QMT2HTTP_API_TOKEN", "").strip()
    if token:
        return token
    for path in TOKEN_FILES:
        token = _read_token_from_file(path)
        if token:
            return token
    return ""


def _resolve_base_url(account: str) -> str:
    if account == "guojin":
        return (
            os.getenv("QMT2HTTP_MAIN_URL", "").strip()
            or os.getenv("QMT2HTTP_BASE_URL", "").strip()
            or DEFAULT_BASE_URLS["guojin"]
        ).rstrip("/")
    return (
        os.getenv("QMT2HTTP_DONGGUAN_BASE_URL", "").strip()
        or os.getenv("QMT2HTTP_TRADE_URL", "").strip()
        or DEFAULT_BASE_URLS["dongguan"]
    ).rstrip("/")


def _headers(token: str) -> Dict[str, str]:
    payload = {"Accept": "application/json"}
    if token:
        payload["Authorization"] = f"Bearer {token}"
        payload["X-API-Token"] = token
    return payload


def _http_get(base_url: str, path: str, token: str, timeout: float = 0) -> Dict:
    if not timeout:
        try:
            timeout = float(os.getenv("QMT2HTTP_FEISHU_TIMEOUT", os.getenv("QMT2HTTP_TIMEOUT", "8")) or 8)
        except Exception:
            timeout = 8.0
    url = f"{base_url.rstrip('/')}{path}"
    req = urllib.request.Request(url, headers=_headers(token), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
            return {"ok": True, "http_status": resp.status, "payload": payload, "url": url}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"raw": raw}
        return {
            "ok": False,
            "http_status": exc.code,
            "error": f"HTTP {exc.code}",
            "payload": payload,
            "url": url,
        }
    except Exception as exc:
        return {"ok": False, "http_status": None, "error": str(exc), "payload": {}, "url": url}


def _extract_item_code(item: Dict) -> str:
    return str(item.get("stock_code") or item.get("code") or item.get("证券代码") or "").strip()


def _extract_float(item: Dict, *keys: str) -> float:
    for key in keys:
        if key not in item:
            continue
        value = item.get(key)
        try:
            return float(value)
        except Exception:
            continue
    return 0.0


def _summarize_positions(items: List[Dict]) -> Tuple[str, List[str]]:
    if not items:
        return "持仓 0 条", []
    total_mv = 0.0
    total_pnl = 0.0
    lines: List[str] = []
    for item in items[:10]:
        code = _extract_item_code(item)
        volume = int(_extract_float(item, "volume", "total_volume", "持仓数量"))
        market_value = _extract_float(item, "market_value", "m_dMarketValue", "市值")
        unrealized = _extract_float(item, "unrealized_pnl", "m_dFloatProfit", "浮动盈亏")
        total_mv += market_value
        total_pnl += unrealized
        if code:
            lines.append(f"- {code} 持仓{volume} 市值{market_value:.2f} 浮盈{unrealized:.2f}")
    headline = f"持仓 {len(items)} 条 | 总市值 {total_mv:.2f} | 浮盈 {total_pnl:.2f}"
    return headline, lines


def _summarize_orders(items: List[Dict]) -> Tuple[str, List[str]]:
    if not items:
        return "委托 0 条", []
    lines: List[str] = []
    for item in items[:10]:
        code = _extract_item_code(item)
        volume = int(_extract_float(item, "order_volume", "volume", "委托数量"))
        price = _extract_float(item, "price", "order_price", "委托价格")
        status = str(item.get("order_status") or item.get("status") or item.get("委托状态") or "").strip()
        if code:
            lines.append(f"- {code} 委托{volume}@{price:.3f} 状态={status or 'unknown'}")
    return f"委托 {len(items)} 条", lines


def _summarize_trades(items: List[Dict]) -> Tuple[str, List[str]]:
    if not items:
        return "成交 0 条", []
    lines: List[str] = []
    for item in items[:10]:
        code = _extract_item_code(item)
        volume = int(_extract_float(item, "traded_volume", "volume", "成交数量"))
        price = _extract_float(item, "traded_price", "price", "成交均价")
        amount = _extract_float(item, "traded_amount", "amount", "成交金额")
        if code:
            lines.append(f"- {code} 成交{volume}@{price:.3f} 金额{amount:.2f}")
    return f"成交 {len(items)} 条", lines


def _collect_trade_logs(base_url: str, token: str, days: int = 3) -> List[Dict]:
    capped_days = max(1, min(10, int(days)))
    rows: List[Dict] = []
    for idx in range(capped_days):
        d = (date.today() - timedelta(days=idx)).isoformat()
        query = urllib.parse.urlencode({"lines": 200, "include_content": "true", "date": d})
        result = _http_get(base_url, f"/api/qmttrader_v2/logs?{query}&kind=all&max_files=20", token)
        if not result.get("ok"):
            rows.append({"date": d, "ok": False, "error": result.get("error", "request_failed")})
            continue
        payload = result.get("payload", {}) or {}
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        lines: List[str] = []
        if isinstance(data, dict):
            raw_lines = data.get("lines")
            if isinstance(raw_lines, list):
                lines = [str(item) for item in raw_lines if item is not None]
            entries = data.get("entries")
            if (not lines) and isinstance(entries, list):
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    content = entry.get("content")
                    if isinstance(content, list) and content:
                        lines = [str(item) for item in content if item is not None]
                        break
        err_hits = [
            str(line)
            for line in lines
            if any(key in str(line) for key in ("Traceback", "ERROR", "Exception", "失败", "超时", "断开"))
        ]
        rows.append(
            {
                "date": d,
                "ok": bool(payload.get("success")),
                "line_count": len(lines),
                "error_hits": len(err_hits),
            }
        )
    return rows


def _normalize_account(text: str) -> str:
    query = str(text or "").lower()
    if "国金" in query or "guojin" in query or "main" in query:
        return "guojin"
    if "东莞" in query or "dongguan" in query or "trade" in query:
        return "dongguan"
    return "all"


def _normalize_intent(text: str) -> str:
    query = str(text or "")
    ql = query.lower()
    if is_strategy_control_query(query):
        return "strategy_control"
    if any(key in query for key in ("帮助", "菜单", "命令", "使用说明")) or "help" in query.lower() or "menu" in query.lower():
        return "help"
    if any(key in query for key in ("影响", "指挥台", "全球影响", "新闻影响")) or "impact" in ql:
        return "global_impact"
    if any(key in query for key in ("全球", "突发", "海外", "全球新闻")) or "global" in ql or "breaking" in ql:
        return "global_events"
    if any(key in query for key in ("\u5173\u6ce8", "watchlist")) or "watchlist" in ql:
        return "watchlist_report"
    if any(key in query for key in ("审计", "能力审计", "能力", "阻塞项")) or "audit" in ql or "capability" in ql:
        return "capability_audit"
    if any(key in query for key in ("\u72b6\u6001", "\u603b\u89c8", "assistant status")) or "assistant status" in ql:
        return "assistant_status"
    if any(key in query for key in ("\u5468\u62a5", "\u5468\u5ea6", "\u672c\u5468")) or "weekly" in ql:
        return "weekly_report"
    if any(key in query for key in ("\u65e9\u62a5", "\u76d8\u524d", "morning brief")) or "morning" in ql:
        return "morning_brief"
    if any(key in query for key in ("\u6536\u76d8", "\u76d8\u540e", "closing brief")) or "closing" in ql:
        return "closing_brief"
    if "健康" in query or ("状态" in query and "运行" in query):
        return "health"
    if ("etf" in ql or "ETF" in query) and ("国金" in query or "13:20" in query or "1320" in ql or "14:20" in query or "1420" in ql):
        return "guojin_etf_brief"
    if "监控" in query or "候选" in query or "买入" in query:
        return "trade_monitor"
    if "持仓" in query:
        return "positions"
    if "委托" in query:
        return "orders"
    if "成交" in query:
        return "trades"
    if "日志" in query or "log" in ql:
        if "东莞" in query or "策略" in query or "nh" in ql or "mix" in ql:
            return "strategy_log_brief"
        return "logs"
    # 分析类查询
    if any(k in query for k in ("预测", "胜率", "准确率", "回测")):
        return "predictions"
    if any(k in query for k in ("风险", "敞口", "集中度", "回撤", "仓位")):
        return "risk"
    if any(k in query for k in ("反思", "复盘", "摘要", "简报")):
        return "reflection"
    if any(k in query for k in ("策略", "权重", "进化", "规则")):
        return "strategy"
    if any(k in query for k in ("长线", "模拟盘", "组合", "调仓计划")):
        return "longterm_portfolio"
    return "summary"


def _extract_query_date(text: str) -> str:
    query = str(text or "").strip()
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", query)
    if m:
        return m.group(1)
    m = re.search(r"\b(20\d{6})\b", query)
    if m:
        raw = m.group(1)
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    if "昨天" in query:
        return (date.today() - timedelta(days=1)).isoformat()
    if "前天" in query:
        return (date.today() - timedelta(days=2)).isoformat()
    if "今天" in query:
        return date.today().isoformat()
    return ""


def _extract_days(text: str) -> int:
    query = str(text or "")
    matched = re.search(r"最近\s*(\d+)\s*天", query)
    if not matched:
        return 3
    return max(1, min(10, int(matched.group(1))))


def _query_health(account: str, token: str) -> str:
    base_url = _resolve_base_url(account)
    result = _http_get(base_url, "/health", token)
    alias = ACCOUNT_ALIASES.get(account, account)
    if not result.get("ok"):
        return f"{alias} 健康检查失败: {result.get('error', 'unknown_error')}"
    payload = result.get("payload", {}) or {}
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    return (
        f"{alias} health={data.get('status', 'unknown')} "
        f"trade_connected={data.get('trade_connected', False)} "
        f"market_available={data.get('market_available', False)}"
    )


def _query_endpoint(account: str, endpoint: str, token: str) -> str:
    base_url = _resolve_base_url(account)
    alias = ACCOUNT_ALIASES.get(account, account)
    path = f"/api/stock/{endpoint}"
    result = _http_get(base_url, path, token)
    if not result.get("ok"):
        if endpoint == "positions":
            fallback_rows, as_of = _fallback_positions_from_snapshot(account)
            if fallback_rows:
                head, details = _summarize_positions(fallback_rows)
                note = f"realtime_failed={result.get('error', 'unknown_error')}; fallback_snapshot={as_of}"
                return "\n".join([f"{alias} {head} ({note})", *details[:8]])
        return f"{alias} {endpoint} request_failed: {result.get('error', 'unknown_error')}"
    payload = result.get("payload", {}) or {}
    if not bool(payload.get("success")):
        return f"{alias} {endpoint} 返回失败: {payload.get('message', 'unknown')}"
    rows = payload.get("data", [])
    if not isinstance(rows, list):
        return f"{alias} {endpoint} 返回格式异常"
    fallback_note = ""
    if endpoint == "positions" and not rows:
        diag_note = _diagnose_empty_positions(base_url, token)
        if diag_note:
            fallback_note = f"（{diag_note}）"
        fallback_rows, as_of = _fallback_positions_from_snapshot(account)
        if fallback_rows:
            rows = fallback_rows
            fallback_note = f"（实时持仓为空，回退快照 {as_of}）"
    if endpoint == "positions":
        head, details = _summarize_positions(rows)
    elif endpoint == "orders":
        head, details = _summarize_orders(rows)
    else:
        head, details = _summarize_trades(rows)
    return "\n".join([f"{alias} {head}{fallback_note}", *details[:8]])


def _fallback_positions_from_snapshot(account: str) -> Tuple[List[Dict], str]:
    """Fallback to latest combined portfolio snapshot when realtime positions are empty."""
    try:
        import db as db_mod
    except Exception:
        return [], ""

    snapshot = db_mod.get_latest_portfolio_snapshot(account_scope="combined")
    if not snapshot:
        return [], ""
    data = snapshot.get("data", {}) or {}
    positions = data.get("qmt_positions", data.get("positions", [])) or []
    if not isinstance(positions, list) or not positions:
        return [], str(snapshot.get("as_of_date", "") or "")

    def _is_usable_position(item: Dict) -> bool:
        if not isinstance(item, dict):
            return False
        code = _extract_item_code(item)
        if not code:
            return False
        volume = _extract_float(item, "volume", "current_volume", "total_volume", "持仓数量")
        market_value = _extract_float(item, "market_value", "m_dMarketValue", "市值")
        unrealized = _extract_float(item, "unrealized_pnl", "m_dFloatProfit", "profit_loss", "浮动盈亏")
        return bool(volume > 0 or market_value > 0 or abs(unrealized) > 0)

    usable_positions = [item for item in positions if _is_usable_position(item)]
    if not usable_positions:
        return [], str(snapshot.get("as_of_date", "") or "")

    expected_source = "main" if account == "guojin" else "trade"
    has_source = any(str(item.get("_source", "")).strip() for item in usable_positions)
    if not has_source:
        # 无来源字段时无法按国金/东莞拆分，避免错误归属
        return [], str(snapshot.get("as_of_date", "") or "")
    filtered = [item for item in usable_positions if str(item.get("_source", "")).lower() == expected_source]
    return filtered, str(snapshot.get("as_of_date", "") or "")


def _diagnose_empty_positions(base_url: str, token: str) -> str:
    """Diagnose why positions endpoint returns empty list."""
    result = _http_get(base_url, "/api/stock/asset", token)
    if not result.get("ok"):
        return "资产探测失败"
    payload = result.get("payload", {}) or {}
    if not bool(payload.get("success")):
        return "资产接口失败"
    asset = payload.get("data")
    if asset is None:
        return "资产接口返回空"
    if isinstance(asset, dict):
        market_value = _extract_float(asset, "market_value", "m_dMarketValue", "市值")
        total_asset = _extract_float(asset, "total_asset", "m_dTotalAsset", "总资产")
        if market_value > 0:
            return f"资产市值{market_value:.2f}但持仓为空"
        if total_asset > 0:
            return "账户可用但当前无持仓"
    return ""


def _query_logs(account: str, token: str, days: int) -> str:
    base_url = _resolve_base_url(account)
    alias = ACCOUNT_ALIASES.get(account, account)
    rows = _collect_trade_logs(base_url, token, days=days)
    parts = [f"{alias} 最近{len(rows)}天 qmttrader_v2 日志"]
    for item in rows:
        if not item.get("ok"):
            parts.append(f"- {item.get('date')} 失败: {item.get('error', 'unknown_error')}")
            continue
        parts.append(
            f"- {item.get('date')} line_count={item.get('line_count', 0)} error_hits={item.get('error_hits', 0)}"
        )
    return "\n".join(parts)


def _query_predictions() -> str:
    """查询最近预测结果与胜率。"""
    import db as db_mod

    from datetime import datetime, timedelta
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    checked = db_mod.get_checked_predictions_in_range(start_date, end_date)
    unchecked = db_mod.get_unchecked_predictions(before_date=end_date)

    lines = ["📊 预测概览", ""]
    if checked:
        total = len(checked)
        correct = sum(1 for p in checked if p.get("is_correct"))
        win_rate = correct / total * 100 if total else 0
        lines.append(f"近7天已回测: {total}条 | 正确{correct} | 胜率{win_rate:.0f}%")

        # 按标的分组
        by_target = {}
        for pred in checked:
            name = pred.get("target_name", pred.get("target", "?"))
            by_target.setdefault(name, []).append(pred)
        for name, preds in sorted(by_target.items()):
            items = len(preds)
            won = sum(1 for p in preds if p.get("is_correct"))
            lines.append(f"  {name}: {won}/{items} ({won/items*100:.0f}%)" if items else f"  {name}: 无数据")
    else:
        lines.append("近7天无已回测预测")

    if unchecked:
        lines.append(f"\n待回测: {len(unchecked)}条")
    return "\n".join(lines)


def _query_risk() -> str:
    try:
        return build_risk_report().get("text", "")
    except Exception as exc:
        return f"risk report failed: {exc}"

def _query_reflection() -> str:
    """查询最新反思摘要。"""
    import os
    from datetime import datetime

    reports_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "reflection_reports",
    )
    date_str = datetime.now().strftime("%Y%m%d")
    report_path = os.path.join(reports_dir, f"reflection_{date_str}.md")
    if not os.path.exists(report_path):
        # Try yesterday
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        report_path = os.path.join(reports_dir, f"reflection_{yesterday}.md")
        if not os.path.exists(report_path):
            return "暂无反思报告，请执行 `python3 main.py reflect` 生成"

    with open(report_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Return first 1200 chars
    if len(content) > 1500:
        content = content[:1500] + "\n\n... (完整报告见 reflection_reports/)"
    return content


def _query_strategy() -> str:
    """查询当前策略配置与表现。"""
    import json
    import os

    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "data",
        "strategy_config.json",
    )
    lines = ["⚙️ 策略配置", ""]
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        weights = config.get("weights", {})
        lines.append("当前权重:")
        for name, weight in weights.items():
            lines.append(f"  {name}: {weight:.1%}")
        lines.append(f"\n自动调整: {'启用' if config.get('auto_adjust_enabled') else '禁用'}")
        history = config.get("weight_history", [])
        if history:
            latest = history[-1]
            lines.append(f"最近调整: {latest.get('date', 'N/A')}")
            if latest.get("performance"):
                lines.append("近期表现:")
                for perf in latest["performance"][:4]:
                    name = perf.get("strategy_used", "?")
                    wr = perf.get("win_rate", 0)
                    lines.append(f"  {name}: 胜率{wr:.0f}% ({perf.get('correct', 0)}/{perf.get('total', 0)})")
    else:
        lines.append("配置文件不存在")
    return "\n".join(lines)


def _query_longterm_portfolio() -> str:
    summary = summarize_longterm_snapshot(load_longterm_snapshot())
    lines = ["🧭 长线组合（模拟盘）", build_longterm_snapshot_text(summary)]
    if not summary.get("available"):
        lines.append("提示: 先在 trading 侧执行 longterm 命令生成 investor_longterm_snapshot.json")
        return "\n".join(lines)

    rejected_summary = summary.get("rejected_reason_summary", []) or []
    if rejected_summary:
        lines.append(
            "计划拒绝原因: "
            + " | ".join(
                f"{str(item.get('reason', 'unknown'))}:{int(item.get('count', 0) or 0)}"
                for item in rejected_summary[:6]
            )
        )
    lines.append(f"快照路径: {summary.get('snapshot_path', '')}")
    return "\n".join(lines)


def _query_trade_monitor(query: str) -> str:
    date_text = _extract_query_date(query)
    try:
        return format_today_summary_text(date=date_text or None)
    except Exception as exc:
        return f"交易监控汇总生成失败: {exc}"


def _query_strategy_log_brief(query: str) -> str:
    date_text = _extract_query_date(query)
    try:
        return run_scheduled_briefing("0945", date_text=date_text or "")
    except Exception as exc:
        return f"东莞策略日志简报生成失败: {exc}"


def _extract_guojin_slot(query: str) -> str:
    q = str(query or "").lower()
    if "13:20" in q or "1320" in q:
        return "1320"
    if "14:20" in q or "1420" in q:
        return "1420"
    return ""


def _query_guojin_etf_brief(query: str) -> str:
    date_text = _extract_query_date(query)
    slot = _extract_guojin_slot(query)
    try:
        if slot:
            return run_scheduled_briefing(slot, date_text=date_text or "")
        first = run_scheduled_briefing("1320", date_text=date_text or "")
        second = run_scheduled_briefing("1420", date_text=date_text or "")
        return f"{first}\n\n{second}"
    except Exception as exc:
        return f"国金ETF简报生成失败: {exc}"



def _query_assistant_status() -> str:
    try:
        return build_assistant_status().get("text", "")
    except Exception as exc:
        return f"assistant status failed: {exc}"

def _query_watchlist_report() -> str:
    try:
        return build_watchlist_report(limit_events=80, top_n=12).get("text", "")
    except Exception as exc:
        return f"watchlist report failed: {exc}"

def _query_global_events() -> str:
    try:
        brief = build_global_event_brief(limit=80, min_score=45, top_n=6)
        return format_global_event_brief(brief)
    except Exception as exc:
        return f"global event brief failed: {exc}"

def _query_global_impact() -> str:
    try:
        return build_global_impact_brief(limit=80, min_score=45, top_n=8, use_cache=True, max_cache_minutes=60).get("text", "")
    except Exception as exc:
        return f"global impact brief failed: {exc}"
def _query_weekly_report() -> str:
    try:
        return build_weekly_report(days=7).get("text", "")
    except Exception as exc:
        return f"weekly report failed: {exc}"


def _query_morning_brief() -> str:
    try:
        return build_morning_brief().get("text", "")
    except Exception as exc:
        return f"morning brief failed: {exc}"


def _query_closing_brief() -> str:
    try:
        return build_closing_brief().get("text", "")
    except Exception as exc:
        return f"closing brief failed: {exc}"

def _query_capability_audit() -> str:
    path = Path("/root/.openclaw/workspace/reports/investor_assistant_capability_audit_latest.json")
    if not path.exists():
        return "No capability audit report yet. Run: cd /root/.openclaw/workspace && python3 scripts/investor_assistant_audit.py"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"Capability audit report read failed: {exc}"
    lines = [
        "OpenClaw capability audit",
        f"time: {payload.get('generated_at', '')}",
        f"overall: {payload.get('overall', '')} | blocked={payload.get('blocked_count', 0)} warn={payload.get('warning_count', 0)}",
    ]
    for item in payload.get("items", []):
        status = item.get("status", "")
        name = item.get("name", "")
        evidence = (item.get("evidence") or [""])[0]
        lines.append(f"- {status}: {name} | {evidence}")
    lines.append("Full report: /root/.openclaw/workspace/reports/investor_assistant_capability_audit_latest.md")
    return "\n".join(lines)

def _help_text() -> str:
    return format_assistant_menu_text()

def handle_feishu_query(query_text: str) -> str:
    query = str(query_text or "").strip()
    if not query:
        return _help_text()
    intent = _normalize_intent(query)
    if intent == "help":
        return _help_text()
    if intent == "assistant_status":
        return _query_assistant_status()
    if intent == "global_events":
        return _query_global_events()
    if intent == "global_impact":
        return _query_global_impact()
    if intent == "watchlist_report":
        return _query_watchlist_report()
    if intent == "capability_audit":
        return _query_capability_audit()
    if intent == "weekly_report":
        return _query_weekly_report()
    if intent == "morning_brief":
        return _query_morning_brief()
    if intent == "closing_brief":
        return _query_closing_brief()

    if intent == "strategy_control":
        return handle_strategy_control_query(query)

    # 分析类查询（不需要 qmt2http token）
    if intent == "predictions":
        return _query_predictions()
    if intent == "risk":
        return _query_risk()
    if intent == "reflection":
        return _query_reflection()
    if intent == "strategy":
        return _query_strategy()
    if intent == "longterm_portfolio":
        return _query_longterm_portfolio()
    if intent == "trade_monitor":
        return _query_trade_monitor(query)
    if intent == "strategy_log_brief":
        return _query_strategy_log_brief(query)
    if intent == "guojin_etf_brief":
        return _query_guojin_etf_brief(query)

    # 实盘数据查询（需要 token）
    account = _normalize_account(query)
    token = _resolve_token()
    if not token:
        return "未找到 QMT2HTTP_API_TOKEN，无法查询实盘接口\n\n💡 试试分析类查询: 最近预测 / 风险敞口 / 策略表现"
    accounts = ["guojin", "dongguan"] if account == "all" else [account]

    lines: List[str] = []
    if intent == "summary":
        for current in accounts:
            lines.append(_query_health(current, token))
            lines.append(_query_endpoint(current, "positions", token))
            lines.append(_query_endpoint(current, "orders", token))
            lines.append(_query_endpoint(current, "trades", token))
        return "\n\n".join(lines)
    if intent == "health":
        return "\n".join(_query_health(current, token) for current in accounts)
    if intent in {"positions", "orders", "trades"}:
        return "\n\n".join(_query_endpoint(current, intent, token) for current in accounts)
    if intent == "logs":
        days = _extract_days(query)
        return "\n\n".join(_query_logs(current, token, days) for current in accounts)
    return _help_text()

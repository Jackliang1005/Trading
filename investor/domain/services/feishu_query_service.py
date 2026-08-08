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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from domain.policies.advisor_policy import ADVISOR_PROFILES, confirm_advisor_profile, load_advisor_policy
from domain.services.assistant_menu_service import format_assistant_menu_text
from domain.services.assistant_status_service import build_assistant_status
from domain.services.watchlist_report_service import build_watchlist_report
from domain.services.report_style_service import money
from domain.services.delivery_audit_service import format_delivery_audit_text
from position_pnl import resolve_position_pnl

from domain.services.live_monitor_view_service import format_today_summary_text
from domain.services.longterm_portfolio_service import (
    build_longterm_snapshot_text,
    format_longterm_rejected_reasons,
    load_longterm_snapshot,
    summarize_longterm_snapshot,
)
from workflows.scheduled_briefings import run_scheduled_briefing
from domain.services.event_service import build_global_event_brief, format_global_event_brief
from domain.services.weekly_report_service import build_weekly_report
from domain.services.risk_report_service import build_risk_report
from domain.services.morning_brief_service import build_morning_brief
from domain.services.closing_brief_service import build_closing_brief
from domain.services.decision_monitor_service import format_decision_monitor_text
from domain.services.global_impact_service import build_global_impact_brief
from domain.services.intraday_outlook_service import build_intraday_outlook
from domain.services.reflection_runtime_service import (
    _decision_monitor_attribution,
    format_reflection_push_text,
)
from domain.services.qmt_strategy_control_service import handle_strategy_control_query, is_strategy_control_query
from domain.services.qmt_position_monitor_exemptions_service import (
    handle_position_monitor_exemptions_query,
    is_position_monitor_exemptions_query,
)
from domain.services.qmt_t_monitor_service import handle_t_monitor_query, is_t_monitor_query


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
        pnl_evidence = resolve_position_pnl(item)
        unrealized = float(pnl_evidence["pnl"])
        total_mv += market_value
        total_pnl += unrealized
        if code:
            conflict = "｜盈亏字段待核验" if pnl_evidence["cumulative_cost_conflict"] else ""
            lines.append(f"- {code}：{volume} 股｜市值 {market_value:,.2f} 元｜持仓盈亏 {unrealized:+,.2f} 元{conflict}")
    headline = f"持仓 {len(items)} 只｜总市值 {total_mv:,.2f} 元｜持仓盈亏 {total_pnl:+,.2f} 元"
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
        status_label = {
            "filled": "已成交",
            "submitted": "已提交",
            "pending": "等待成交",
            "cancelled": "已撤单",
            "canceled": "已撤单",
            "rejected": "已拒绝",
        }.get(status.lower(), status or "待确认")
        if code:
            lines.append(f"- {code}：委托 {volume} 股｜价格 {price:.3f} 元｜状态：{status_label}")
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
            lines.append(f"- {code}：成交 {volume} 股｜均价 {price:.3f} 元｜金额 {amount:,.2f} 元")
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
        log_summary = _summarize_log_payload(data if isinstance(data, dict) else {})
        rows.append(
            {
                "date": d,
                "ok": bool(payload.get("success")),
                **log_summary,
            }
        )
    return rows


def _summarize_log_payload(data: Dict) -> Dict:
    """Aggregate every returned log file without letting one file clear another."""
    groups: List[List[str]] = []
    raw_lines = data.get("lines")
    if isinstance(raw_lines, list):
        groups.append([str(item) for item in raw_lines if item is not None])
    if not groups:
        entries = data.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                content = entry.get("content")
                if isinstance(content, list):
                    groups.append([str(item) for item in content if item is not None])

    totals = {"error_hits": 0, "recovered_error_hits": 0}
    categories: Dict[str, int] = {}
    signatures = set()
    for lines in groups:
        summary = _summarize_log_errors(lines)
        totals["error_hits"] += int(summary.get("error_hits", 0) or 0)
        totals["recovered_error_hits"] += int(summary.get("recovered_error_hits", 0) or 0)
        for name, count in (summary.get("error_categories") or {}).items():
            categories[name] = categories.get(name, 0) + int(count or 0)
        signatures.update(str(item) for item in (summary.get("error_signatures") or []) if item)
    return {
        "line_count": sum(len(lines) for lines in groups),
        **totals,
        "error_categories": categories,
        "error_signatures": sorted(signatures),
        "file_count": len(groups),
    }


def _summarize_log_errors(lines: List[str]) -> Dict:
    """Separate active failures from startup errors followed by a healthy heartbeat."""
    error_keys = ("Traceback", "ERROR", "Exception", "失败", "超时", "断开")
    error_indexes = [index for index, line in enumerate(lines) if any(key in str(line) for key in error_keys)]
    healthy_indexes = [
        index
        for index, line in enumerate(lines)
        if "heartbeat status=ok" in str(line).lower() or "heartbeat_status=ok" in str(line).lower()
    ]
    last_healthy_index = max(healthy_indexes, default=-1)
    active_indexes = [index for index in error_indexes if index > last_healthy_index]
    recovered_indexes = [index for index in error_indexes if index <= last_healthy_index]

    categories = {"连接链路": 0, "数据质量": 0, "运行异常": 0}
    for index in active_indexes:
        text = str(lines[index]).lower()
        if any(key in text for key in ("无法连接", "断开", "超时", "providerunavailable", "connection")):
            categories["连接链路"] += 1
        elif any(key in text for key in ("invalid stockcode", "invalid symbol", "无效代码")):
            categories["数据质量"] += 1
        else:
            categories["运行异常"] += 1
    active_region = "\n".join(str(line).lower() for line in lines[min(active_indexes):]) if active_indexes else ""
    signatures = []
    if "qmt trader connect failed" in active_region:
        signatures.append("QMT交易连接失败")
    if "无法连接行情服务" in active_region or "no provider returned realtime quotes" in active_region:
        signatures.append("行情服务不可用")
    if "invalid stockcode" in active_region or "invalid symbol" in active_region:
        signatures.append("证券代码无效")
    return {
        "error_hits": len(active_indexes),
        "recovered_error_hits": len(recovered_indexes),
        "error_categories": {key: value for key, value in categories.items() if value},
        "error_signatures": signatures,
    }


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
    if any(key in query for key in ("风险偏好", "风险档案", "投顾档案")):
        return "advisor_profile"
    if "\u65e5\u5185\u9884\u6d4b" in query or "intraday outlook" in ql:
        return "intraday_outlook"
    if is_position_monitor_exemptions_query(query):
        return "position_monitor_exemptions"
    if is_t_monitor_query(query):
        return "t_monitor"
    if any(key in query for key in ("推送验收", "投递验收")):
        return "live_delivery_acceptance"
    if query.strip().lower() in {"/投顾", "投顾", "/advisor", "advisor brief"} or any(
        key in query for key in ("投顾总览", "今日投顾", "助理简报")
    ):
        return "advisor_brief"
    if any(key in query for key in ("推送状态", "投递状态")):
        return "delivery_audit"
    if "\u677f\u5757" in query and any(key in query for key in ("\u6da8", "\u5f3a", "\u70ed", "\u8d44\u91d1")):
        return "skill_sector_flow"
    if any(key in query for key in ("宏观", "CPI", "PPI", "M2", "社融", "LPR", "社零")):
        return "skill_macro"
    if any(key in query for key in ("筛选", "选股", "PE<", "PB<", "ROE>")):
        return "skill_screener"
    if any(key in query for key in ("市场情绪", "涨停", "连板", "情绪")):
        return "skill_sentiment"
    if "回测" in query or query.strip().startswith("/backtest"):
        return "skill_backtest"
    if any(key in query for key in ("估值", "PE", "PB", "PEG", "杜邦", "基本面分析")):
        return "skill_fundamental"
    if any(key in query for key in ("财务", "基本面", "ROE", "毛利率", "负债率")):
        return "skill_financial"
    if any(key in query for key in ("公告解读", "新闻解读", "研报解读")) or ("解读" in query and any(key in query for key in ("公告", "新闻", "研报"))):
        return "skill_news_interpretation"
    if "公告" in query or "业绩预告" in query or "减持" in query:
        return "skill_announcements"
    if any(key in query for key in ("技术分析", "技术面", "MACD", "KDJ", "RSI", "均线")) or query.strip().startswith("/技术"):
        return "skill_technical"
    if "行情" in query or query.strip().startswith("/报价"):
        return "skill_quote"
    if "\u5206\u6790" in query or "\u600e\u4e48\u6837" in query:
        return "skill_overview"
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
    if any(key in query for key in ("\u65e9\u62a5", "\u6668\u62a5", "\u76d8\u524d", "morning brief")) or "morning" in ql:
        return "morning_brief"
    if any(key in query for key in ("\u6536\u76d8", "\u76d8\u540e", "closing brief")) or "closing" in ql:
        return "closing_brief"
    if "健康" in query or ("状态" in query and "运行" in query):
        return "health"
    if ("etf" in ql or "ETF" in query) and ("国金" in query or "13:20" in query or "1320" in ql or "14:20" in query or "1420" in ql):
        return "guojin_etf_brief"
    if "交易建议" in query or "持仓建议" in query or query.strip().lower() in {"/建议", "建议"}:
        return "decision_monitor"
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
    if any(key in query for key in ("账户", "账户概览", "资产")):
        return "summary"
    return "help"


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


def _connection_error_cn(error: object) -> str:
    text = str(error or "").lower()
    if "timed out" in text or "timeout" in text:
        return "连接超时"
    if "closed connection" in text or "remote end closed" in text:
        return "远端主动关闭连接"
    if "refused" in text:
        return "连接被拒绝"
    if "http 502" in text or "bad gateway" in text:
        return "上游服务暂不可用（HTTP 502）"
    return "连接失败"


def _query_health(account: str, token: str) -> str:
    base_url = _resolve_base_url(account)
    result = _http_get(base_url, "/health", token)
    alias = ACCOUNT_ALIASES.get(account, account)
    if not result.get("ok"):
        return "\n".join([
            f"❌ {alias}连接状态检查失败",
            "",
            f"- 原因：{_connection_error_cn(result.get('error'))}。",
            "- 本次没有可验证的交易或行情连接结论。",
        ])
    payload = result.get("payload", {}) or {}
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    health = "正常" if str(data.get("status") or "").lower() in {"ok", "healthy", "up"} else "异常"
    trade = "已连接" if data.get("trade_connected") else "未连接"
    market = "可用" if data.get("market_available") else "不可用"
    return "\n".join([
        f"🩺 {alias}连接状态",
        f"查询时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"- 服务健康：{health}",
        f"- 交易连接：{trade}",
        f"- 行情能力：{market}",
        "- 以上仅代表 qmt2http 健康接口的本次回读；行情不可用时不生成实时价格判断。",
    ])


def _query_endpoint(account: str, endpoint: str, token: str) -> str:
    base_url = _resolve_base_url(account)
    alias = ACCOUNT_ALIASES.get(account, account)
    endpoint_label = {"positions": "持仓", "orders": "委托", "trades": "成交"}.get(endpoint, endpoint)
    path = f"/api/stock/{endpoint}"
    result = _http_get(base_url, path, token)
    if not result.get("ok"):
        if endpoint == "positions":
            fallback_rows, as_of = _fallback_positions_from_snapshot(account)
            if fallback_rows:
                head, details = _summarize_positions(fallback_rows)
                note = f"实时接口{_connection_error_cn(result.get('error'))}；以下为 {as_of or '日期未知'} 的历史快照，不代表当前持仓"
                return "\n".join([f"📦 {alias}持仓（降级快照）", f"- 数据说明：{note}。", f"- 快照汇总：{head}", *details[:8]])
        return f"{alias}{endpoint_label}查询失败：{_connection_error_cn(result.get('error'))}，本次没有可验证数据。"
    payload = result.get("payload", {}) or {}
    if not bool(payload.get("success")):
        return f"{alias}{endpoint_label}查询失败：{payload.get('message') or '接口未返回原因'}"
    rows = payload.get("data", [])
    if not isinstance(rows, list):
        return f"{alias}{endpoint_label}查询失败：接口返回格式异常"
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
    title = {"positions": "📦 持仓回读", "orders": "📋 委托回读", "trades": "✅ 成交回读"}.get(endpoint, endpoint_label)
    return "\n".join([
        f"{title}｜{alias}",
        f"查询时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"- 接口结果：{head}{fallback_note}。",
        *details[:8],
        "- 以上仅代表 qmt2http 本次回读；接口不可用或使用历史快照时会明确标注，不据此推断成交。",
    ])


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
        unrealized = float(resolve_position_pnl(item)["pnl"])
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
    parts = [f"🧾 {alias}策略日志检查", f"检查范围：最近 {len(rows)} 天", ""]
    for item in rows:
        if not item.get("ok"):
            parts.append(f"- {item.get('date')}：日志接口{_connection_error_cn(item.get('error'))}，当天状态无法核验。")
            continue
        error_hits = int(item.get("error_hits", 0) or 0)
        recovered_hits = int(item.get("recovered_error_hits", 0) or 0)
        if error_hits:
            category_text = "、".join(
                f"{name} {count} 条" for name, count in (item.get("error_categories") or {}).items()
            )
            if category_text:
                parts.append(f"- {item.get('date')}：读取 {int(item.get('line_count', 0) or 0)} 行，存在 {error_hits} 条活跃异常（{category_text}），需要检查。")
            else:
                parts.append(f"- {item.get('date')}：读取 {int(item.get('line_count', 0) or 0)} 行，命中 {error_hits} 条异常关键词，需要检查。")
        elif recovered_hits:
            parts.append(
                f"- {item.get('date')}：读取 {int(item.get('line_count', 0) or 0)} 行；"
                f"早前 {recovered_hits} 条异常后已有健康心跳，当前按已恢复记录。"
            )
        else:
            parts.append(f"- {item.get('date')}：读取 {int(item.get('line_count', 0) or 0)} 行，未命中异常关键词。")
    parts.append("- 这里只做日志关键词检查，不等同于交易与行情链路完整可用。")
    return "\n".join(parts)


def _query_account_health_evidence(account: str, token: str) -> str:
    """Summarize live gateway, account-read, and strategy-log evidence."""
    alias = ACCOUNT_ALIASES.get(account, account)
    base_url = _resolve_base_url(account)
    lines = [_query_health(account, token), "", f"**{alias}账户与策略日志证据**"]

    positions_result = _http_get(base_url, "/api/stock/positions", token)
    if not positions_result.get("ok"):
        lines.append(f"- 持仓读取：失败（{_connection_error_cn(positions_result.get('error'))}），本次不使用缓存冒充实时账户状态。")
    else:
        payload = positions_result.get("payload", {}) or {}
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not bool(payload.get("success")):
            lines.append(f"- 持仓读取：接口失败（{payload.get('message') or '未返回原因'}）。")
        elif isinstance(rows, list):
            lines.append(f"- 持仓读取：成功，实时接口返回 {len(rows)} 条持仓记录。")
        else:
            lines.append("- 持仓读取：返回格式异常，不能确认实时持仓数量。")

    log_rows = _collect_trade_logs(base_url, token, days=1)
    log_item = log_rows[0] if log_rows else {}
    if not log_item.get("ok"):
        lines.append(f"- 当日策略日志：读取失败（{_connection_error_cn(log_item.get('error'))}）。")
    else:
        line_count = int(log_item.get("line_count", 0) or 0)
        error_hits = int(log_item.get("error_hits", 0) or 0)
        recovered_hits = int(log_item.get("recovered_error_hits", 0) or 0)
        if line_count < 1:
            lines.append("- 当日策略日志：接口可用但未读到日志行，需要结合是否为交易日与策略进程继续核验。")
        elif error_hits:
            category_text = "、".join(
                f"{name} {count} 条" for name, count in (log_item.get("error_categories") or {}).items()
            )
            suffix = f"（{category_text}）" if category_text else ""
            lines.append(f"- 当日策略日志：读取 {line_count} 行，存在 {error_hits} 条活跃异常{suffix}，需要检查。")
            signatures = "、".join(str(item) for item in (log_item.get("error_signatures") or []) if item)
            if signatures:
                lines.append(f"- 根因签名：{signatures}。请在对应 Windows QMT 客户端核验登录、交易连接与行情连接。")
        elif recovered_hits:
            lines.append(f"- 当日策略日志：早前 {recovered_hits} 条异常后已有健康心跳，当前按已恢复记录。")
        else:
            lines.append(f"- 当日策略日志：读取 {line_count} 行，未命中异常关键词。")
    return "\n".join(lines)


def _query_unified_health(accounts: List[str], token: str) -> str:
    """Return local assistant health even when authenticated gateway probes cannot run."""
    sections = [_query_assistant_status(), "**账户网关实时诊断**"]
    if not token:
        sections.append(
            "- 未找到 QMT2HTTP_API_TOKEN：核心服务、定时器和报告状态仍已完成检查；"
            "账户读取与策略日志本次无法核验。"
        )
    else:
        sections.extend(_query_account_health_evidence(account, token) for account in accounts)
    sections.append("- 健康检查只读取运行证据，不会触发下单或改变仓位。")
    return "\n\n".join(section for section in sections if section)


def _query_predictions() -> str:
    """查询最近预测结果与胜率。"""
    import db as db_mod

    from datetime import datetime, timedelta
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    checked = db_mod.get_checked_predictions_in_range(start_date, end_date)
    unchecked = db_mod.get_unchecked_predictions(before_date=end_date)

    lines = ["📊 预测验证概览", f"统计区间：{start_date} 至 {end_date}", "", "**结论**"]
    if checked:
        total = len(checked)
        correct = sum(1 for p in checked if p.get("is_correct"))
        win_rate = correct / total * 100 if total else 0
        if total < 5:
            lines.append(f"- 已完成 {total} 条预测验证，其中 {correct} 条方向正确；样本不足 5 条，暂不把胜率作为有效评价。")
        else:
            lines.append(f"- 已完成 {total} 条预测验证，其中 {correct} 条方向正确，历史胜率 {win_rate:.0f}%。")

        # 按标的分组
        lines.extend(["", "**按标的拆分**"])
        by_target = {}
        for pred in checked:
            name = pred.get("target_name", pred.get("target", "?"))
            by_target.setdefault(name, []).append(pred)
        for name, preds in sorted(by_target.items()):
            items = len(preds)
            won = sum(1 for p in preds if p.get("is_correct"))
            lines.append(f"- {name}：{won}/{items} 条方向正确。" if items else f"- {name}：暂无可验证数据。")
    else:
        lines.append("- 近 7 天没有完成回测的预测，因此无法计算可靠胜率。")

    if unchecked:
        lines.extend(["", "**等待验证**", f"- 还有 {len(unchecked)} 条预测尚未到验证时间或缺少对应行情。"])
    lines.extend(["", "**边界**", "- 未完成验证的预测不计入胜率；历史正确率也不代表下一次预测一定正确。"])
    return "\n".join(lines)


def _query_risk() -> str:
    try:
        return build_risk_report().get("text", "")
    except Exception:
        return "风险报告生成失败；本次不输出仓位或风险结论。"

def _query_reflection() -> str:
    """查询最新反思摘要。"""
    from datetime import datetime

    reports_dir = Path(__file__).resolve().parents[2] / "reflection_reports"
    candidates = sorted(reports_dir.glob("trading_summary_*.json"), reverse=True)
    if not candidates:
        return "暂无可用的交易复盘快照；本次不根据旧报告推断交易结果。"
    try:
        summary = json.loads(candidates[0].read_text(encoding="utf-8"))
    except Exception:
        return "最新交易复盘快照无法读取；本次不输出成交或盈亏结论。"
    as_of = str(summary.get("as_of_date") or candidates[0].stem.removeprefix("trading_summary_") or "")
    if len(as_of) == 8 and as_of.isdigit():
        as_of = f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]}"
    return format_reflection_push_text(
        summary,
        _decision_monitor_attribution(summary),
        summary.get("prediction_validation") or {},
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        as_of,
    )


def _query_strategy() -> str:
    """查询当前策略配置与表现。"""
    import json
    import os

    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "data",
        "strategy_config.json",
    )
    strategy_labels = {
        "technical": "技术趋势",
        "fundamental": "基本面",
        "sentiment": "市场情绪",
        "geopolitical": "宏观与地缘",
    }
    lines = ["⚙️ 预测策略概览", ""]
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        weights = config.get("weights", {})
        lines.append("**当前信号权重**")
        for name, weight in weights.items():
            lines.append(f"- {strategy_labels.get(name, '其他信号')}：{weight:.1%}")
        lines.append(f"\n- 自动校准：{'已启用' if config.get('auto_adjust_enabled') else '未启用'}")
        history = config.get("weight_history", [])
        if history:
            latest = history[-1]
            latest_date = str(latest.get("date") or "").strip()
            lines.append(f"- 最近校准：{latest_date or '暂无记录'}")
            try:
                calibration_age = (date.today() - date.fromisoformat(latest_date[:10])).days
            except Exception:
                calibration_age = None
            if calibration_age is None:
                lines.append("- 校准日期无法验证；当前权重仅作研究参考。")
            elif calibration_age > 30:
                lines.append(f"- 校准距今 {calibration_age} 天，已超过 30 天新鲜度要求；在新增验证样本前，当前权重仅作参考。")
            if latest.get("performance"):
                lines.extend(["", "**近期验证样本**"])
                for perf in latest["performance"][:4]:
                    name = perf.get("strategy_used", "?")
                    wr = perf.get("win_rate", 0)
                    correct = int(perf.get("correct", 0) or 0)
                    total = int(perf.get("total", 0) or 0)
                    label = strategy_labels.get(name, "其他信号")
                    if total < 5:
                        lines.append(f"- {label}：仅 {total} 个已验证样本，暂不足以评价胜率。")
                    else:
                        lines.append(f"- {label}：{correct}/{total} 次方向正确，胜率 {wr:.0f}%。")
            if (latest.get("evidence") or {}).get("legacy_weights_quarantined"):
                lines.append("- 历史未画像化样本形成的权重已隔离；当前已恢复系统基线，满足20/5/2门槛前不再校准。")
        lines.extend(["", "**使用边界**", "- 权重只用于生成研究判断，不会据此自动下单。"])
    else:
        lines.append("当前没有可用的策略配置，无法评价信号权重。")
    return "\n".join(lines)


def _query_longterm_portfolio() -> str:
    summary = summarize_longterm_snapshot(load_longterm_snapshot())
    lines = ["🧭 长线组合（模拟盘）", "", "**组合概览**", f"- {build_longterm_snapshot_text(summary)}"]
    if not summary.get("available"):
        lines.extend(["", "**数据说明**", "- 当前没有可用快照，报告不会用历史记忆补齐组合数据。"])
        return "\n".join(lines)

    rejected_text = format_longterm_rejected_reasons(summary)
    if rejected_text:
        lines.extend(["", "**风控拦截**", f"- {rejected_text}。"])
    lines.extend(["", "**边界**", "- 这是模拟组合记录，不代表券商真实成交，也不会自动提交委托。"])
    return "\n".join(lines)


def _query_trade_monitor(query: str) -> str:
    date_text = _extract_query_date(query)
    try:
        return format_today_summary_text(date=date_text or None, fast=True)
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
    except Exception:
        return "投资助理运行状态生成失败；请以后续健康检查告警为准。"

def _query_watchlist_report() -> str:
    try:
        return build_watchlist_report(limit_events=80, top_n=12).get("text", "")
    except Exception:
        return "事件驱动观察池生成失败；本次不推荐个股。"

def _query_global_events() -> str:
    try:
        brief = build_global_event_brief(limit=80, min_score=45, top_n=6)
        return format_global_event_brief(brief)
    except Exception:
        return "全球事件简报生成失败；本次不推断对 A 股的影响。"

def _query_global_impact() -> str:
    try:
        return build_global_impact_brief(limit=80, min_score=45, top_n=8, use_cache=True, max_cache_minutes=60).get("text", "")
    except Exception:
        return "全球事件影响分析生成失败；本次不输出交易方向。"
def _query_weekly_report() -> str:
    try:
        return build_weekly_report(days=7).get("text", "")
    except Exception:
        return "周报生成失败；本次不输出胜率或下周结论。"


def _query_morning_brief() -> str:
    try:
        return build_morning_brief().get("text", "")
    except Exception:
        return "晨报生成失败；本次不输出开盘预判。"


def _query_closing_brief() -> str:
    try:
        return build_closing_brief().get("text", "")
    except Exception:
        return "收盘简报生成失败；本次不输出次日机会或持仓处理结论。"


def _query_intraday_outlook(query: str) -> str:
    compact = str(query or "").replace(":", "")
    slot = "1430" if "1430" in compact or "14\u70b930" in compact else "1030" if "1030" in compact or "10\u70b930" in compact else "0930"
    try:
        return str(build_intraday_outlook(slot, save=False).get("text") or "")
    except Exception as exc:
        return f"日内预测生成失败：{type(exc).__name__}。本次不输出方向或清仓结论。"

def _capability_audit_freshness(generated_at: str, *, today: date | None = None) -> tuple[bool, str]:
    raw = str(generated_at or "").strip()[:10]
    try:
        audit_day = date.fromisoformat(raw)
    except ValueError:
        return False, "审计日期无法验证，结果不能视为当前状态。"
    current_day = today or date.today()
    age_days = (current_day - audit_day).days
    if age_days == 0:
        return True, "审计结果为当日生成。"
    if age_days > 0:
        return False, f"最近一次审计距今 {age_days} 天；以下为历史检查结果，不代表当前运行状态。"
    return False, f"审计日期晚于当前日期 {abs(age_days)} 天，日期口径冲突，仅作排障参考。"


def _query_capability_audit() -> str:
    path = Path("/root/.openclaw/workspace/reports/investor_assistant_capability_audit_latest.json")
    if not path.exists():
        return "🧪 能力审计\n\n- 尚无审计结果；请等待每日 21:00 自动检查。"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "🧪 能力审计\n\n- 审计文件读取失败；当前状态不作为投资判断依据。"
    items = payload.get("items", []) or []
    blocked = [item for item in items if item.get("status") == "blocked"]
    warnings = [item for item in items if item.get("status") == "warn"]
    passed = sum(1 for item in items if item.get("status") == "ok")
    generated_at = str(payload.get("generated_at") or "").strip()
    is_fresh, freshness_note = _capability_audit_freshness(generated_at)
    lines = ["🧪 OpenClaw 能力审计", f"检查时间：{generated_at or '未知'}", "", "**结论**"]
    if not blocked and not warnings:
        scope = "当前" if is_fresh else "最近一次"
        lines.append(f"- {passed} 项检查全部通过，{scope}检查没有阻断项或警告项。")
    else:
        lines.append(f"- 通过 {passed} 项，阻断 {len(blocked)} 项，警告 {len(warnings)} 项。")
    if blocked:
        lines.extend(["", "**需要立即处理**"])
        lines.extend(f"- {_capability_item_label(item.get('name'))}" for item in blocked[:8])
    if warnings:
        lines.extend(["", "**需要观察**"])
        lines.extend(f"- {_capability_item_label(item.get('name'))}" for item in warnings[:8])
    lines.extend(["", "**新鲜度**", f"- {freshness_note}"])
    lines.extend(["", "**边界**", "- 这是系统能力检查，不代表行情数据一定可用，也不会自动下单。"])
    return "\n".join(lines)


def _query_advisor_profile(query: str) -> str:
    selected = next((name for name in ADVISOR_PROFILES if name in str(query or "")), "")
    confirmed = bool(selected and re.search(r"(?:^|\s)确认\s*$", str(query or "").strip()))
    if selected and confirmed:
        try:
            policy = confirm_advisor_profile(selected, confirmed_via="feishu_explicit_command")
        except Exception as exc:
            return f"🎚️ 风险偏好\n\n- 档案写入或回读失败（{type(exc).__name__}）；原风险政策保持不变。"
        prefix = f"已确认并回读：{selected}"
    elif selected:
        policy = {"profile_name": selected, "profile_status": "preview", **ADVISOR_PROFILES[selected]}
        prefix = f"待确认预览：{selected}"
    else:
        policy = load_advisor_policy()
        current_name = policy.get("profile_name") or "系统默认"
        status = "用户已确认" if policy.get("profile_status") == "user_confirmed" else "尚未确认"
        prefix = f"当前档案：{current_name}（{status}）"

    lines = [
        "🎚️ OpenClaw 风险偏好",
        "",
        f"**{prefix}**",
        f"- 单票预警 / 准备 / 降风险目标：{float(policy.get('single_position_alert_ratio', 0))*100:.0f}% / "
        f"{float(policy.get('single_position_prepare_ratio', 0))*100:.0f}% / "
        f"{float(policy.get('single_position_reduce_target_ratio', 0))*100:.0f}%",
        f"- 亏损仓权重复核 / 降风险目标：{float(policy.get('loss_position_review_ratio', 0))*100:.0f}% / "
        f"{float(policy.get('loss_position_reduce_target_ratio', 0))*100:.0f}%",
        f"- 累计回撤触发：普通 {float(policy.get('loss_review_drawdown_ratio', 0.05))*100:.0f}% / "
        f"严重 {float(policy.get('severe_loss_drawdown_ratio', 0.20))*100:.0f}%（严重回撤不受仓位下限限制）",
        f"- 前三持仓预警：{float(policy.get('top3_position_alert_ratio', 0))*100:.0f}%｜最低现金参考："
        f"{float(policy.get('minimum_cash_ratio', 0))*100:.0f}%",
    ]
    if selected and not confirmed:
        lines.extend(
            [
                f"- 档案说明：{ADVISOR_PROFILES[selected]['description']}。",
                "",
                f"如确认，请发送：`/风险偏好 {selected} 确认`",
                "未出现末尾“确认”时只预览，不写入。",
            ]
        )
    elif not selected:
        lines.extend(
            [
                "",
                "**可选档案**",
                *[f"- {name}：{values['description']}" for name, values in ADVISOR_PROFILES.items()],
                "",
                "先发送 `/风险偏好 稳健`、`/风险偏好 均衡` 或 `/风险偏好 进取` 查看，不会直接写入。",
            ]
        )
    lines.extend(["", "**边界**", "- 风险偏好只改变建议阈值，不会自动下单，也不会绕过实时数据与人工确认门槛。"])
    return "\n".join(lines)


def _query_live_delivery_acceptance() -> str:
    script = Path("/root/.openclaw/workspace/scripts/report_live_acceptance.py")
    try:
        result = subprocess.run(
            ["python3", str(script)],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return "📮 OpenClaw 真实推送验收\n\n- 验收工具本次不可用；不会用最近投递记录推断今日报告已经送达。"
    text = str(result.stdout or "").strip()
    if text and result.returncode in {0, 1, 2}:
        return text
    return "📮 OpenClaw 真实推送验收\n\n- 验收结果读取失败；请检查投递审计账本，不把缺失记录视为已送达。"


def _capability_item_label(name: object) -> str:
    labels = {
        "feishu_push_entry_inventory": "飞书推送入口完整性",
        "financial_news_event_push": "财经新闻与事件推送",
        "event_driven_watchlist": "事件驱动观察池",
        "global_breaking_news_radar": "全球突发事件雷达",
        "global_impact_command_center": "全球事件对A股影响",
        "holdings_account_monitor": "双账户持仓与交易接口",
        "post_market_closing_brief": "盘后收盘简报",
        "pre_market_morning_brief": "盘前晨报",
        "portfolio_risk_report": "持仓风险报告",
        "intraday_timed_alerts": "盘中定时报告",
        "post_market_review": "每日交易复盘",
        "longterm_portfolio_tracking": "长线模拟组合跟踪",
        "service_health_diagnostics": "服务健康与故障诊断",
        "operator_status_overview": "投资助理运行状态",
        "runbook_and_operator_menu": "操作菜单与故障手册",
    }
    return labels.get(str(name or ""), "其他系统能力检查")

def _help_text() -> str:
    return format_assistant_menu_text()


def _skill_request(payload: Dict) -> Dict:
    """Call the local Skill API with a bounded timeout for Feishu responses."""
    try:
        action = str(payload.get("action") or "")
        if action == "fundamental":
            timeout = 40
        elif action in {"announcements", "news_interpretation", "sector_flow"}:
            timeout = 15
        elif action == "macro":
            timeout = 18
        else:
            timeout = 25
        result = subprocess.run(
            ["python3", "/root/.openclaw/workspace/investor/skill_api.py"],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return json.loads(result.stdout or "{}") if result.returncode == 0 else {"ok": False, "error": result.stderr or "skill_failed"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _skill_code_from_query(query: str) -> Tuple[str, str]:
    """Use an explicit ticker first, then resolve one unambiguous company name."""
    matched = re.search(r"(?<!\d)([036159]\d{5}(?:\.(?:SH|SZ))?)(?!\d)", query.upper())
    if matched:
        return matched.group(1), ""
    resolution = _skill_request({"action": "resolve", "query": query})
    matches = resolution.get("matches") or []
    if len(matches) == 1:
        item = matches[0]
        return f"{item.get('code')}.{item.get('exchange')}", ""
    if matches:
        candidates = "、".join(f"{item.get('name')} {item.get('code')}" for item in matches[:5])
        return "", f"名称存在多个候选，请指定代码：{candidates}"
    return "", "未识别到证券代码或唯一公司名称，请提供 6 位代码。"


def _skill_failure(label: str, data: Dict) -> str:
    return f"{label}暂不可用；本次没有可验证数据，不会用历史记忆补齐。"


def _display_value(value: object, suffix: str = "", digits: int = 2) -> str:
    if value is None or value == "":
        return "暂无"
    try:
        return f"{float(value):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return f"{value}{suffix}"


def _format_data_time(value: object) -> str:
    raw = re.sub(r"\D", "", str(value or ""))
    if len(raw) >= 14:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}:{raw[12:14]}"
    if len(raw) >= 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return str(value or "无法确认")


def _technical_label(value: object) -> str:
    text = str(value or "").strip().lower()
    return {
        "bullish": "偏强",
        "bearish": "偏弱",
        "neutral": "震荡",
        "up": "上行",
        "down": "下行",
        "golden_cross": "金叉",
        "death_cross": "死叉",
        "none": "暂无明确交叉",
    }.get(text, str(value or "待确认"))


def _query_skill_quote(query: str) -> str:
    codes = re.findall(r"(?<!\d)([036159]\d{5}(?:\.(?:SH|SZ))?)(?!\d)", query.upper())
    if not codes:
        code, error = _skill_code_from_query(query)
        if error:
            return error
        codes = [code]
    data = _skill_request({"action": "quote", "codes": codes[:5]})
    if not data.get("ok"):
        return _skill_failure("行情数据", data)
    lines = ["📈 行情快照", ""]
    for item in data.get("quotes") or []:
        lines.append(
            f"- **{item.get('name') or item.get('code')}（{item.get('code')}）**："
            f"{_display_value(item.get('price'), digits=3)}，涨跌 {_display_value(item.get('change_pct'), '%')}；"
            f"成交额 {_display_value(item.get('turnover_yi'), '亿元')}"
        )
        lines.append(f"  数据时间：{_format_data_time(item.get('as_of'))}")
    lines.extend(["", "- 行情时间不是当前交易时段时，仅作最近快照，不作为盘中价格触发依据。"])
    return "\n".join(lines)


def _query_skill_technical(query: str) -> str:
    code, error = _skill_code_from_query(query)
    if error:
        return error
    data = _skill_request({"action": "technical", "code": code})
    if not data.get("ok"):
        return _skill_failure("技术分析数据", data)
    return "\n".join([
        f"📐 技术分析｜{data.get('code')}",
        f"数据日：{data.get('as_of') or '无法确认'}",
        "",
        "**趋势结论**",
        f"- 收盘价 {_display_value(data.get('close'), digits=3)}，趋势判断为{_technical_label(data.get('stance'))}。",
        f"- 5/10/20/60 日均线：{_display_value(data.get('ma5'))}、{_display_value(data.get('ma10'))}、{_display_value(data.get('ma20'))}、{_display_value(data.get('ma60'))}；均线信号为{_technical_label(data.get('ma_cross'))}。",
        "",
        "**动量与位置**",
        f"- RSI(14) {_display_value(data.get('rsi14'))}；MACD 交叉为{_technical_label(data.get('macd_cross'))}；KDJ 为 {_display_value(data.get('kdj_k'))}/{_display_value(data.get('kdj_d'))}/{_display_value(data.get('kdj_j'))}。",
        f"- 近 20 日支撑约 {_display_value(data.get('support'))}，阻力约 {_display_value(data.get('resistance'))}。",
        "",
        "- 技术指标只描述历史价格，不单独构成买卖建议。",
    ])


def _query_skill_sentiment(query: str) -> str:
    data = _skill_request({"action": "sentiment", "date": _extract_query_date(query)})
    if not data.get("data_ok"):
        return _skill_failure("市场情绪数据", data)
    lines = [
        "🌡️ A股市场情绪",
        f"数据日：{data.get('as_of') or '无法确认'}",
        "",
        f"- 情绪判断：{data.get('sentiment') or '待确认'}。",
        f"- 涨停 {int(data.get('limit_up', 0) or 0)} 家，跌停 {int(data.get('limit_down', 0) or 0)} 家，最高 {int(data.get('max_height', 0) or 0)} 连板。",
    ]
    for row in (data.get("ladders") or [])[:3]:
        names = "、".join(str(item.get("name")) for item in (row.get("stocks") or [])[:8])
        lines.append(f"- {row.get('height')}板: {names or '无'}")
    flow = data.get("capital_flow") or {}
    if flow.get("ok"):
        northbound = [item for item in (flow.get("northbound") or []) if item.get("direction") == "北向"]
        if northbound:
            total_net_buy = sum(float(item.get("net_buy") or 0) for item in northbound)
            breadth = "；".join(f"{item.get('board')} 上涨/下跌 {item.get('up_count')}/{item.get('down_count')} 家" for item in northbound)
            if any(abs(float(item.get("net_buy") or 0)) > 1e-9 for item in northbound):
                lines.append(f"- 北向资金合计净买入 {total_net_buy:.2f}；{breadth}。")
            else:
                lines.append(f"- 北向净买额字段未返回有效值，不按 0 处理；{breadth}。")
        top_lhb = sorted(flow.get("lhb") or [], key=lambda item: float(item.get("net_buy") or 0), reverse=True)[:3]
        if top_lhb:
            lines.append("- 龙虎榜净买额靠前：" + "；".join(f"{item.get('name')} {float(item.get('net_buy') or 0) / 100000000:.2f}亿元" for item in top_lhb) + "。")
    elif flow.get("error"):
        lines.append("- 资金流数据本次不可用，情绪判断仅依据涨跌停和连板结构。")
    lines.append("- 数据日不为今天时，仅用于历史复盘。")
    return "\n".join(lines)


def _query_skill_backtest(query: str) -> str:
    code, error = _skill_code_from_query(query)
    if error:
        return error
    data = _skill_request({"action": "backtest", "code": code, "days": 250})
    if not data.get("ok"):
        return _skill_failure("回测数据", data)
    return "\n".join([
        f"🧪 策略回测｜{data.get('code')}",
        f"区间：{_format_data_time(data.get('start'))} 至 {_format_data_time(data.get('end'))}",
        "",
        "**结果**",
        f"- 样本 {int(data.get('bars', 0) or 0)} 个交易日，信号切换 {int(data.get('signal_changes', 0) or 0)} 次。",
        f"- 总收益 {_display_value(data.get('total_return_pct'), '%')}，年化收益 {_display_value(data.get('annualized_return_pct'), '%')}。",
        f"- 最大回撤 {_display_value(abs(float(data.get('max_drawdown_pct', 0) or 0)), '%')}，夏普比率 {_display_value(data.get('sharpe'))}，历史胜率 {_display_value(data.get('win_rate_pct'), '%')}。",
        "",
        "- 回测假设：收盘产生信号、下一交易日执行，且未计手续费和滑点；结果可能明显高估真实收益。历史回测不代表未来收益，也不会触发自动交易。",
    ])


def _query_skill_financial(query: str) -> str:
    code, error = _skill_code_from_query(query)
    if error:
        return error
    data = _skill_request({"action": "financial", "code": code})
    if not data.get("ok"):
        return _skill_failure("财务数据", data)
    metrics = data.get("metrics") or {}
    return "\n".join([
        f"📑 财务概览｜{data.get('code')}",
        f"报告期：{_format_data_time(data.get('period'))}",
        "",
        "**规模与现金流**",
        f"- 营业收入 {money(metrics.get('revenue'))}；归母净利润 {money(metrics.get('net_profit'))}；经营现金流 {money(metrics.get('operating_cashflow'))}。",
        "",
        "**盈利与负债**",
        f"- ROE {_display_value(metrics.get('roe'), '%')}；毛利率 {_display_value(metrics.get('gross_margin'), '%')}；资产负债率 {_display_value(metrics.get('debt_ratio'), '%')}。",
        f"- 营收同比 {_display_value(metrics.get('revenue_growth'), '%')}；净利润同比 {_display_value(metrics.get('net_profit_growth'), '%')}。",
        "",
        "- 财务报告存在披露滞后，应结合最新公告和价格走势复核。",
    ])


def _query_skill_fundamental(query: str) -> str:
    code, error = _skill_code_from_query(query)
    if error:
        return error
    data = _skill_request({"action": "fundamental", "code": code})
    if not data.get("ok"):
        return _skill_failure("基本面分析数据", data)
    valuation = data.get("valuation") or {}
    health = data.get("financial_health") or {}
    growth = data.get("growth") or {}
    observation_labels = {
        "roe_low": "当前 ROE 偏低",
        "roe_healthy": "ROE 处于较健康水平",
        "debt_controlled": "资产负债率较低",
        "debt_high": "资产负债率偏高",
        "cashflow_covers_profit": "经营现金流能够覆盖归母净利润",
        "cashflow_weak": "经营现金流弱于利润表现",
        "growth_positive": "收入或利润保持增长",
        "growth_negative": "收入或利润出现下滑",
    }
    observations = "；".join(observation_labels.get(str(item), "存在需结合行业复核的指标") for item in (data.get("observations") or [])) or "暂未形成额外观察结论"
    return "\n".join([
        f"🔎 基本面分析｜{data.get('code')}",
        "",
        f"**估值快照｜{_format_data_time(data.get('valuation_as_of'))}**",
        f"- 价格 {_display_value(valuation.get('price'))}；市盈率(TTM) {_display_value(valuation.get('pe_ttm'))}；市净率 {_display_value(valuation.get('pb'))}；市销率 {_display_value(valuation.get('ps'))}；PEG {_display_value(valuation.get('peg'))}。",
        "",
        f"**财务质量｜{_format_data_time(data.get('financial_period'))}**",
        f"- ROE {_display_value(health.get('roe'), '%')}；资产负债率 {_display_value(health.get('debt_ratio'), '%')}；经营现金流/归母净利润 {_display_value(health.get('operating_cashflow_to_net_profit'))}。",
        f"- 营收同比 {_display_value(growth.get('revenue_growth'), '%')}；净利润同比 {_display_value(growth.get('net_profit_growth'), '%')}；毛利率 {_display_value(growth.get('gross_margin'), '%')}。",
        "",
        f"- 观察：{observations}。",
        "- 估值与财务数据日期不同，不能把财报期数据当作实时经营情况。",
    ])


def _query_skill_announcements(query: str) -> str:
    code, error = _skill_code_from_query(query)
    if error:
        return error
    data = _skill_request({"action": "announcements", "code": code, "date": _extract_query_date(query)})
    if not data.get("ok"):
        return _skill_failure("公告数据", data)
    lines = [f"📣 公司公告｜{data.get('code')}", f"检索日期：{data.get('date') or '无法确认'}", ""]
    if data.get("cache_fallback"):
        lines.append(f"- 实时公告源暂不可用，以下为同一检索日期的缓存结果（缓存时间：{data.get('fetched_at') or '无法确认'}）。")
    for item in data.get("items") or []:
        lines.append(f"- {item.get('公告类型')}: {item.get('公告标题')}\n  {item.get('网址')}")
    if not data.get("items"):
        lines.append("- 当日未检索到公告。")
    lines.append("- 公告结论以交易所原文为准。")
    return "\n".join(lines)


def _query_skill_news_interpretation(query: str) -> str:
    code, error = _skill_code_from_query(query)
    if error:
        return error
    data = _skill_request({"action": "news_interpretation", "code": code, "date": _extract_query_date(query)})
    if not data.get("ok"):
        return _skill_failure("公告解读数据", data)
    counts = data.get("counts") or {}
    sentiment_label = {
        "positive": "偏正面",
        "negative": "偏负面",
        "neutral": "中性",
    }.get(str(data.get("title_level_sentiment") or "").lower(), "中性")
    lines = [
        f"📰 公告标题初筛｜{data.get('code')}",
        f"检索日期：{data.get('date') or '无法确认'}",
        "",
        f"- 标题倾向：{sentiment_label}；正向线索 {counts.get('positive_titles', 0)} 条，负向线索 {counts.get('negative_titles', 0)} 条。",
    ]
    if data.get("cache_fallback"):
        lines.append(f"- 实时公告源暂不可用，本次复用同一检索日期的缓存（缓存时间：{data.get('fetched_at') or '无法确认'}）。")
    for fact in (data.get("facts") or [])[:8]:
        signal = fact.get("title_signals") or {}
        labels = "、".join((signal.get("positive") or []) + (signal.get("negative") or [])) or "未命中倾向词"
        lines.append(f"- 【{labels}】{fact.get('title')}\n  {fact.get('url')}")
    lines.append("- 这里只做标题级初筛；重大判断必须回到公告原文核验，不据此自动交易。")
    return "\n".join(lines)


def _query_skill_screener(query: str) -> str:
    codes = re.findall(r"(?<!\d)([036159]\d{5}(?:\.(?:SH|SZ))?)(?!\d)", query.upper())
    aliases = {
        "PE": "pe_ttm",
        "PB": "pb",
        "ROE": "roe",
        "营收增速": "revenue_growth",
        "净利增速": "net_profit_growth",
        "负债率": "debt_ratio",
        "\u0032\u0030\u65e5\u6da8\u5e45": "momentum_20d",
        "\u0036\u0030\u65e5\u6da8\u5e45": "momentum_60d",
    }
    conditions: Dict[str, Dict[str, float]] = {"min": {}, "max": {}}
    for label, metric in aliases.items():
        for operator, value in re.findall(rf"{re.escape(label)}\s*([<>])\s*(\d+(?:\.\d+)?)", query, flags=re.IGNORECASE):
            conditions["min" if operator == ">" else "max"][metric] = float(value)
    conditions = {key: value for key, value in conditions.items() if value}
    requested = set((conditions.get("min") or {})) | set((conditions.get("max") or {}))
    payload: Dict = {"action": "screener", "codes": codes[:10], "conditions": conditions}
    rank_by = (
        "value" if "\u4ef7\u503c\u56e0\u5b50" in query
        else "growth" if "\u6210\u957f\u56e0\u5b50" in query
        else "momentum" if "\u52a8\u91cf\u56e0\u5b50" in query
        else ""
    )
    if rank_by:
        payload["rank_by"] = rank_by
    if not codes:
        payload["universe"] = "mootdx"
    data = _skill_request(payload)
    if not data.get("ok"):
        if data.get("error") == "full_universe_metric_unavailable":
            metric_labels = {"pe_ttm": "市盈率", "pb": "市净率", "ps": "市销率", "peg": "PEG", "momentum_20d": "20日动量", "momentum_60d": "60日动量"}
            metrics = "、".join(metric_labels.get(str(item), "其他指标") for item in (data.get("unsupported_metrics") or []))
            return (
                f"全市场选股暂不支持：{metrics or '所选指标'}。\n"
                "当前可稳定筛选 ROE、负债率、营收、净利润、经营现金流。"
                "PE/PB/PS/PEG 与历史增速需要完整的市值和多期财务数据，当前不会用不完整数据给出结果。"
            )
        return _skill_failure("选股数据", data)
    scope = "全市场基本财务快照" if data.get("scope") == "mootdx_full_universe" else "指定标的"
    lines = [
        f"🔍 选股筛选｜{scope}",
        f"- 按用户条件执行筛选，共命中 {data.get('match_count', len(data.get('matches') or []))} 只。",
    ]
    if data.get("rank_by") == "value":
        lines.append("\u56e0\u5b50\u6392\u5e8f: \u4ef7\u503c (ROE \u9ad8\u3001PE/PB \u4f4e\u7684\u7b49\u6743\u767e\u5206\u4f4d)")
    elif data.get("rank_by") == "growth":
        lines.append("\u56e0\u5b50\u6392\u5e8f: \u6210\u957f (\u8425\u6536\u540c\u6bd4 + \u51c0\u5229\u540c\u6bd4\u7684\u7b49\u6743\u767e\u5206\u4f4d)")
    elif data.get("rank_by") == "momentum":
        lines.append("\u56e0\u5b50\u6392\u5e8f: \u52a8\u91cf (20\u65e5 + 60\u65e5\u6536\u76ca\u7387\u7684\u7b49\u6743\u767e\u5206\u4f4d)")
    if data.get("as_of"):
        lines.append(f"- 数据更新：{data.get('as_of')}。")
    coverage = data.get("metric_coverage") or {}
    total = data.get("universe_size")
    if coverage and total:
        lines.append(f"- 覆盖 {total} 只股票；其中 ROE 可用 {coverage.get('roe', 0)} 只，负债率可用 {coverage.get('debt_ratio', 0)} 只。")
    quote_coverage = data.get("quote_coverage")
    if quote_coverage and total:
        lines.append(f"- 市盈率/市净率快照覆盖 {quote_coverage}/{total} 只。")
    if data.get("momentum_coverage") and total:
        lines.append(f"- 动量数据覆盖 {data.get('momentum_coverage')}/{total} 只。")
    if data.get("growth_report_period"):
        lines.append(f"- 增长指标报告期：{data.get('growth_report_period')}。")
    if (data.get("screening_defaults") or {}).get("exclude_st"):
        lines.append("- 筛选口径：默认排除 ST 标的。")
    for item in (data.get("matches") or [])[:15]:
        metrics = item.get("metrics") or item
        selected_metrics = []
        if "pe_ttm" in requested:
            selected_metrics.append(f"市盈率 {_display_value(metrics.get('pe_ttm'))}")
        if "pb" in requested:
            selected_metrics.append(f"市净率 {_display_value(metrics.get('pb'))}")
        if "ps" in requested:
            selected_metrics.append(f"市销率 {_display_value(metrics.get('ps'))}")
        if "peg" in requested:
            selected_metrics.append(f"PEG {_display_value(metrics.get('peg'))}")
        if "revenue_growth" in requested:
            selected_metrics.append(f"营收同比 {_display_value(metrics.get('revenue_growth'), '%')}")
        if "net_profit_growth" in requested:
            selected_metrics.append(f"净利润同比 {_display_value(metrics.get('net_profit_growth'), '%')}")
        if "momentum_20d" in requested or data.get("rank_by") == "momentum":
            selected_metrics.append(f"20日动量 {_display_value(metrics.get('momentum_20d'), '%')}")
        if "momentum_60d" in requested or data.get("rank_by") == "momentum":
            selected_metrics.append(f"60日动量 {_display_value(metrics.get('momentum_60d'), '%')}")
        if metrics.get("factor_score") is not None:
            selected_metrics.append(f"综合因子 {_display_value(metrics.get('factor_score'))}")
        if selected_metrics:
            item = dict(item)
            item["name"] = f"{item.get('name') or item.get('code')}【{'、'.join(selected_metrics)}】"
        try:
            revenue = f"{float(metrics.get('revenue')) / 100000000:.2f}亿"
        except (TypeError, ValueError):
            revenue = "--"
        lines.append(f"- {item.get('name') or item.get('code')}（{item.get('code', '')}）：ROE {_display_value(metrics.get('roe'), '%')}；负债率 {_display_value(metrics.get('debt_ratio'), '%')}；营收 {revenue}。")
    remaining = max(0, int(data.get("match_count", len(data.get("matches") or []))) - 15)
    if remaining:
        lines.append(f"其他 {remaining} 只仅作候选，需结合行业、估值、走势与公告复核。")
    rejected_count = len(data.get("rejected") or [])
    if rejected_count:
        lines.append(f"- 另有 {rejected_count} 只未满足筛选条件，不展示内部过滤代码。")
    if data.get("limitations"):
        lines.append("- 数据限制：部分估值、增长或动量字段覆盖不足时，结果只基于可用字段，不作缺失值推断。")
    lines.append("- 筛选结果只是候选池，仍需结合行业、估值、走势和公告人工复核。")
    return "\n".join(lines)


def _query_skill_macro(query: str) -> str:
    data = _skill_request({"action": "macro"})
    if not data.get("ok"):
        return _skill_failure("宏观数据", data)
    source_labels = {
        "cpi": "居民消费价格",
        "ppi": "工业生产者价格",
        "gdp": "国内生产总值",
        "money_supply": "货币供应",
        "lpr": "贷款市场报价利率",
        "retail_sales": "社会消费品零售",
    }
    field_preferences = {
        "retail_sales": ("当月", "同比增长", "累计"),
        "money_supply": ("货币和准货币(M2)-数量(亿元)", "货币和准货币(M2)-同比增长", "货币(M1)-数量(亿元)"),
        "cpi": ("全国-同比增长", "全国-环比增长", "全国-当月"),
        "ppi": ("当月同比增长", "累计同比增长"),
        "gdp": ("国内生产总值-绝对值", "国内生产总值-同比增长"),
        "lpr": ("LPR1Y", "LPR5Y"),
    }
    display_names = {
        "货币和准货币(M2)-数量(亿元)": "M2余额（亿元）",
        "货币和准货币(M2)-同比增长": "M2同比",
        "货币(M1)-数量(亿元)": "M1余额（亿元）",
        "当月": "当月值",
        "累计": "累计值",
        "同比增长": "同比",
        "环比增长": "环比",
    }

    def format_macro_value(key: str, value: object) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if "增长" in key or key in {"LPR1Y", "LPR5Y"}:
            return f"{number:.2f}".rstrip("0").rstrip(".") + "%"
        return f"{number:,.2f}".rstrip("0").rstrip(".")

    lines = ["🌏 中国宏观数据快照", ""]
    for name, row in (data.get("data") or {}).items():
        label = source_labels.get(str(name), "其他宏观序列")
        if isinstance(row, dict):
            period_key = next((key for key in ("月份", "季度", "报告期", "日期") if row.get(key) not in (None, "")), "")
            period = str(row.get(period_key) or "报告期未知")
            preferred = [key for key in field_preferences.get(str(name), ()) if row.get(key) not in (None, "")]
            if not preferred:
                preferred = [key for key in row if key != period_key and row.get(key) not in (None, "")][:3]
            details = "；".join(
                f"{display_names.get(key, key)} {format_macro_value(key, row.get(key))}"
                for key in preferred[:3]
            )
            lines.append(f"- **{label}｜{period}**：{details or '暂无有效值'}。")
        else:
            lines.append(f"- **{label}**：{row}。")
    if data.get("stale_sources"):
        stale = "、".join(source_labels.get(str(name), "其他序列") for name in data.get("stale_sources"))
        lines.append(f"- 已排除超过 120 天未更新的序列：{stale}。")
    lines.append("- 各指标发布日期不同，必须以每一行的报告期判断新鲜度，不把旧月份数据当作当前状态。")
    return "\n".join(lines)


def _query_skill_overview(query: str) -> str:
    code, error = _skill_code_from_query(query)
    if error:
        return error
    quote_data = _skill_request({"action": "quote", "codes": [code]})
    technical = _skill_request({"action": "technical", "code": code})
    fundamental = _skill_request({"action": "fundamental", "code": code})
    notice = _skill_request({"action": "news_interpretation", "code": code, "date": _extract_query_date(query)})
    quote = (quote_data.get("quotes") or [{}])[0]
    lines = [f"🧭 个股综合分析｜{quote.get('name') or code}（{code}）", ""]
    if quote_data.get("ok"):
        lines.append(f"- **行情｜{_format_data_time(quote.get('as_of'))}**：价格 {_display_value(quote.get('price'), digits=3)}，涨跌 {_display_value(quote.get('change_pct'), '%')}，成交额 {_display_value(quote.get('turnover_yi'), '亿元')}。")
    else:
        lines.append("- **行情**：本次不可用，不判断当前价格强弱。")
    if technical.get("ok"):
        lines.append(f"- **技术｜{_format_data_time(technical.get('as_of'))}**：趋势{_technical_label(technical.get('stance'))}；5/20 日均线 {_display_value(technical.get('ma5'))}/{_display_value(technical.get('ma20'))}；RSI {_display_value(technical.get('rsi14'))}；MACD {_technical_label(technical.get('macd_cross'))}。")
    else:
        lines.append("- **技术**：本次不可用，不补齐趋势判断。")
    if fundamental.get("ok"):
        valuation = fundamental.get("valuation") or {}
        health = fundamental.get("financial_health") or {}
        lines.append(f"- **基本面｜财报 {_format_data_time(fundamental.get('financial_period'))}**：市盈率 {_display_value(valuation.get('pe_ttm'))}，市净率 {_display_value(valuation.get('pb'))}，ROE {_display_value(health.get('roe'), '%')}，负债率 {_display_value(health.get('debt_ratio'), '%')}。")
    else:
        lines.append("- **基本面**：本次不可用，不作估值结论。")
    if notice.get("ok"):
        counts = notice.get("counts") or {}
        notice_label = {"positive": "偏正面", "negative": "偏负面", "neutral": "中性"}.get(str(notice.get("title_level_sentiment") or "").lower(), "中性")
        lines.append(f"- **公告初筛｜{_format_data_time(notice.get('date'))}**：{notice_label}；正向线索 {counts.get('positive_titles', 0)} 条，负向线索 {counts.get('negative_titles', 0)} 条。")
    else:
        lines.append("- **公告**：本次不可用，需手动核验交易所披露。")
    lines.append("- 各数据模块日期可能不同；以上仅为条件分析，不构成自动交易指令。")
    return "\n".join(lines)


def _query_skill_sector_flow(query: str) -> str:
    data = _skill_request({"action": "sector_flow", "limit": 10})
    if not data.get("ok"):
        return _skill_failure("板块数据", data)
    market_date = str(data.get("market_date") or "无法确认")
    retrieved_at = _format_data_time(data.get("retrieved_at"))
    date_label = "数据交易日（接口无时间戳，按交易日历推定）" if data.get("market_date_estimated") else "数据交易日"
    lines = [
        "🔥 概念板块强弱",
        f"{date_label}：{market_date}｜抓取时间：{retrieved_at}",
        "",
    ]
    if data.get("cache_fallback"):
        lines.append("- 实时板块源暂不可用，以下为最近一次成功缓存；不把它当作当前交易时点排名。")
    for index, item in enumerate(data.get("sectors") or [], 1):
        lines.append(f"{index}. {item.get('name')}：涨跌 {float(item.get('change_pct') or 0):+.2f}%，主力净流入 {float(item.get('main_net_inflow_yi') or 0):+.2f} 亿元。")
    lines.append("- 非交易时段获取时，板块数值可能是最近交易日快照；排名只用于发现主题，推荐个股前仍需核验概念归属与 20/60 日动量。")
    return "\n".join(lines)

def handle_feishu_query(query_text: str) -> str:
    query = str(query_text or "").strip()
    if not query:
        return _help_text()
    intent = _normalize_intent(query)
    if intent == "help":
        return _help_text()
    if intent == "skill_quote":
        return _query_skill_quote(query)
    if intent == "skill_technical":
        return _query_skill_technical(query)
    if intent == "skill_sentiment":
        return _query_skill_sentiment(query)
    if intent == "skill_backtest":
        return _query_skill_backtest(query)
    if intent == "skill_financial":
        return _query_skill_financial(query)
    if intent == "skill_fundamental":
        return _query_skill_fundamental(query)
    if intent == "skill_announcements":
        return _query_skill_announcements(query)
    if intent == "skill_news_interpretation":
        return _query_skill_news_interpretation(query)
    if intent == "skill_screener":
        return _query_skill_screener(query)
    if intent == "skill_macro":
        return _query_skill_macro(query)
    if intent == "skill_overview":
        return _query_skill_overview(query)
    if intent == "skill_sector_flow":
        return _query_skill_sector_flow(query)
    if intent == "assistant_status":
        return _query_assistant_status()
    if intent == "advisor_brief":
        from domain.services.advisor_brief_service import build_advisor_brief

        return build_advisor_brief().get("text", "投顾总览生成失败；本次不输出账户或交易结论。")
    if intent == "advisor_profile":
        return _query_advisor_profile(query)
    if intent == "delivery_audit":
        return format_delivery_audit_text()
    if intent == "live_delivery_acceptance":
        return _query_live_delivery_acceptance()
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
    if intent == "intraday_outlook":
        return _query_intraday_outlook(query)

    if intent == "strategy_control":
        return handle_strategy_control_query(query)
    if intent == "position_monitor_exemptions":
        return handle_position_monitor_exemptions_query(query)
    if intent == "t_monitor":
        return handle_t_monitor_query(query)

    # 分析类查询（不需要 qmt2http token）
    if intent == "predictions":
        return _query_predictions()
    if intent == "risk":
        return _query_risk()
    if intent == "decision_monitor":
        return format_decision_monitor_text(slot="Feishu 查询")
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
    accounts = ["guojin", "dongguan"] if account == "all" else [account]
    if intent == "health":
        return _query_unified_health(accounts, token)
    if not token:
        return "未找到 QMT2HTTP_API_TOKEN，无法查询实盘接口\n\n💡 试试分析类查询: 最近预测 / 风险敞口 / 策略表现"
    lines: List[str] = []
    if intent == "summary":
        for current in accounts:
            lines.append(_query_health(current, token))
            lines.append(_query_endpoint(current, "positions", token))
            lines.append(_query_endpoint(current, "orders", token))
            lines.append(_query_endpoint(current, "trades", token))
        return "\n\n".join(lines)
    if intent in {"positions", "orders", "trades"}:
        return "\n\n".join(_query_endpoint(current, intent, token) for current in accounts)
    if intent == "logs":
        days = _extract_days(query)
        return "\n\n".join(_query_logs(current, token, days) for current in accounts)
    return _help_text()

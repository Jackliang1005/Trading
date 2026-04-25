#!/usr/bin/env python3
"""Feishu query service for live qmt2http account data."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from domain.services.live_monitor_view_service import format_today_summary_text
from workflows.scheduled_briefings import run_scheduled_briefing


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


def _http_get(base_url: str, path: str, token: str, timeout: float = 15.0) -> Dict:
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
        result = _http_get(base_url, f"/api/trade/log?{query}", token)
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
    if "帮助" in query or "help" in query.lower():
        return "help"
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
        return f"{alias} {endpoint} 请求失败: {result.get('error', 'unknown_error')}"
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
    parts = [f"{alias} 最近{len(rows)}天交易日志"]
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
    """查询当前风险敞口。"""
    from datetime import datetime

    import db as db_mod

    # 读取最新 portfolio snapshot
    portfolio = db_mod.get_latest_portfolio_snapshot(account_scope="combined")
    positions = []
    total_asset = 0.0
    if portfolio:
        data = portfolio.get("data", {}) or {}
        positions = data.get("qmt_positions", data.get("positions", [])) or []
        account = data.get("qmt_account", {}) or {}
        total_asset = float(account.get("total_asset", 0) or 0)

    lines = ["⚠️ 风险概览", f"数据时间: {portfolio.get('as_of_date', 'N/A') if portfolio else 'N/A'}", ""]

    if not positions:
        lines.append("无持仓数据")
        return "\n".join(lines)

    # 计算集中度
    total_mv = sum(float(pos.get("market_value", 0) or 0) for pos in positions)
    effective_total = total_asset or total_mv
    if effective_total > 0:
        lines.append(f"总资产: {effective_total:,.0f}")
        lines.append(f"持仓市值: {total_mv:,.0f}")
        lines.append(f"现金占比: {(1 - total_mv/effective_total)*100:.0f}%" if effective_total else "")
        lines.append("")

        # Top 5 集中度
        sorted_positions = sorted(
            positions,
            key=lambda p: float(p.get("market_value", 0) or 0),
            reverse=True,
        )
        lines.append("仓位集中度 TOP5:")
        for i, pos in enumerate(sorted_positions[:5], 1):
            code = str(pos.get("stock_code", pos.get("code", "")) or "")[:12]
            name = str(pos.get("stock_name", pos.get("name", "")) or "")[:8]
            mv = float(pos.get("market_value", 0) or 0)
            ratio = mv / effective_total * 100
            flag = " 🔴" if ratio > 30 else " 🟡" if ratio > 20 else ""
            lines.append(f"  #{i} {code} {name}: {mv:,.0f} ({ratio:.1f}%){flag}")

        # 盈亏汇总
        total_pnl = sum(
            float(pos.get("unrealized_pnl", pos.get("profit_loss", 0)) or 0)
            for pos in positions
        )
        winners = [p for p in positions if float(p.get("unrealized_pnl", p.get("profit_loss", 0)) or 0) > 0]
        losers = [p for p in positions if float(p.get("unrealized_pnl", p.get("profit_loss", 0)) or 0) < 0]
        lines.append(f"\n未实现盈亏: {total_pnl:+,.0f}")
        lines.append(f"盈利: {len(winners)}只 | 亏损: {len(losers)}只")

    return "\n".join(lines)


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


def _help_text() -> str:
    return (
        "🦞 Investor 交易助手\n\n"
        "**实盘查询**\n"
        "- 国金/东莞持仓\n"
        "- 双账户成交/委托\n"
        "- 国金健康状态\n"
        "- 东莞最近5天日志\n\n"
        "**分析查询**（新增）\n"
        "- 最近预测/胜率\n"
        "- 当前风险敞口\n"
        "- 今日交易摘要/复盘\n"
        "- 交易监控（候选/买入/对账）\n"
        "- 东莞策略日志快照（NH/MIX）\n"
        "- 国金ETF 13:20/14:20 简报\n"
        "- 当前策略权重/表现\n\n"
        "**快捷指令**\n"
        "/持仓 /成交 /账户 /摘要 /监控 /候选 /买入 /日志 /预测 /风险 /策略 /帮助\n"
        "示例: 国金ETF 13:20 / 国金ETF 14:20"
    )


def handle_feishu_query(query_text: str) -> str:
    query = str(query_text or "").strip()
    if not query:
        return _help_text()
    intent = _normalize_intent(query)
    if intent == "help":
        return _help_text()

    # 分析类查询（不需要 qmt2http token）
    if intent == "predictions":
        return _query_predictions()
    if intent == "risk":
        return _query_risk()
    if intent == "reflection":
        return _query_reflection()
    if intent == "strategy":
        return _query_strategy()
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

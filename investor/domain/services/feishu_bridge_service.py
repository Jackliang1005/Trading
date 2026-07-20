#!/usr/bin/env python3
"""Feishu plugin bridge service (no direct Feishu OpenAPI calls)."""

from __future__ import annotations

import json
import re
from typing import Any, Dict

from app.feishu_card_builder import build_briefing_card
from domain.services.feishu_query_service import handle_feishu_query
from domain.services.report_style_service import build_report_card, report_quality_issues
from domain.services.live_monitor_view_service import get_today_summary
from domain.services.longterm_portfolio_service import (
    build_longterm_snapshot_text,
    format_longterm_rejected_reasons,
    load_longterm_snapshot,
    summarize_longterm_snapshot,
)

INVESTOR_PATTERNS = (
    r"持仓|委托|成交|资产|日志|健康|运行状态|runtime|qmt|qmt2http",
    r"国金|东莞|双账户|账户",
    r"交易监控|today-summary|today-account|today-candidates|today-buys|监控|候选|买入|对账|交易建议|持仓建议|/建议|行情|技术分析|技术面|MACD|KDJ|RSI|布林|涨停|连板|情绪|估值|PE|PB|PEG|财务|基本面|公告|解读|筛选|选股|宏观|CPI|PPI|M2|社融|LPR|社零",
    r"today-account|today-summary|monitor-trading|runtime-check|fix-task",
    # 分析类查询 — 纯本地数据，不需要 qmt2http token
    r"预测|胜率|准确率|回测",
    r"风险|敞口|集中度|回撤|仓位",
    r"反思|复盘|摘要|简报",
    r"策略|权重|进化|规则",
    r"长线|模拟盘|组合|调仓计划",
    r"帮助|help",
)
LLM_PATTERNS = (
    r"分析|解读|怎么看|判断|建议|原因|为什么|优化",
    r"写|润色|总结|翻译|生成",
    r"怎么|如何|什么|哪些|能否",
)
LONGTERM_QUERY_RE = re.compile(r"长线|模拟盘|组合|调仓计划", re.IGNORECASE)
TRADE_SUMMARY_QUERY_RE = re.compile(
    r"交易监控|交易摘要|today-summary|today-account|today-candidates|today-buys|候选|买入|对账",
    re.IGNORECASE,
)
BRIEFING_TITLE_RE = re.compile(r"^⏰\s*(?P<slot>\d{2}:\d{2}|\d{4})\s+(?P<title>.+?)\s+\[(?P<date>\d{4}-\d{2}-\d{2})\]")
DATE_ISO_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
DATE_COMPACT_RE = re.compile(r"\b(20\d{6})\b")
BRIDGE_SCHEMA_VERSION = "v1"


def _unified_reply(reply: str) -> tuple[str, Dict[str, Any]]:
    """Apply the same visible-content gate and card renderer as every other report path."""
    text = str(reply or "").strip()
    issues = report_quality_issues(text)
    if issues:
        text = "正常查询结果未通过报告质量检查，原始正文未发送。\n" + "\n".join(f"- {item}" for item in issues)
    return text, build_report_card(text, template="orange" if issues else "blue")


def _build_longterm_portfolio_payload() -> Dict[str, Any]:
    summary = summarize_longterm_snapshot(load_longterm_snapshot())
    return {
        "summary": summary,
        "text": build_longterm_snapshot_text(summary),
    }


def _build_longterm_card(longterm_payload: Dict[str, Any]) -> Dict[str, Any]:
    summary = (longterm_payload or {}).get("summary", {}) or {}
    text = str((longterm_payload or {}).get("text", "") or "暂无可用的长线模拟组合快照。")
    sections = [
        {"label": "长线组合（模拟盘）", "text": text, "highlight": True},
    ]
    if summary.get("available"):
        top_positions = summary.get("top_positions", []) or []
        if top_positions:
            top_text = "、".join(
                f"{str(item.get('name') or item.get('code', ''))}（{float(item.get('weight', 0) or 0):.1%}）"
                for item in top_positions[:5]
            )
            sections.append({"label": "主要持仓", "text": top_text})
        rejected_text = format_longterm_rejected_reasons(summary)
        if rejected_text:
            sections.append(
                {
                    "label": "风控拦截",
                    "text": rejected_text,
                }
            )
    sections.append({"label": "边界", "text": "模拟组合记录，不代表券商真实成交，也不会自动提交委托。"})
    card_wrapper = build_briefing_card(title="🧭 长线组合聚合", sections=sections)
    return card_wrapper.get("card", {})


def _extract_query_date(query: str) -> str:
    text = str(query or "").strip()
    m = DATE_ISO_RE.search(text)
    if m:
        return m.group(1)
    m = DATE_COMPACT_RE.search(text)
    if m:
        raw = m.group(1)
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return ""


def _build_trade_summary_payload(query: str) -> Dict[str, Any]:
    date_text = _extract_query_date(query)
    payload = get_today_summary(date=date_text or None)
    return {
        "requested_date": payload.get("requested_date", ""),
        "log_date": payload.get("log_date", ""),
        "strategy": payload.get("strategy", ""),
        "candidates_count": len(payload.get("final_candidates", []) or []),
        "submitted_count": len(payload.get("submitted_buys", []) or []),
        "filled_count": len(payload.get("filled_buys", []) or []),
        "skipped_count": len(payload.get("skipped_buys", []) or []),
        "trade_reconciliation_status": (payload.get("trade_reconciliation", {}) or {}).get("status", ""),
        "accounts_overview": payload.get("accounts_overview", {}) or {},
        "incidents": payload.get("trade_incidents", []) or [],
    }


def _build_trade_summary_card(trade_payload: Dict[str, Any]) -> Dict[str, Any]:
    overview = (trade_payload or {}).get("accounts_overview", {}) or {}
    sections = [
        {
            "label": "交易监控汇总",
            "text": (
                f"date={trade_payload.get('requested_date', '')} "
                f"log={trade_payload.get('log_date', '')} "
                f"strategy={trade_payload.get('strategy', '') or 'N/A'}"
            ),
            "highlight": True,
        },
        {
            "label": "候选/提交/成交/过滤",
            "text": (
                f"{int(trade_payload.get('candidates_count', 0) or 0)} / "
                f"{int(trade_payload.get('submitted_count', 0) or 0)} / "
                f"{int(trade_payload.get('filled_count', 0) or 0)} / "
                f"{int(trade_payload.get('skipped_count', 0) or 0)}"
            ),
        },
        {
            "label": "对账状态",
            "text": str(trade_payload.get("trade_reconciliation_status", "") or "unknown"),
        },
        {
            "label": "账户可达/持仓/委托/成交",
            "text": (
                f"{int(overview.get('reachable_server_count', 0) or 0)}/"
                f"{int(overview.get('server_count', 0) or 0)} | "
                f"pos={int(overview.get('total_positions_count', 0) or 0)} "
                f"ord={int(overview.get('total_orders_count', 0) or 0)} "
                f"trd={int(overview.get('total_trades_count', 0) or 0)}"
            ),
        },
    ]
    incidents = (trade_payload or {}).get("incidents", []) or []
    if incidents:
        sections.append(
            {
                "label": "告警",
                "text": " | ".join(
                    f"{str(item.get('severity', ''))}/{str(item.get('kind', ''))}"
                    for item in incidents[:4]
                ),
            }
        )
    card_wrapper = build_briefing_card(title="📡 交易监控聚合", sections=sections)
    return card_wrapper.get("card", {})


def _parse_key_value_line(line: str) -> tuple[str, str]:
    text = str(line or "").strip()
    if not text:
        return "", ""
    for sep in ("：", ":"):
        if sep in text:
            left, right = text.split(sep, 1)
            return left.strip(), right.strip()
    return "", text


def _build_scheduled_briefing_payload_from_reply(query: str, reply: str) -> Dict[str, Any] | None:
    lines = [str(item).strip() for item in str(reply or "").splitlines() if str(item).strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.startswith("⏰"):
        return None
    parsed = BRIEFING_TITLE_RE.match(first)
    payload: Dict[str, Any] = {
        "query": str(query or ""),
        "header": first,
        "slot": "",
        "title": first,
        "as_of": "",
        "sections": [],
        "error_lines": [],
    }
    if parsed:
        payload["slot"] = parsed.group("slot")
        payload["title"] = parsed.group("title")
        payload["as_of"] = parsed.group("date")

    sections = []
    for line in lines[1:]:
        if line.startswith("- "):
            sections.append({"label": "明细", "text": line[2:].strip()})
            continue
        label, text = _parse_key_value_line(line)
        if label:
            sections.append({"label": label, "text": text})
        else:
            sections.append({"label": "补充", "text": text})
        if any(key in line for key in ("失败", "error", "异常", "unreachable")):
            payload["error_lines"].append(line)

    payload["sections"] = sections
    return payload


def _build_scheduled_briefing_card(briefing_payload: Dict[str, Any]) -> Dict[str, Any]:
    title = str(briefing_payload.get("title", "") or "定时简报")
    slot = str(briefing_payload.get("slot", "") or "")
    as_of = str(briefing_payload.get("as_of", "") or "")
    header = f"{slot} {title}".strip() if slot else title
    if as_of:
        header = f"{header} [{as_of}]"

    rows = briefing_payload.get("sections", []) or []
    card_sections = []
    for item in rows[:12]:
        card_sections.append(
            {
                "label": str(item.get("label", "") or "补充"),
                "text": str(item.get("text", "") or ""),
            }
        )

    if not card_sections:
        card_sections.append({"label": "内容", "text": str(briefing_payload.get("header", "") or "")})

    card_wrapper = build_briefing_card(title=f"⏰ {header}".strip(), sections=card_sections)
    return card_wrapper.get("card", {})


def _compose_aggregates_payload(
    longterm_payload: Dict[str, Any],
    trade_payload: Dict[str, Any] | None,
    briefing_payload: Dict[str, Any] | None,
) -> Dict[str, Any]:
    return {
        "schema_version": BRIDGE_SCHEMA_VERSION,
        "aggregates": {
            "longterm_portfolio": longterm_payload,
            "trade_summary": trade_payload or {},
            "scheduled_briefing": briefing_payload or {},
        },
    }


def classify_intent(query: str) -> Dict[str, Any]:
    text = str(query or "").strip()
    lowered = text.lower()
    report_terms = (
        "菜单", "晨报", "早报", "周报", "收盘简报", "全球事件", "全球影响",
        "关注池", "能力审计", "投资助理状态", "板块涨幅", "板块强弱",
        "推送状态", "投递状态", "推送验收", "投递验收",
    )
    skill_terms = ("行情", "报价", "分析", "怎么样", "技术分析", "技术面", "MACD", "KDJ", "RSI", "布林", "估值", "PE", "PB", "PEG", "财务", "基本面", "公告", "解读", "筛选", "选股", "回测", "涨停", "连板", "情绪", "宏观", "CPI", "PPI", "M2", "社融", "LPR", "社零")
    if text in {"/交易建议", "/建议", "交易建议", "持仓建议"} or re.match(r"^/(行情|报价|技术|情绪|涨停|连板|回测|backtest|财务|基本面|公告|解读|筛选|选股)\b", text) or any(term in text for term in report_terms) or any(term in text for term in skill_terms):
        return {"intent": "investor", "confidence": 1.0, "reason": "direct_decision_monitor_command"}
    investor_hits = [
        pat for pat in INVESTOR_PATTERNS if re.search(pat, text, re.IGNORECASE)
    ]
    llm_hits = [
        pat for pat in LLM_PATTERNS if re.search(pat, text, re.IGNORECASE)
    ]

    if investor_hits and llm_hits:
        return {"intent": "hybrid", "confidence": 0.95, "reason": "同时包含实盘数据与分析诉求"}
    if investor_hits:
        return {"intent": "investor", "confidence": 0.98, "reason": "命中实盘交易/账户关键词"}
    if llm_hits:
        return {"intent": "llm", "confidence": 0.9, "reason": "命中分析/建议类关键词"}
    if any(key in lowered for key in ("今天", "实时", "最新")):
        return {"intent": "investor", "confidence": 0.75, "reason": "时效性请求默认走investor"}
    return {"intent": "llm", "confidence": 0.6, "reason": "未命中实盘关键词"}


def _resolve_intent(event_payload: Dict[str, Any], query: str) -> Dict[str, Any]:
    forced = str((event_payload or {}).get("intent", "") or "").strip().lower()
    if forced in {"investor", "llm", "hybrid"}:
        return {"intent": forced, "confidence": 1.0, "reason": "caller_forced"}
    return classify_intent(query)


def _build_llm_task(query: str, investor_reply: str = "") -> Dict[str, Any]:
    if investor_reply:
        prompt = (
            "你是投资分析助手。必须基于给定实盘数据回答，不得编造账户数据。\n"
            f"用户问题: {query}\n"
            f"实盘数据:\n{investor_reply}\n"
            "请给出简洁分析与可执行建议。"
        )
    else:
        prompt = (
            "你是投资分析助手。根据用户问题给出简洁、可执行回答。\n"
            f"用户问题: {query}"
        )
    return {"model_task": "analysis", "prompt": prompt}


def _parse_message_content(raw: Any) -> str:
    if isinstance(raw, dict):
        for key in ("text", "content", "query", "message"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text:
        return ""
    if text.startswith("{") and text.endswith("}"):
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return str(payload.get("text", "") or "").strip()
        except Exception:
            return text
    return text


def extract_query_from_event(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("query", "text", "message", "input"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    event = payload.get("event")
    if isinstance(event, dict):
        message = event.get("message")
        if isinstance(message, dict):
            content = _parse_message_content(message.get("content"))
            if content:
                return content
            for key in ("text", "message"):
                value = message.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        body = event.get("body")
        if isinstance(body, dict):
            content = _parse_message_content(body.get("content"))
            if content:
                return content
    return ""


def build_bridge_response(event_payload: Dict[str, Any]) -> Dict[str, Any]:
    challenge = str(event_payload.get("challenge", "") or "").strip() if isinstance(event_payload, dict) else ""
    if challenge:
        return {"ok": True, "challenge": challenge, "channel": "feishu-plugin"}

    query = extract_query_from_event(event_payload or {})
    if not query:
        return {
            "ok": False,
            "error": "missing_query",
            "message": "未识别到消息文本，支持字段: query/text/event.message.content",
            "channel": "feishu-plugin",
        }

    decision = _resolve_intent(event_payload or {}, query)
    intent = str(decision.get("intent", "llm"))

    if intent == "investor":
        investor_reply, unified_card = _unified_reply(handle_feishu_query(query))
        longterm_payload = _build_longterm_portfolio_payload()
        trade_payload: Dict[str, Any] | None = None
        briefing_payload = _build_scheduled_briefing_payload_from_reply(query=query, reply=investor_reply)
        if TRADE_SUMMARY_QUERY_RE.search(query):
            try:
                trade_payload = _build_trade_summary_payload(query)
            except Exception:
                trade_payload = None
        response = {
            "ok": True,
            "query": query,
            "intent": intent,
            "route": "investor_only",
            "classifier": decision,
            "reply": investor_reply,
            "longterm_portfolio": longterm_payload,
            "channel": "feishu-plugin",
            "card": unified_card,
        }
        response.update(
            _compose_aggregates_payload(
                longterm_payload=longterm_payload,
                trade_payload=trade_payload,
                briefing_payload=briefing_payload,
            )
        )
        if trade_payload:
            response["trade_summary"] = trade_payload
        if briefing_payload:
            response["scheduled_briefing"] = briefing_payload
        return response

    if intent == "hybrid":
        investor_reply, unified_card = _unified_reply(handle_feishu_query(query))
        longterm_payload = _build_longterm_portfolio_payload()
        trade_payload: Dict[str, Any] | None = None
        briefing_payload = _build_scheduled_briefing_payload_from_reply(query=query, reply=investor_reply)
        if TRADE_SUMMARY_QUERY_RE.search(query):
            try:
                trade_payload = _build_trade_summary_payload(query)
            except Exception:
                trade_payload = None
        response = {
            "ok": True,
            "query": query,
            "intent": intent,
            "route": "investor_then_llm",
            "classifier": decision,
            "investor_reply": investor_reply,
            "llm_task": _build_llm_task(query, investor_reply=investor_reply),
            # fallback reply for clients that do not run the second-stage LLM yet
            "reply": investor_reply,
            "longterm_portfolio": longterm_payload,
            "channel": "feishu-plugin",
            "card": unified_card,
        }
        response.update(
            _compose_aggregates_payload(
                longterm_payload=longterm_payload,
                trade_payload=trade_payload,
                briefing_payload=briefing_payload,
            )
        )
        if trade_payload:
            response["trade_summary"] = trade_payload
        if briefing_payload:
            response["scheduled_briefing"] = briefing_payload
        return response

    return {
        "ok": True,
        "query": query,
        "intent": "llm",
        "route": "llm_only",
        "classifier": decision,
        "llm_task": _build_llm_task(query),
        "reply": "该请求已路由到LLM分析通道",
        "schema_version": BRIDGE_SCHEMA_VERSION,
        "aggregates": {
            "longterm_portfolio": {},
            "trade_summary": {},
            "scheduled_briefing": {},
        },
        "channel": "feishu-plugin",
    }


def build_feishu_webhook_response(bridge_result: Dict[str, Any]) -> Dict[str, Any]:
    """将 bridge_result 转换为飞书 Webhook 回复格式。

    飞书要求回复格式：
    - 文本: {"msg_type": "text", "content": {"text": "..."}}
    - 卡片: {"msg_type": "interactive", "card": {...}}

    当 bridge 返回了 card 字段时使用 interactive 格式，否则用 text。
    """
    if not bridge_result.get("ok"):
        return {
            "msg_type": "text",
            "content": {"text": str(bridge_result.get("message", bridge_result.get("error", "处理失败")))},
        }

    # 如果 bridge 已经返回了 Feishu 格式的 card
    if bridge_result.get("card"):
        card_payload = bridge_result["card"]
        if isinstance(card_payload, dict) and card_payload.get("msg_type") == "interactive" and card_payload.get("card"):
            card_payload = card_payload.get("card")
        return {
            "msg_type": "interactive",
            "card": card_payload,
        }

    # 默认文本回复
    reply = str(bridge_result.get("reply", "") or "")
    if not reply:
        reply = "已处理（无额外数据）"

    return {
        "msg_type": "text",
        "content": {"text": reply},
    }


def extract_query_from_feishu_v2_event(event_payload: Dict[str, Any]) -> str:
    """从飞书 v2 事件格式中提取消息文本。

    飞书 v2 事件格式:
    {
      "schema": "2.0",
      "header": {"event_type": "im.message.receive_v1", ...},
      "event": {
        "message": {
          "content": "{\"text\":\"用户消息\"}"
        }
      }
    }
    """
    event = event_payload.get("event", {}) if isinstance(event_payload, dict) else {}
    if isinstance(event, dict):
        # v2 format: message is inside event
        msg = event.get("message", {})
        if isinstance(msg, dict):
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        text = parsed.get("text", "")
                        if text:
                            return str(text).strip()
                except (json.JSONDecodeError, TypeError):
                    return content.strip()
            return ""
        # Alternative: event.body
        body = event.get("body", {})
        if isinstance(body, dict):
            text = body.get("text", body.get("content", ""))
            if isinstance(text, str):
                return text.strip()

    # Fall back to generic extraction
    return extract_query_from_event(event_payload)

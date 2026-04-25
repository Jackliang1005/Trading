#!/usr/bin/env python3
"""Feishu plugin bridge service (no direct Feishu OpenAPI calls)."""

from __future__ import annotations

import json
import re
from typing import Any, Dict

from domain.services.feishu_query_service import handle_feishu_query

INVESTOR_PATTERNS = (
    r"持仓|委托|成交|资产|日志|健康|运行状态|runtime|qmt|qmt2http",
    r"国金|东莞|双账户|账户",
    r"today-account|today-summary|monitor-trading|runtime-check|fix-task",
    # 分析类查询 — 纯本地数据，不需要 qmt2http token
    r"预测|胜率|准确率|回测",
    r"风险|敞口|集中度|回撤|仓位",
    r"反思|复盘|摘要|简报",
    r"策略|权重|进化|规则",
    r"帮助|help",
)
LLM_PATTERNS = (
    r"分析|解读|怎么看|判断|建议|原因|为什么|优化",
    r"写|润色|总结|翻译|生成",
    r"怎么|如何|什么|哪些|能否",
)


def classify_intent(query: str) -> Dict[str, Any]:
    text = str(query or "").strip()
    lowered = text.lower()
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
        investor_reply = handle_feishu_query(query)
        return {
            "ok": True,
            "query": query,
            "intent": intent,
            "route": "investor_only",
            "classifier": decision,
            "reply": investor_reply,
            "channel": "feishu-plugin",
        }

    if intent == "hybrid":
        investor_reply = handle_feishu_query(query)
        return {
            "ok": True,
            "query": query,
            "intent": intent,
            "route": "investor_then_llm",
            "classifier": decision,
            "investor_reply": investor_reply,
            "llm_task": _build_llm_task(query, investor_reply=investor_reply),
            # fallback reply for clients that do not run the second-stage LLM yet
            "reply": investor_reply,
            "channel": "feishu-plugin",
        }

    return {
        "ok": True,
        "query": query,
        "intent": "llm",
        "route": "llm_only",
        "classifier": decision,
        "llm_task": _build_llm_task(query),
        "reply": "该请求已路由到LLM分析通道",
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
        return {
            "msg_type": "interactive",
            "card": bridge_result["card"],
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

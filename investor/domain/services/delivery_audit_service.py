#!/usr/bin/env python3
"""Human-facing view of privacy-safe Feishu delivery metadata."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_AUDIT_PATH = Path("/root/.openclaw/workspace/reports/feishu_delivery_audit.jsonl")


def _audit_path() -> Path:
    configured = os.getenv("FEISHU_DELIVERY_AUDIT_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_AUDIT_PATH


def load_delivery_audit(limit: int = 30) -> list[dict[str, Any]]:
    path = _audit_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-max(1, int(limit)) :]
    except Exception:
        return []
    for line in lines:
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _display_time(value: object) -> str:
    raw = str(value or "").strip()
    try:
        return datetime.fromisoformat(raw).strftime("%m-%d %H:%M:%S")
    except Exception:
        return raw[:19] or "时间未知"


def format_delivery_audit_text(limit: int = 12) -> str:
    rows = load_delivery_audit(limit=max(limit * 3, 30))
    lines = ["📨 飞书推送验收", ""]
    if not rows:
        lines.extend(
            [
                "- 新版投递审计启用后尚无记录。",
                "- 审计只记录标题、卡片类型、传输结果和时间，不保存正文或接收者身份。",
            ]
        )
        return "\n".join(lines)

    # One logical delivery can try the common rich-card transport first and then
    # a validated text fallback. Keep only the final outcome for the same body.
    logical: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(rows):
        digest = str(item.get("content_sha256") or "").strip()
        key = digest or f"row:{index}"
        if key in logical:
            logical.pop(key)
        logical[key] = item
    recent = list(logical.values())[-max(1, int(limit)) :]
    success = sum(1 for item in recent if item.get("sent"))
    blocked = sum(1 for item in recent if item.get("quality_gate") == "blocked")
    selftests = sum(1 for item in recent if item.get("transport") == "audit_selftest")
    failed = len(recent) - success - blocked - selftests
    summary = f"- 最近 {len(recent)} 次：成功 {success} 次，质量门禁拦截 {blocked} 次，传输失败 {failed} 次。"
    if selftests:
        summary += f"另有链路自检 {selftests} 次（未实际发送）。"
    lines.extend(
        [
            "**最近结果**",
            summary,
            "",
            "**投递明细**",
        ]
    )
    transport_labels = {
        "webhook": "机器人富卡片",
        "openapi": "飞书开放接口",
        "openclaw_cli": "OpenClaw 文本通道",
        "quality_gate": "质量门禁",
        "missing_target": "缺少接收目标",
        "audit_selftest": "审计自检",
        "wrapper_raw_fallback": "定时报告文本降级",
        "webhook_raw_fallback": "查询回复文本降级",
        "event_raw_fallback": "事件提醒文本降级",
        "health_raw_fallback": "健康告警文本降级",
    }
    for item in reversed(recent):
        if item.get("transport") == "audit_selftest":
            state = "链路自检，未实际发送"
        elif item.get("sent"):
            state = "已送达"
        elif item.get("quality_gate") == "blocked":
            state = "已拦截，未发送"
        else:
            state = "发送失败"
        format_text = "富卡片" if item.get("message_format") == "rich_card" else "文本"
        if item.get("diagnostic"):
            format_text += "诊断"
        transport = transport_labels.get(str(item.get("transport") or ""), "其他通道")
        lines.append(
            f"- {_display_time(item.get('occurred_at'))}｜{item.get('report_title') or '未命名报告'}｜"
            f"{format_text}｜{transport}｜{state}。"
        )
    lines.extend(
        [
            "",
            "**隐私与边界**",
            "- 审计不保存报告正文、接收者 ID 或账户数据；它只能证明发送链路结果，不能替代你对内容质量的人工评分。",
        ]
    )
    return "\n".join(lines)

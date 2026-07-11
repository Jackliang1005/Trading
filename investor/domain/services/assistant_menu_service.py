#!/usr/bin/env python3
"""Operator-facing command menu for the OpenClaw A-share assistant."""
from __future__ import annotations

from typing import Dict, List


def build_assistant_menu() -> Dict:
    sections: List[Dict] = [
        {
            "title": "飞书/自然语言常用问法",
            "items": [
                {"ask": "/状态", "cli": "python3 main.py assistant-status", "purpose": "查看投资助理服务、定时器、最新报告和阻塞项"},
                {"ask": "/健康", "cli": "python3 main.py runtime-check", "purpose": "检查 qmt2http、账户读口、日志与服务健康"},
                {"ask": "/策略状态 国金 / /策略停止 国金 确认", "cli": "python3 main.py strategy-control <status|start|stop|restart> <guojin|dongguan> [--confirm]", "purpose": "查看或控制 qmt2http 策略服务；启停需带确认"},
                {"ask": "/持仓 或 国金今天持仓", "cli": "python3 main.py today-account", "purpose": "查看国金/东莞账户、持仓、委托、成交"},
                {"ask": "/早报", "cli": "python3 main.py morning-brief --save", "purpose": "盘前汇总全球事件、风险、长线组合和阻塞项"},
                {"ask": "/监控", "cli": "python3 main.py today-summary --text", "purpose": "查看候选、买入、账户、告警和长线摘要"},
                {"ask": "/风险", "cli": "python3 main.py risk-report --save", "purpose": "查看持仓集中度、现金比例、浮盈亏和快照新鲜度"},
                {"ask": "/事件 或 今天有什么事件", "cli": "python3 main.py event-today", "purpose": "查看财经事件、主题、产业链和相关 A股"},
                {"ask": "/全球", "cli": "python3 main.py global-event-brief", "purpose": "扫描全球突发财经新闻并映射到 A股主题"},
                {"ask": "/影响", "cli": "python3 main.py global-impact-brief", "purpose": "按优先级输出全球突发新闻、持仓命中、A股传导和观察动作"},
                {"ask": "/关注", "cli": "python3 main.py watchlist-report", "purpose": "把最近事件主题映射成 A股关注清单"},
                {"ask": "/长线", "cli": "python3 main.py longterm-summary", "purpose": "查看长线组合 NAV、现金、持仓和计划"},
                {"ask": "/主题 华为", "cli": "python3 main.py theme-map 华为", "purpose": "把主题映射到产业链和 A股标的"},
                {"ask": "/收盘", "cli": "python3 main.py closing-brief --save", "purpose": "收盘后汇总事件、风险、长线组合和阻塞项"},
                {"ask": "/复盘", "cli": "python3 main.py reflect", "purpose": "收盘后生成预测/交易反思"},
                {"ask": "/审计", "cli": "python3 main.py capability-audit", "purpose": "查看投资助理能力审计与阻塞项"},
                {"ask": "/周报", "cli": "python3 main.py weekly-report --save", "purpose": "生成一周事件、预测、长线组合和阻塞项报告"},
            ],
        },
        {
            "title": "服务器 CLI 快捷命令",
            "items": [
                {"cli": "/root/.openclaw/workspace/scripts/investor_assistant_healthcheck.sh", "purpose": "生成一键健康巡检报告"},
                {"cli": "/root/.openclaw/workspace/scripts/investor_assistant_audit.py", "purpose": "生成投资助理能力覆盖审计"},
                {"cli": "/root/.openclaw/workspace/scripts/investor_health_alert.py --dry-run", "purpose": "Preview lightweight health alert probe"},
                {"cli": "/root/.openclaw/workspace/scripts/qmt2http_remote_recovery.py --server guojin --timeout 6", "purpose": "Read-only Guojin qmt2http recovery probe"},
                {"cli": "systemctl list-timers --all | grep -Ei 'investor|trading|qmttrader-v2'", "purpose": "查看投资助理定时任务"},
                {"cli": "systemctl status investor-event-watch feishu-webhook trading-intraday", "purpose": "查看三类常驻服务"},
                {"cli": "cd /root/.openclaw/workspace/investor && python3 main.py smoke-check", "purpose": "执行 investor 主线 smoke 检查"},
            ],
        },
        {
            "title": "自动化时间表",
            "items": [
                {"time": "07:30", "unit": "investor-collect.timer", "purpose": "采集每日市场与账户数据"},
                {"time": "08:55", "unit": "investor-morning-brief.timer", "purpose": "交易日盘前投资助理早报与飞书推送"},
                {"time": "09:30", "unit": "investor-predict.timer", "purpose": "生成每日预测"},
                {"time": "09:00-15:45", "unit": "investor-health-alert.timer", "purpose": "交易时段 qmt2http、日志与服务健康告警"},
                {"time": "10:35/14:35", "unit": "investor-risk-report.timer", "purpose": "盘中持仓风险报告与飞书推送"},
                {"time": "24/7 every 15m", "unit": "investor-global-event-scan.timer", "purpose": "全球突发财经新闻扫描与飞书推送"},
                {"time": "09:35", "unit": "trading-morning.timer", "purpose": "长线早盘复核"},
                {"time": "09:45", "unit": "investor-briefing-0945.timer", "purpose": "东莞策略简报"},
                {"time": "13:20", "unit": "investor-briefing-1320.timer", "purpose": "国金 ETF 午盘简报"},
                {"time": "14:20", "unit": "investor-briefing-1420.timer", "purpose": "国金 ETF 尾盘简报"},
                {"time": "15:35", "unit": "trading-evening.timer", "purpose": "长线收盘决策"},
                {"time": "16:05", "unit": "investor-closing-brief.timer", "purpose": "收盘后快速复盘简报与飞书推送"},
                {"time": "18:05", "unit": "qmttrader-v2-concepts.timer", "purpose": "更新 qmttrader_v2 热点概念库"},
                {"time": "18:30", "unit": "investor-daily-maintain.timer", "purpose": "packet 与 handoff 维护"},
                {"time": "20:30", "unit": "investor-reflect.timer", "purpose": "每日反思复盘"},
                {"time": "21:00", "unit": "investor-capability-audit.timer", "purpose": "每日能力审计与阻塞项推送"},
                {"time": "Fri 20:45", "unit": "investor-weekly-report.timer", "purpose": "周度投资助理报告与飞书推送"},
            ],
        },
        {
            "title": "当前外部依赖提醒",
            "items": [
                {"name": "国金 qmt2http", "status": "当前健康/持仓读口超时或断连", "next": "在国金 Windows 生产机排查 miniQMT 与 qmt2http"},
                {"name": "qmttrader_v2 日志", "status": "OpenClaw 已切到 /api/qmttrader_v2/status 与 /logs", "next": "以 qmt2http 返回为生产状态依据"},
                {"name": "概念库", "status": "已由 qmttrader-v2-concepts.timer 每个交易日晚间更新 /root/qmttrader_v2/concept_db/concepts.db", "next": "关注数据源失败告警"},
            ],
        },
    ]
    return {"name": "OpenClaw A股个人投资助理", "sections": sections}


def format_assistant_menu_text() -> str:
    data = build_assistant_menu()
    lines = [data["name"], ""]
    for section in data["sections"]:
        lines.append(f"【{section['title']}】")
        for item in section["items"]:
            label = item.get("ask") or item.get("time") or item.get("name") or item.get("cli") or item.get("unit")
            detail = item.get("purpose") or item.get("status") or ""
            cli = item.get("cli")
            unit = item.get("unit")
            next_step = item.get("next")
            suffix = detail
            if cli:
                suffix = f"{suffix} | {cli}" if suffix else cli
            if unit:
                suffix = f"{suffix} | {unit}" if suffix else unit
            if next_step:
                suffix = f"{suffix} | 下一步: {next_step}" if suffix else f"下一步: {next_step}"
            lines.append(f"- {label}: {suffix}" if suffix else f"- {label}")
        lines.append("")
    return "\n".join(lines).strip()

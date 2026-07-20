#!/usr/bin/env python3
"""Concise operator-facing menu for the OpenClaw A-share assistant."""

from __future__ import annotations

from typing import Dict, List


def build_assistant_menu() -> Dict:
    sections: List[Dict] = [
        {
            "title": "常用报告与查询",
            "items": [
                {"ask": "/早报", "cli": "python3 main.py morning-brief", "purpose": "08:30 汇总隔夜新闻、美股收盘、日韩开盘、A股传导和组合风险"},
                {"ask": "/日内预测 0930", "cli": "python3 main.py intraday-outlook 0930", "purpose": "开盘预测与验证条件"},
                {"ask": "/日内预测 1030", "cli": "python3 main.py intraday-outlook 1030", "purpose": "根据实际走势修正开盘预测"},
                {"ask": "/日内预测 1430", "cli": "python3 main.py intraday-outlook 1430", "purpose": "复盘预测并评估降仓或清仓条件"},
                {"ask": "/风险", "cli": "python3 main.py risk-report", "purpose": "持仓集中度、现金、盈亏和快照新鲜度"},
                {"ask": "/交易建议", "cli": "python3 main.py decision-monitor", "purpose": "按实时证据逐仓给出风险建议，不自动下单"},
                {"ask": "/影响", "cli": "python3 main.py global-impact-brief", "purpose": "全球事件、A股传导、概念板块与个股动量"},
                {"ask": "/关注", "cli": "python3 main.py watchlist-report", "purpose": "仅展示直接事件命中或通过概念动量筛选的候选"},
                {"ask": "/收盘", "cli": "python3 main.py closing-brief", "purpose": "市场、组合、下一交易日机会与持仓动作"},
                {"ask": "/复盘", "cli": "python3 main.py reflect", "purpose": "有效成交、持仓风险、建议闭环和预测复盘"},
                {"ask": "/周报", "cli": "python3 main.py weekly-report", "purpose": "一周主线、预测验证、长线组合和下周动作"},
                {"ask": "/持仓", "cli": "python3 main.py today-account", "purpose": "国金与东莞账户、持仓、委托和成交"},
                {"ask": "/豁免名单 东莞", "cli": "python3 main.py position-monitor-exemptions list dongguan", "purpose": "查看持仓监控豁免名单"},
                {"ask": "/追加豁免 东莞 600584 确认", "cli": "python3 main.py position-monitor-exemptions append dongguan 600584 --confirm", "purpose": "确认后写入并回读验证"},
                {"ask": "/策略状态 国金", "cli": "python3 main.py strategy-control status guojin", "purpose": "只读查看策略状态；启停命令必须确认"},
                {"ask": "/状态", "cli": "python3 main.py assistant-status", "purpose": "服务、定时器、最新报告和阻断项"},
                {"ask": "/推送状态", "cli": "python3 main.py feishu-query /推送状态", "purpose": "最近飞书卡片的送达、降级与质量门禁结果"},
                {"ask": "/推送验收", "cli": "python3 ../scripts/report_live_acceptance.py", "purpose": "逐时核对今天所有必达报告是否按时且合格送达"},
            ],
        },
        {
            "title": "自动推送时间表",
            "items": [
                {"time": "08:30", "unit": "investor-morning-brief.timer", "purpose": "盘前简报"},
                {"time": "09:30", "unit": "investor-predict.timer", "purpose": "开盘预测"},
                {"time": "09:35", "unit": "investor-decision-0935.timer", "purpose": "开盘持仓风险检查"},
                {"time": "09:35", "unit": "trading-morning.timer", "purpose": "长线组合开盘人工复核"},
                {"time": "09:45", "unit": "investor-briefing-0945.timer", "purpose": "东莞策略检查"},
                {"time": "10:30", "unit": "investor-decision-1030.timer", "purpose": "走势修正"},
                {"time": "10:36 / 14:36", "unit": "investor-risk-report.timer", "purpose": "持仓风险报告"},
                {"time": "13:20", "unit": "investor-briefing-1320.timer", "purpose": "国金ETF午盘简报"},
                {"time": "14:20", "unit": "investor-briefing-1420.timer", "purpose": "国金ETF尾盘简报"},
                {"time": "14:30", "unit": "investor-outlook-1430.timer", "purpose": "预测复盘与仓位决策"},
                {"time": "15:35", "unit": "trading-evening.timer", "purpose": "长线组合收盘决策"},
                {"time": "16:05", "unit": "investor-closing-brief.timer", "purpose": "收盘简报"},
                {"time": "20:30", "unit": "investor-reflect.timer", "purpose": "每日交易复盘"},
                {"time": "周五 20:45", "unit": "investor-weekly-report.timer", "purpose": "投资周报"},
            ],
        },
        {
            "title": "个股与市场工具",
            "items": [
                {"ask": "/行情 603986｜/技术分析 603986", "purpose": "行情快照、趋势、动量、支撑和阻力"},
                {"ask": "/财务 603986｜/估值 603986", "purpose": "财报质量、增长和估值日期差异"},
                {"ask": "/公告 603986｜/公告解读 603986", "purpose": "公告原文与标题级线索，重大结论需回读原文"},
                {"ask": "/筛选 ROE>10｜/板块涨幅", "purpose": "基本面候选与概念板块强弱，随后核验个股动量"},
                {"ask": "/市场情绪｜/宏观", "purpose": "涨跌停结构与带报告期的宏观快照"},
            ],
        },
        {
            "title": "安全与数据原则",
            "items": [
                {"name": "实时数据", "status": "不可达、非当日或回读不一致时明确降级", "next": "不根据历史持仓、委托或模型推断实时结果"},
                {"name": "交易动作", "status": "所有报告只给人工建议", "next": "不会因报告自动下单或自动启动QMT客户端"},
                {"name": "候选个股", "status": "事件主题 → 概念板块 → 20/60日动量", "next": "盘中仍须用板块强弱和量价确认"},
            ],
        },
    ]
    return {"name": "OpenClaw A股投资助理", "sections": sections}


def format_assistant_menu_text() -> str:
    menu = build_assistant_menu()
    lines = [f"🦞 {menu['name']}"]
    for section in menu.get("sections", []):
        lines.extend(["", f"**{section.get('title')}**"])
        for item in section.get("items", []):
            command = item.get("ask") or item.get("time") or item.get("name") or item.get("cli")
            purpose = item.get("purpose") or item.get("status") or ""
            lines.append(f"- {command}｜{purpose}")
    return "\n".join(lines)

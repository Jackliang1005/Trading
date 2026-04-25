#!/usr/bin/env python3
"""Legacy main.py orchestration facade."""

from __future__ import annotations

from typing import Any, Dict

import db
from data_collector import collect_daily_data
from domain.services.assistant_service import dashboard as dashboard_service
from domain.services.evolution_service import evolve, generate_system_prompt
from domain.services.prediction_orchestrator import generate_predictions
from domain.services.reflection_service import daily_reflection, monthly_audit, weekly_attribution
from knowledge_base import KnowledgeBase, auto_memorize_news
from sector_scanner import generate_sector_report


def init_system() -> None:
    """Initialize DB, KB and baseline prompt."""
    print("🦞 OpenClaw Investor 初始化...")
    db.init_db()
    KnowledgeBase()
    generate_system_prompt()
    print("✅ 初始化完成\n")


def cron_daily_collect() -> Dict[str, Any]:
    """每日数据采集（07:30执行）"""
    init_system()
    data = collect_daily_data()
    all_news = data.get("news_eastmoney", []) + data.get("news_rss", [])
    auto_memorize_news(all_news)
    return data


def cron_daily_predict() -> Dict[str, Any]:
    """每日预测生成（09:30收盘竞价后执行）"""
    init_system()
    prediction_ids = generate_predictions()
    return {"prediction_ids": prediction_ids, "count": len(prediction_ids)}


def cron_daily_reflect() -> Dict[str, Any]:
    """每日反思（20:30执行，晚间复盘后）"""
    init_system()
    return daily_reflection()


def cron_weekly_evolve() -> Dict[str, Any]:
    """每周进化（周日 21:00执行）"""
    init_system()
    weekly_attribution()
    return evolve()


def cron_sector_scan() -> Dict[str, Any]:
    """板块扫描+持仓诊断"""
    init_system()
    return generate_sector_report()


def cron_monthly_audit() -> Dict[str, Any]:
    """每月审计（每月1日 22:00）"""
    init_system()
    monthly_audit()
    return evolve()


def build_dashboard() -> str:
    """Render status dashboard with compatibility init behavior."""
    init_system()
    return dashboard_service()

#!/usr/bin/env python3
"""Compatibility wrappers for live monitor workflows."""

from __future__ import annotations

from typing import Dict, Optional

from domain.services.live_monitor_service import run_live_monitor as _run_live_monitor
from domain.services.live_monitor_view_service import (
    format_today_summary_text as _format_today_summary_text,
    get_today_account as _get_today_account,
    get_today_buys as _get_today_buys,
    get_today_candidates as _get_today_candidates,
    get_today_summary as _get_today_summary,
    run_trading_monitor as _run_trading_monitor,
)


def run_live_monitor(date: Optional[str] = None) -> Dict:
    return _run_live_monitor(date=date)


def run_trading_monitor(date: Optional[str] = None) -> Dict:
    return _run_trading_monitor(date=date)


def get_today_candidates(date: Optional[str] = None) -> Dict:
    return _get_today_candidates(date=date)


def get_today_buys(date: Optional[str] = None) -> Dict:
    return _get_today_buys(date=date)


def get_today_account(date: Optional[str] = None) -> Dict:
    return _get_today_account(date=date)


def get_today_summary(date: Optional[str] = None) -> Dict:
    return _get_today_summary(date=date)


def format_today_summary_text(date: Optional[str] = None) -> str:
    return _format_today_summary_text(date=date)


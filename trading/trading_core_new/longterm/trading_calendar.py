from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple


def _is_weekday(date_str: str) -> bool:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.weekday() < 5


def _load_holiday_set() -> set[str]:
    candidates = [
        Path("/root/.openclaw/workspace/trading_holidays.json"),
        Path("/root/.openclaw/workspace/trading/trading_holidays.json"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, list):
            return {str(x) for x in payload if str(x)}
        if isinstance(payload, dict):
            values = payload.get("holidays") or payload.get("dates") or []
            if isinstance(values, list):
                return {str(x) for x in values if str(x)}
    return set()


def is_cn_trading_day(date_str: str) -> Tuple[bool, str]:
    date_str = str(date_str or "").strip()
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    # Primary source: exchange_calendars XSHG
    try:
        import exchange_calendars as xcals

        cal = xcals.get_calendar("XSHG")
        if bool(cal.is_session(date_str)):
            return True, "exchange_calendars:XSHG"
        return False, "exchange_calendars:XSHG"
    except Exception:
        pass

    # Fallback: weekday + local holiday list
    if not _is_weekday(date_str):
        return False, "weekday_fallback:weekend"
    holidays = _load_holiday_set()
    if date_str in holidays:
        return False, "weekday_fallback:holiday_file"
    return True, "weekday_fallback"


def last_trading_day(before_date: Optional[str] = None) -> str:
    """Return the most recent trading day on or before the given date.

    Walks backwards from before_date (default: today) until a trading day is found.
    """
    if before_date:
        dt = datetime.strptime(str(before_date).strip()[:10], "%Y-%m-%d")
    else:
        dt = datetime.now()

    for _ in range(30):
        ds = dt.strftime("%Y-%m-%d")
        is_open, _ = is_cn_trading_day(ds)
        if is_open:
            return ds
        dt = dt - timedelta(days=1)

    # Fallback: return original date
    return (datetime.strptime(str(before_date)[:10], "%Y-%m-%d") if before_date else datetime.now()).strftime("%Y-%m-%d")

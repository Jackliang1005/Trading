"""Read-only T-monitor status adapter."""
from __future__ import annotations
import json
import re
import urllib.request
from domain.services.qmt_strategy_control_service import ACCOUNT_ALIASES, DEFAULT_BASE_URLS, _headers, _resolve_base_url, _resolve_token, normalize_strategy_account


def get_t_monitor(account: str, symbol: str = "") -> dict:
    if account not in DEFAULT_BASE_URLS:
        return {"ok": False, "text": "Please specify guojin or dongguan."}
    alias = ACCOUNT_ALIASES.get(account, account)
    try:
        request = urllib.request.Request(_resolve_base_url(account) + "/api/qmttrader_v2/status", headers=_headers(_resolve_token()))
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except Exception as exc:
        return {"ok": False, "text": f"{alias} T-monitor unavailable: {exc}. No trading action was taken."}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    live_state = data.get("live_state") if isinstance(data.get("live_state"), dict) else {}
    state = live_state.get("data") if isinstance(live_state.get("data"), dict) else {}
    monitor = state.get("t_monitor") if isinstance(state.get("t_monitor"), dict) else {}
    if not monitor:
        return {"ok": False, "text": f"{alias} T-monitor has no current data."}
    rows = monitor.get("advisories") if isinstance(monitor.get("advisories"), list) else []
    target = str(symbol or "").strip().upper()
    if target:
        rows = [row for row in rows if isinstance(row, dict) and str(row.get("symbol", "")).upper().startswith(target)]
    lines = [f"{alias} T-monitor (advisory only; never submits orders)"]
    for row in rows:
        lines.append(f"- {row.get('symbol')}: {row.get('action')} | price {float(row.get('price') or 0):.2f} | sellable {int(row.get('closeable_volume') or 0)} | sell watch {float(row.get('sell_watch_price') or 0):.2f} | buyback watch {float(row.get('buyback_watch_price') or 0):.2f}")
    if not rows:
        lines.append("No matching exempt holding advisory in this cycle.")
    return {"ok": True, "monitor": monitor, "text": "\n".join(lines)}


def is_t_monitor_query(text: str) -> bool:
    query = str(text or "").lower()
    return any(item in query for item in ("t-monitor", "t monitor", "做t"))


def handle_t_monitor_query(text: str) -> str:
    query = str(text or "")
    codes = re.findall(r"(?<!\d)(\d{6}(?:\.(?:SH|SZ))?)(?!\d)", query, re.IGNORECASE)
    return get_t_monitor(normalize_strategy_account(query), codes[0] if codes else "").get("text", "")

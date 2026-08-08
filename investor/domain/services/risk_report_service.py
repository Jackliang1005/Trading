
#!/usr/bin/env python3
"""Portfolio risk report from latest OpenClaw portfolio snapshot."""

from __future__ import annotations

import contextlib
import io
import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

import db
from domain.services.report_style_service import join_cn, money, pct, risk_label, source_label

WORKSPACE = Path("/root/.openclaw/workspace")
REPORTS_DIR = WORKSPACE / "reports"
ACCOUNT_SENTINEL_LIMIT = 10_000_000_000.0


def _init_db_quietly() -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        db.init_db()


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _position_code(item: Dict[str, Any]) -> str:
    return str(item.get("stock_code") or item.get("code") or item.get("instrument_id") or "").strip()


def _position_name(item: Dict[str, Any]) -> str:
    return str(item.get("stock_name") or item.get("name") or item.get("instrument_name") or _position_code(item)).strip()


def _market_value(item: Dict[str, Any]) -> float:
    return _f(item.get("market_value", item.get("m_dMarketValue")))


def _float_profit(item: Dict[str, Any]) -> float:
    return _f(item.get("unrealized_pnl", item.get("float_profit", item.get("m_dFloatProfit"))))


def _volume(item: Dict[str, Any]) -> int:
    return int(_f(item.get("volume", item.get("m_nVolume", item.get("total_volume")))))


def _snapshot_age_days(as_of: str) -> int | None:
    try:
        d = date.fromisoformat(str(as_of)[:10])
    except Exception:
        return None
    return (date.today() - d).days


def _plausible_account_value(value: Any) -> bool:
    try:
        numeric = float(value)
    except Exception:
        return False
    return 0 <= numeric < ACCOUNT_SENTINEL_LIMIT


def _account_metrics(data: Dict[str, Any], total_position_value: float) -> Dict[str, Any]:
    """Build conservative asset metrics and reject upstream sentinel values."""
    summary = data.get("qmt_trading_summary", {}) or {}
    source_accounts = summary.get("accounts", {}) or {}
    if not isinstance(source_accounts, dict) or not source_accounts:
        source_accounts = {"combined": data.get("qmt_account", {}) or {}}

    known_cash = 0.0
    known_total_assets = 0.0
    valid_cash_sources: List[str] = []
    invalid_sources: List[str] = []
    for source, account in source_accounts.items():
        if not isinstance(account, dict) or not account:
            invalid_sources.append(str(source))
            continue
        raw_cash = account.get("cash", account.get("m_dCash"))
        raw_total = account.get("total_asset", account.get("m_dTotalAsset"))
        cash_ok = _plausible_account_value(raw_cash)
        total_ok = _plausible_account_value(raw_total)
        if cash_ok:
            known_cash += float(raw_cash)
            valid_cash_sources.append(str(source))
        if total_ok:
            known_total_assets += float(raw_total)
        if not cash_ok or not total_ok:
            invalid_sources.append(str(source))

    # Position rows are independently verified by source.  When one account's
    # asset endpoint contains a sentinel (for example 99,999,999,999), compute
    # concentration from position value plus only known-good cash.
    effective_total = max(total_position_value + known_cash, known_total_assets, total_position_value)
    return {
        "cash": known_cash,
        "cash_complete": not invalid_sources,
        "valid_cash_sources": valid_cash_sources,
        "invalid_sources": sorted(set(invalid_sources)),
        "effective_total": effective_total,
    }


def _snapshot_is_usable(snapshot: Dict[str, Any] | None) -> bool:
    """Distinguish a verified empty account from a failed empty payload."""
    if not snapshot:
        return False
    data = snapshot.get("data", {}) or {}
    positions = data.get("qmt_positions", data.get("positions", [])) or []
    if any(isinstance(item, dict) and (_market_value(item) > 0 or _volume(item) > 0) for item in positions):
        return True
    account = data.get("qmt_account", {}) or {}
    account_id = str(account.get("account_id") or account.get("m_strAccountID") or "").strip()
    if account_id:
        return True
    # Some gateways omit the account id but still return an explicit asset
    # payload.  Presence of a real numeric field is evidence; an empty dict is
    # not.  This keeps a genuinely all-cash/zero-position account valid.
    return any(
        key in account and account.get(key) is not None
        for key in ("total_asset", "m_dTotalAsset", "cash", "m_dCash", "market_value", "m_dMarketValue")
    )


def _recent_combined_snapshots(limit: int = 50) -> List[Dict[str, Any]]:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM portfolio_snapshots
               WHERE account_scope='combined'
               ORDER BY as_of_date DESC, created_at DESC, id DESC
               LIMIT ?""",
            (max(1, limit),),
        ).fetchall()
    finally:
        conn.close()
    snapshots: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["data"] = json.loads(item.get("data") or "{}")
        except Exception:
            item["data"] = {}
        try:
            item["metadata"] = json.loads(item.get("metadata") or "{}")
        except Exception:
            item["metadata"] = {}
        snapshots.append(item)
    return snapshots


def _select_risk_snapshot(max_fallback_age_days: int = 3) -> tuple[Dict[str, Any] | None, bool]:
    latest = db.get_latest_portfolio_snapshot(account_scope="combined")
    if _snapshot_is_usable(latest):
        return latest, False
    latest_id = latest.get("id") if latest else None
    for candidate in _recent_combined_snapshots():
        if latest_id is not None and candidate.get("id") == latest_id:
            continue
        age = _snapshot_age_days(str(candidate.get("as_of_date") or ""))
        if age is None or age > max_fallback_age_days:
            continue
        if _snapshot_is_usable(candidate):
            return candidate, True
    return None, False


def build_risk_report() -> Dict[str, Any]:
    _init_db_quietly()
    snapshot, fallback_snapshot = _select_risk_snapshot()
    if not snapshot:
        return {
            "available": False,
            "data_status": "unavailable",
            "text": "持仓风险报告不可用：最新账户数据为空，且近 3 天没有可验证的非空快照。不会把缺失值写成零持仓。",
        }
    data = snapshot.get("data", {}) or {}
    positions = data.get("qmt_positions", data.get("positions", [])) or []
    positions = [p for p in positions if isinstance(p, dict) and _market_value(p) > 0]
    as_of = str(snapshot.get("as_of_date", "") or "")
    total_mv = sum(_market_value(p) for p in positions)
    total_pnl = sum(_float_profit(p) for p in positions)
    account_metrics = _account_metrics(data, total_mv)
    cash = float(account_metrics["cash"])
    effective_total = float(account_metrics["effective_total"])
    sorted_positions = sorted(positions, key=_market_value, reverse=True)
    top1_ratio = (_market_value(sorted_positions[0]) / effective_total) if sorted_positions and effective_total else 0.0
    top3_mv = sum(_market_value(p) for p in sorted_positions[:3])
    top3_ratio = (top3_mv / effective_total) if effective_total else 0.0
    by_source: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "market_value": 0.0, "pnl": 0.0})
    for p in positions:
        source = str(p.get("_source") or "unknown")
        by_source[source]["count"] += 1
        by_source[source]["market_value"] += _market_value(p)
        by_source[source]["pnl"] += _float_profit(p)
    age_days = _snapshot_age_days(as_of)
    flags: List[str] = []
    if age_days is None:
        flags.append("snapshot_date_unknown")
    elif age_days > 3:
        flags.append(f"snapshot_stale_{age_days}d")
    if top1_ratio >= 0.30:
        flags.append("top1_concentration_high")
    if top3_ratio >= 0.70:
        flags.append("top3_concentration_high")
    if total_pnl < 0:
        flags.append("portfolio_unrealized_loss")
    if not flags:
        flags.append("no_major_snapshot_risk_flag")
    report = {
        "available": True,
        "data_status": "fallback_snapshot" if fallback_snapshot else "current_snapshot",
        "fallback_snapshot": fallback_snapshot,
        "snapshot_created_at": str(snapshot.get("created_at") or ""),
        "as_of": as_of,
        "snapshot_age_days": age_days,
        "positions_count": len(positions),
        "total_market_value": round(total_mv, 2),
        "effective_total_asset": round(effective_total, 2),
        "cash": round(cash, 2),
        "cash_ratio": round(cash / effective_total, 4) if effective_total else 0.0,
        "cash_complete": bool(account_metrics["cash_complete"]),
        "invalid_account_sources": account_metrics["invalid_sources"],
        "total_unrealized_pnl": round(total_pnl, 2),
        "top1_ratio": round(top1_ratio, 4),
        "top3_ratio": round(top3_ratio, 4),
        "by_source": {k: {"count": v["count"], "market_value": round(v["market_value"], 2), "pnl": round(v["pnl"], 2)} for k, v in by_source.items()},
        "top_positions": [
            {
                "code": _position_code(p),
                "name": _position_name(p),
                "source": str(p.get("_source") or "unknown"),
                "volume": _volume(p),
                "market_value": round(_market_value(p), 2),
                "weight": round((_market_value(p) / effective_total) if effective_total else 0.0, 4),
                "pnl": round(_float_profit(p), 2),
            }
            for p in sorted_positions[:8]
        ],
        "risk_flags": flags,
    }
    report["text"] = format_risk_report(report)
    return report


def format_risk_report(report: Dict[str, Any]) -> str:
    if not report.get("available"):
        return str(report.get("text") or "持仓风险报告不可用。")
    flags = [risk_label(item) for item in report.get("risk_flags") or []]
    if report.get("fallback_snapshot"):
        flags.insert(0, "实时账户数据不可用，使用最近可验证快照")
    age = report.get("snapshot_age_days")
    freshness = "日期未知" if age is None else ("当日快照" if age == 0 else f"距今 {age} 天")
    lines = [
        "🛡️ 持仓风险报告",
        f"数据日：{report.get('as_of') or '未知'}（{freshness}；{'最近可验证快照' if report.get('fallback_snapshot') else '当前采集快照'}）",
        "",
        "**核心结论**",
        f"- {join_cn(flags)}。",
        f"- {'快照记录' if report.get('fallback_snapshot') else '当前'} {report.get('positions_count')} 只持仓，市值 {money(report.get('total_market_value'))}，浮动盈亏 {money(report.get('total_unrealized_pnl'))}。",
        "",
        "**仓位结构**",
        (
            f"- 现金 {money(report.get('cash'))}（{pct(report.get('cash_ratio', 0) * 100)}）；"
            if report.get("cash_complete")
            else f"- 可验证现金 {money(report.get('cash'))}（部分账户资产字段异常，不计算完整现金占比）；"
        )
        + f"第一大持仓 {pct(report.get('top1_ratio', 0) * 100)}，前三大持仓 {pct(report.get('top3_ratio', 0) * 100)}。",
    ]
    if report.get("invalid_account_sources"):
        lines.append(
            "- 账户资产降级："
            + join_cn(source_label(item) for item in report.get("invalid_account_sources") or [])
            + " 的现金或总资产值未通过合理性校验；集中度按可验证持仓市值与现金计算。"
        )
    source_parts = []
    for key, value in sorted((report.get("by_source") or {}).items()):
        source_parts.append(f"{source_label(key)} {value['count']} 只 / {money(value['market_value'])} / 盈亏 {money(value['pnl'])}")
    if source_parts:
        lines.append("- 账户分布：" + "；".join(source_parts) + "。")
    if report.get("fallback_snapshot"):
        lines.append("- 实时账户链路未提供可信数据；以上为最近可验证快照，不代表当前实时持仓或成交状态。")
    lines.extend(["", "**重点持仓**"])
    for p in report.get("top_positions", [])[:8]:
        lines.append(f"- **{p['name']}（{p['code']}）**｜仓位 {pct(p['weight']*100)}｜市值 {money(p['market_value'])}｜盈亏 {money(p['pnl'])}｜{source_label(p['source'])}")
    lines.extend(["", "**执行原则**"])
    if report.get("top1_ratio", 0) >= 0.30:
        lines.append("- 优先降低单票集中度；弱于所属板块且无放量承接时，不用补仓摊低成本。")
    if report.get("total_unrealized_pnl", 0) < 0:
        lines.append("- 组合浮亏阶段先控制回撤，再考虑新增高波动仓位。")
    if not any((report.get("top1_ratio", 0) >= 0.30, report.get("total_unrealized_pnl", 0) < 0)):
        lines.append("- 当前快照未触发主要风险阈值，继续观察集中度与回撤变化。")
    return "\n".join(lines)


def save_risk_report(report: Dict[str, Any]) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"investor_risk_report_{stamp}.md"
    latest = REPORTS_DIR / "investor_risk_report_latest.md"
    text = str(report.get("text") or format_risk_report(report))
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    (REPORTS_DIR / "investor_risk_report_latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(path)

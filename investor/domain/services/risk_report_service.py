
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


def build_risk_report() -> Dict[str, Any]:
    _init_db_quietly()
    snapshot = db.get_latest_portfolio_snapshot(account_scope="combined")
    if not snapshot:
        return {"available": False, "text": "持仓风险报告生成失败：没有可用的组合快照。不会依据历史持仓推断当前风险。"}
    data = snapshot.get("data", {}) or {}
    positions = data.get("qmt_positions", data.get("positions", [])) or []
    positions = [p for p in positions if isinstance(p, dict) and _market_value(p) > 0]
    account = data.get("qmt_account", {}) or {}
    as_of = str(snapshot.get("as_of_date", "") or "")
    total_mv = sum(_market_value(p) for p in positions)
    total_pnl = sum(_float_profit(p) for p in positions)
    account_total = _f(account.get("total_asset", account.get("m_dTotalAsset")))
    cash = _f(account.get("cash", account.get("m_dCash")))
    effective_total = max(account_total, total_mv + max(cash, 0), total_mv)
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
        "as_of": as_of,
        "snapshot_age_days": age_days,
        "positions_count": len(positions),
        "total_market_value": round(total_mv, 2),
        "effective_total_asset": round(effective_total, 2),
        "cash": round(cash, 2),
        "cash_ratio": round(cash / effective_total, 4) if effective_total else 0.0,
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
    age = report.get("snapshot_age_days")
    freshness = "日期未知" if age is None else ("当日快照" if age == 0 else f"距今 {age} 天")
    lines = [
        "🛡️ 持仓风险报告",
        f"数据日：{report.get('as_of') or '未知'}（{freshness}）",
        "",
        "**核心结论**",
        f"- {join_cn(flags)}。",
        f"- 当前 {report.get('positions_count')} 只持仓，市值 {money(report.get('total_market_value'))}，浮动盈亏 {money(report.get('total_unrealized_pnl'))}。",
        "",
        "**仓位结构**",
        f"- 现金 {money(report.get('cash'))}（{pct(report.get('cash_ratio', 0) * 100)}）；第一大持仓 {pct(report.get('top1_ratio', 0) * 100)}，前三大持仓 {pct(report.get('top3_ratio', 0) * 100)}。",
    ]
    source_parts = []
    for key, value in sorted((report.get("by_source") or {}).items()):
        source_parts.append(f"{source_label(key)} {value['count']} 只 / {money(value['market_value'])} / 盈亏 {money(value['pnl'])}")
    if source_parts:
        lines.append("- 账户分布：" + "；".join(source_parts) + "。")
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

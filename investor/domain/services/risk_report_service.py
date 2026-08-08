
#!/usr/bin/env python3
"""Portfolio risk report from latest OpenClaw portfolio snapshot."""

from __future__ import annotations

import contextlib
import copy
import io
import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

import db
from domain.policies.advisor_policy import load_advisor_policy
from domain.services.report_style_service import join_cn, money, pct, risk_label, source_label
from position_pnl import resolve_position_pnl

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
    return float(resolve_position_pnl(item)["pnl"])


def _volume(item: Dict[str, Any]) -> int:
    return int(_f(item.get("volume", item.get("m_nVolume", item.get("total_volume")))))


def _available_volume(item: Dict[str, Any]) -> tuple[int, bool]:
    """Return broker-reported sellable shares and whether that evidence exists."""
    for key in ("can_use_volume", "available_volume", "m_nCanUseVolume"):
        if key not in item or item.get(key) is None:
            continue
        try:
            value = float(item.get(key))
        except (TypeError, ValueError):
            return 0, False
        if value < 0:
            return 0, False
        return int(value), True
    return 0, False


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
    account_market_values: Dict[str, float] = {}
    valid_cash_sources: List[str] = []
    invalid_sources: List[str] = []
    expected_sources = [str(source) for source in summary.get("expected_sources") or source_accounts.keys()]
    missing_sources = [source for source in expected_sources if source not in source_accounts]
    stale_sources = {str(source): str(as_of) for source, as_of in (summary.get("stale_sources") or {}).items()}
    invalid_sources.extend(missing_sources)
    invalid_sources.extend(stale_sources)
    for source, account in source_accounts.items():
        if not isinstance(account, dict) or not account:
            invalid_sources.append(str(source))
            continue
        raw_cash = account.get("cash", account.get("m_dCash"))
        raw_total = account.get("total_asset", account.get("m_dTotalAsset"))
        raw_market_value = account.get("market_value", account.get("m_dMarketValue"))
        cash_ok = _plausible_account_value(raw_cash)
        total_ok = _plausible_account_value(raw_total)
        market_value_ok = _plausible_account_value(raw_market_value)
        if cash_ok:
            known_cash += float(raw_cash)
            valid_cash_sources.append(str(source))
        if total_ok:
            known_total_assets += float(raw_total)
        if market_value_ok:
            account_market_values[str(source)] = float(raw_market_value)
        if not cash_ok or not total_ok:
            invalid_sources.append(str(source))

    # Position rows are independently verified by source.  When one account's
    # asset endpoint contains a sentinel (for example 99,999,999,999), compute
    # concentration from position value plus only known-good cash.
    known_account_market_value = sum(account_market_values.values())
    effective_total = max(
        total_position_value + known_cash,
        known_account_market_value + known_cash,
        known_total_assets,
        total_position_value,
    )
    return {
        "cash": known_cash,
        "cash_complete": not invalid_sources,
        "valid_cash_sources": valid_cash_sources,
        "invalid_sources": sorted(set(invalid_sources)),
        "expected_sources": expected_sources,
        "missing_sources": missing_sources,
        "stale_sources": stale_sources,
        "source_coverage_complete": not missing_sources and not stale_sources,
        "account_market_values": account_market_values,
        "known_account_market_value": known_account_market_value,
        "effective_total": effective_total,
    }


def _position_coverage(positions: List[Dict[str, Any]], account_metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Detect account rows lost or duplicated while building a combined snapshot."""
    observed: Dict[str, float] = defaultdict(float)
    for item in positions:
        observed[str(item.get("_source") or "unknown")] += _market_value(item)
    expected = account_metrics.get("account_market_values") or {}
    mismatches = []
    for source, expected_value in expected.items():
        observed_value = observed.get(str(source), 0.0)
        tolerance = max(1.0, abs(float(expected_value)) * 0.005)
        if abs(observed_value - float(expected_value)) > tolerance:
            mismatches.append(
                {
                    "source": str(source),
                    "account_market_value": round(float(expected_value), 2),
                    "position_market_value": round(observed_value, 2),
                    "gap": round(float(expected_value) - observed_value, 2),
                }
            )
    expected_total = sum(float(value) for value in expected.values())
    observed_total = sum(observed.get(str(source), 0.0) for source in expected)
    return {
        "complete": not mismatches,
        "expected_market_value": round(expected_total, 2),
        "observed_market_value": round(observed_total, 2),
        "gap": round(expected_total - observed_total, 2),
        "mismatches": mismatches,
    }


def _aggregate_security_exposures(
    positions: List[Dict[str, Any]],
    stale_sources: set[str] | None = None,
) -> List[Dict[str, Any]]:
    """Aggregate concentration while preserving account-level execution evidence."""
    stale = {str(source) for source in (stale_sources or set())}
    grouped: Dict[str, Dict[str, Any]] = {}
    for item in positions:
        code = _position_code(item)
        if not code:
            continue
        exposure = grouped.setdefault(
            code,
            {
                "code": code,
                "name": _position_name(item),
                "volume": 0,
                "available_volume": 0,
                "available_volume_complete": True,
                "market_value": 0.0,
                "cost_value": 0.0,
                "pnl": 0.0,
                "pnl_bases": [],
                "pnl_conflict": False,
                "sources": [],
                "stale_sources": [],
                "account_positions": [],
            },
        )
        source = str(item.get("_source") or "unknown")
        available_volume, available_complete = _available_volume(item)
        source_stale = source in stale
        exposure["volume"] += _volume(item)
        if available_complete and not source_stale:
            exposure["available_volume"] += available_volume
        else:
            exposure["available_volume_complete"] = False
        exposure["market_value"] += _market_value(item)
        pnl_evidence = resolve_position_pnl(item)
        exposure["pnl"] += float(pnl_evidence["pnl"])
        if pnl_evidence.get("cost_value") is not None:
            exposure["cost_value"] += float(pnl_evidence["cost_value"])
        if pnl_evidence["basis"] not in exposure["pnl_bases"]:
            exposure["pnl_bases"].append(pnl_evidence["basis"])
        exposure["pnl_conflict"] = bool(exposure["pnl_conflict"] or pnl_evidence["cumulative_cost_conflict"])
        if source not in exposure["sources"]:
            exposure["sources"].append(source)
        if source_stale and source not in exposure["stale_sources"]:
            exposure["stale_sources"].append(source)
        exposure["account_positions"].append(
            {
                "source": source,
                "volume": _volume(item),
                "available_volume": available_volume if available_complete and not source_stale else None,
                "available_volume_complete": bool(available_complete and not source_stale),
                "stale": source_stale,
                "pnl_basis": pnl_evidence["basis"],
                "pnl_conflict": pnl_evidence["cumulative_cost_conflict"],
            }
        )
    for exposure in grouped.values():
        cost_value = float(exposure.get("cost_value") or 0)
        exposure["pnl_ratio"] = (float(exposure.get("pnl") or 0) / cost_value) if cost_value > 0 else None
    return list(grouped.values())


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


def _missing_snapshot_sources(snapshot: Dict[str, Any]) -> List[str]:
    data = snapshot.get("data", {}) or {}
    summary = data.get("qmt_trading_summary", {}) or {}
    expected = [str(source) for source in summary.get("expected_sources") or []]
    if not expected:
        return []
    accounts = summary.get("accounts", {}) or {}
    error_sources = {str(key).split(".", 1)[0] for key in (summary.get("source_errors") or {})}
    return [source for source in expected if source not in accounts or source in error_sources]


def _snapshot_source_payload(snapshot: Dict[str, Any], source: str) -> tuple[Dict[str, Any] | None, List[Dict[str, Any]]]:
    data = snapshot.get("data", {}) or {}
    summary = data.get("qmt_trading_summary", {}) or {}
    account = (summary.get("accounts", {}) or {}).get(source)
    positions = [
        copy.deepcopy(item)
        for item in data.get("qmt_positions", data.get("positions", [])) or []
        if isinstance(item, dict) and str(item.get("_source") or "") == source
    ]
    return copy.deepcopy(account) if isinstance(account, dict) else None, positions


def _enrich_partial_snapshot(
    latest: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    max_source_age_days: int = 3,
) -> Dict[str, Any]:
    """Fill an unavailable account from its recent per-source snapshot, explicitly stale."""
    missing = _missing_snapshot_sources(latest)
    if not missing:
        return latest
    enriched = copy.deepcopy(latest)
    data = enriched.setdefault("data", {})
    summary = data.setdefault("qmt_trading_summary", {})
    accounts = summary.setdefault("accounts", {})
    positions = list(data.get("qmt_positions", data.get("positions", [])) or [])
    stale_sources: Dict[str, str] = {}
    try:
        latest_date = date.fromisoformat(str(latest.get("as_of_date") or "")[:10])
    except Exception:
        latest_date = date.today()
    for source in missing:
        for candidate in candidates:
            if candidate.get("id") == latest.get("id"):
                continue
            try:
                candidate_date = date.fromisoformat(str(candidate.get("as_of_date") or "")[:10])
            except Exception:
                continue
            age = (latest_date - candidate_date).days
            if age < 0 or age > max_source_age_days:
                continue
            account, source_positions = _snapshot_source_payload(candidate, source)
            if account is None:
                continue
            accounts[source] = account
            positions = [item for item in positions if str(item.get("_source") or "") != source]
            positions.extend(source_positions)
            stale_sources[source] = candidate_date.isoformat()
            break
    if stale_sources:
        data["qmt_positions"] = positions
        summary["stale_sources"] = stale_sources
        enriched.setdefault("metadata", {})["stale_sources"] = stale_sources
    return enriched


def _select_risk_snapshot(max_fallback_age_days: int = 3) -> tuple[Dict[str, Any] | None, bool]:
    latest = db.get_latest_portfolio_snapshot(account_scope="combined")
    recent = _recent_combined_snapshots()
    if latest:
        latest = _enrich_partial_snapshot(latest, recent, max_source_age_days=max_fallback_age_days)
    if _snapshot_is_usable(latest):
        return latest, False
    latest_id = latest.get("id") if latest else None
    for candidate in recent:
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
    policy = load_advisor_policy()
    position_coverage = _position_coverage(positions, account_metrics)
    cash = float(account_metrics["cash"])
    effective_total = float(account_metrics["effective_total"])
    exposures = _aggregate_security_exposures(positions, set(account_metrics["stale_sources"]))
    sorted_positions = sorted(exposures, key=lambda item: float(item.get("market_value") or 0), reverse=True)
    top1_ratio = (float(sorted_positions[0]["market_value"]) / effective_total) if sorted_positions and effective_total else 0.0
    top3_mv = sum(float(p.get("market_value") or 0) for p in sorted_positions[:3])
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
    if top1_ratio >= float(policy["single_position_alert_ratio"]):
        flags.append("top1_concentration_high")
    if top3_ratio >= float(policy["top3_position_alert_ratio"]):
        flags.append("top3_concentration_high")
    if not position_coverage["complete"]:
        flags.append("position_coverage_incomplete")
    if not account_metrics["source_coverage_complete"]:
        flags.append("account_source_incomplete")
    if account_metrics["stale_sources"]:
        flags.append("stale_account_source")
    if total_pnl < 0:
        flags.append("portfolio_unrealized_loss")
    if any(bool(item.get("pnl_conflict")) for item in exposures):
        flags.append("position_pnl_conflict")
    if not flags:
        flags.append("no_major_snapshot_risk_flag")
    position_summaries = [
        {
            "code": str(p.get("code") or ""),
            "name": str(p.get("name") or p.get("code") or ""),
            "source": str((p.get("sources") or ["unknown"])[0]) if len(p.get("sources") or []) == 1 else "combined",
            "sources": list(p.get("sources") or []),
            "volume": int(p.get("volume") or 0),
            "available_volume": int(p.get("available_volume") or 0),
            "available_volume_complete": bool(p.get("available_volume_complete")),
            "stale_sources": list(p.get("stale_sources") or []),
            "account_positions": list(p.get("account_positions") or []),
            "market_value": round(float(p.get("market_value") or 0), 2),
            "cost_value": round(float(p.get("cost_value") or 0), 2),
            "weight": round((float(p.get("market_value") or 0) / effective_total) if effective_total else 0.0, 4),
            "pnl": round(float(p.get("pnl") or 0), 2),
            "pnl_ratio": round(float(p.get("pnl_ratio")), 4) if p.get("pnl_ratio") is not None else None,
            "pnl_bases": list(p.get("pnl_bases") or []),
            "pnl_conflict": bool(p.get("pnl_conflict")),
        }
        for p in sorted_positions
    ]
    report = {
        "available": True,
        "data_status": "fallback_snapshot" if fallback_snapshot else "current_snapshot",
        "fallback_snapshot": fallback_snapshot,
        "snapshot_created_at": str(snapshot.get("created_at") or ""),
        "as_of": as_of,
        "snapshot_age_days": age_days,
        "positions_count": len(exposures),
        "position_rows_count": len(positions),
        "total_market_value": round(total_mv, 2),
        "effective_total_asset": round(effective_total, 2),
        "cash": round(cash, 2),
        "cash_ratio": round(cash / effective_total, 4) if effective_total else 0.0,
        "cash_complete": bool(account_metrics["cash_complete"]),
        "invalid_account_sources": account_metrics["invalid_sources"],
        "account_source_coverage_complete": bool(account_metrics["source_coverage_complete"]),
        "missing_account_sources": account_metrics["missing_sources"],
        "stale_account_sources": account_metrics["stale_sources"],
        "position_coverage_complete": bool(position_coverage["complete"]),
        "position_market_value_gap": position_coverage["gap"],
        "position_coverage_mismatches": position_coverage["mismatches"],
        "reported_account_market_value": position_coverage["expected_market_value"],
        "total_unrealized_pnl": round(total_pnl, 2),
        "top1_ratio": round(top1_ratio, 4),
        "top3_ratio": round(top3_ratio, 4),
        "by_source": {k: {"count": v["count"], "market_value": round(v["market_value"], 2), "pnl": round(v["pnl"], 2)} for k, v in by_source.items()},
        # ``positions`` is the complete normalized inventory for decision
        # coverage. ``top_positions`` remains a compact display view.
        "positions": position_summaries,
        "top_positions": position_summaries[:8],
        "risk_flags": flags,
        "advisor_policy": policy,
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
        f"- {'快照记录' if report.get('fallback_snapshot') else '当前'}已知 {report.get('positions_count')} 条持仓明细，市值 {money(report.get('total_market_value'))}，浮动盈亏 {money(report.get('total_unrealized_pnl'))}。",
        "",
        "**仓位结构**",
        (
            f"- 现金 {money(report.get('cash'))}（{pct(report.get('cash_ratio', 0) * 100)}）；"
            if report.get("cash_complete")
            else f"- 可验证现金 {money(report.get('cash'))}（部分账户资产字段异常，不计算完整现金占比）；"
        )
        + (
            f"第一大持仓 {pct(report.get('top1_ratio', 0) * 100)}，前三大持仓 {pct(report.get('top3_ratio', 0) * 100)}。"
            if report.get("position_coverage_complete", True)
            else f"按已知明细计算第一大持仓 {pct(report.get('top1_ratio', 0) * 100)}、前三大持仓 {pct(report.get('top3_ratio', 0) * 100)}，不视为完整组合比例。"
        ),
    ]
    policy = report.get("advisor_policy") or {}
    lines.append(
        f"- 风险政策：单票预警 {pct(float(policy.get('single_position_alert_ratio', 0.30)) * 100)}，"
        f"前三持仓预警 {pct(float(policy.get('top3_position_alert_ratio', 0.70)) * 100)}。"
    )
    lines.append(
        f"- 亏损仓复核需同时满足权重 {pct(float(policy.get('loss_position_review_ratio', 0.18)) * 100)} 和累计回撤 "
        f"{pct(float(policy.get('loss_review_drawdown_ratio', 0.05)) * 100)}；累计回撤达到 "
        f"{pct(float(policy.get('severe_loss_drawdown_ratio', 0.20)) * 100)} 时不受仓位下限限制。"
    )
    if not report.get("position_coverage_complete", True):
        lines.append(
            f"- 持仓明细覆盖不完整：账户资产口径市值比明细合计多 {money(report.get('position_market_value_gap'))}；"
            "可能存在跨账户同代码被合并或明细缺失，修复前不输出精确总仓位。"
        )
    if report.get("invalid_account_sources"):
        lines.append(
            "- 账户资产降级："
            + join_cn(source_label(item) for item in report.get("invalid_account_sources") or [])
            + " 的接口缺失或资产值未通过合理性校验；集中度按可验证持仓市值与现金计算。"
        )
    if report.get("stale_account_sources"):
        lines.append(
            "- 账户持仓回退："
            + join_cn(
                f"{source_label(source)}使用 {as_of} 快照"
                for source, as_of in (report.get("stale_account_sources") or {}).items()
            )
            + "；与当前账户明细分开标注，不冒充实时持仓。"
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
        sources = join_cn((source_label(source) for source in p.get("sources") or []), source_label(p.get("source")))
        pnl_ratio = "收益率待核验" if p.get("pnl_ratio") is None else f"收益率 {pct(float(p['pnl_ratio']) * 100, signed=True)}"
        lines.append(f"- **{p['name']}（{p['code']}）**｜仓位 {pct(p['weight']*100)}｜市值 {money(p['market_value'])}｜盈亏 {money(p['pnl'])}｜{pnl_ratio}｜{sources}")
    lines.extend(["", "**执行原则**"])
    if report.get("top1_ratio", 0) >= float(policy.get("single_position_alert_ratio", 0.30)):
        lines.append("- 优先降低单票集中度；弱于所属板块且无放量承接时，不用补仓摊低成本。")
    if report.get("total_unrealized_pnl", 0) < 0:
        lines.append("- 组合浮亏阶段先控制回撤，再考虑新增高波动仓位。")
    if not any((report.get("top1_ratio", 0) >= float(policy.get("single_position_alert_ratio", 0.30)), report.get("total_unrealized_pnl", 0) < 0)):
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

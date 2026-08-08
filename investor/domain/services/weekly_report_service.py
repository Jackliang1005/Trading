
#!/usr/bin/env python3
"""Weekly assistant report for OpenClaw Investor."""

from __future__ import annotations

import contextlib
import io
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

import db
from domain.services.longterm_portfolio_service import (
    build_longterm_snapshot_text,
    load_longterm_snapshot,
    longterm_plan_status,
    summarize_longterm_snapshot,
)
from domain.services.report_style_service import event_impact_label, event_summary_cn, join_cn, pct, theme_label, trend_label
from domain.services.event_service import RawEvent, _near_duplicate_title, analyze_event
from domain.services.concept_momentum_service import THEME_CONCEPT_KEYWORDS, build_concept_momentum_candidates
from domain.services.evolution_service import EVOLUTION_EVIDENCE_PROFILES, load_strategy_config

WORKSPACE = Path("/root/.openclaw/workspace")
REPORTS_DIR = WORKSPACE / "reports"


def _u(value: str) -> str:
    return value


def _init_db_quietly() -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        db.init_db()


def _parse_dt(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if "%H" in fmt else text[:10], fmt)
        except Exception:
            continue
    return None


def _week_range(days: int = 7, end: date | None = None) -> Tuple[date, date]:
    end_date = end or date.today()
    start_date = end_date - timedelta(days=max(1, days) - 1)
    return start_date, end_date


def _load_week_events(start: date, end: date, limit: int = 200) -> List[Dict[str, Any]]:
    _init_db_quietly()
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time())
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, captured_at, data FROM market_snapshots WHERE snapshot_type='event_alert' ORDER BY captured_at DESC LIMIT ?",
        (max(1, limit),),
    ).fetchall()
    conn.close()
    events: List[Dict[str, Any]] = []
    for row in rows:
        captured = _parse_dt(row["captured_at"])
        if captured and not (start_dt <= captured < end_dt):
            continue
        try:
            payload = json.loads(row["data"])
        except Exception:
            continue
        published = _parse_dt(payload.get("published_at", ""))
        if published and not (start_dt <= published < end_dt):
            continue
        payload["_snapshot_id"] = row["id"]
        payload["_captured_at"] = row["captured_at"]
        events.append(payload)
    return events


def _summarize_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    unique: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_titles: List[str] = []
    for stored_event in events:
        event = dict(stored_event)
        # Historical rows may carry labels produced by an older keyword rule.
        # Reclassify visible weekly content with the current context-aware model.
        refreshed = analyze_event(
            RawEvent(
                title=str(event.get("title") or ""),
                summary=str(event.get("summary") or ""),
                url=str(event.get("url") or ""),
                source=str(event.get("source") or ""),
                published_at=str(event.get("published_at") or ""),
            ),
            positions=[],
        )
        if refreshed.get("is_multi_story_digest"):
            continue
        for key in ("themes", "related_stocks", "score", "severity"):
            event[key] = refreshed.get(key)
        identity = str(event.get("event_id") or "").strip()
        title = str(event.get("title") or "").strip()
        if (identity and identity in seen_ids) or any(_near_duplicate_title(title, previous) for previous in seen_titles):
            continue
        if identity:
            seen_ids.add(identity)
        if title:
            seen_titles.append(title)
        unique.append(event)
    theme_counts: Counter[str] = Counter()
    severity_counts: Counter[str] = Counter()
    for event in unique:
        severity_counts[str(event.get("severity") or "unknown")] += 1
        for theme in event.get("themes", []) or []:
            name = str(theme.get("theme") or "").strip()
            if name:
                theme_counts[name] += 1
    top_events: List[Dict[str, Any]] = []
    seen_theme_sets: set[tuple[str, ...]] = set()
    for event in sorted(unique, key=lambda e: int(e.get("score", 0) or 0), reverse=True):
        signature = tuple(sorted(str(item.get("theme") or "") for item in event.get("themes", []) or [] if item.get("theme")))
        if signature and signature in seen_theme_sets:
            continue
        seen_theme_sets.add(signature)
        top_events.append(event)
        if len(top_events) >= 5:
            break
    return {
        "count": len(unique),
        "theme_counts": theme_counts.most_common(8),
        "severity_counts": severity_counts.most_common(),
        "top_events": top_events,
    }


def _summarize_predictions(start: date, end: date) -> Dict[str, Any]:
    _init_db_quietly()
    rows = db.get_checked_predictions_in_range(start.isoformat(), end.isoformat())
    total = len(rows)
    correct = sum(1 for item in rows if item.get("is_correct"))
    config = load_strategy_config()
    min_total = int(config.get("min_evolution_samples", 20) or 20)
    min_per_strategy = int(config.get("min_strategy_samples", 5) or 5)
    min_strategies = int(config.get("min_evolution_strategies", 2) or 2)
    by_strategy: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "correct": 0})
    for item in rows:
        strategy = str(item.get("strategy_used") or "").strip()
        expected_profile = EVOLUTION_EVIDENCE_PROFILES.get(strategy, "")
        if (
            not expected_profile
            or str(item.get("evidence_profile") or "").strip() != expected_profile
            or not str(item.get("prediction_run_id") or "").strip()
        ):
            continue
        by_strategy[strategy]["total"] += 1
        if item.get("is_correct"):
            by_strategy[strategy]["correct"] += 1
    qualified_total = sum(stat["total"] for stat in by_strategy.values())
    qualified_correct = sum(stat["correct"] for stat in by_strategy.values())
    eligible = [name for name, stat in by_strategy.items() if stat["total"] >= min_per_strategy]
    comparison_ready = qualified_total >= min_total and len(eligible) >= min_strategies
    strategies = []
    for name, evidence_profile in EVOLUTION_EVIDENCE_PROFILES.items():
        stat = by_strategy[name]
        t = stat["total"]
        c = stat["correct"]
        strategies.append(
            {
                "strategy": name,
                "evidence_profile": evidence_profile,
                "total": t,
                "correct": c,
                "minimum": min_per_strategy,
                "eligible": t >= min_per_strategy,
                "win_rate": round(c / t * 100, 1) if comparison_ready and t >= min_per_strategy else None,
            }
        )
    return {
        "total": total,
        "correct": correct,
        "win_rate": round(correct / total * 100, 1) if total else 0.0,
        "qualified_total": qualified_total,
        "qualified_correct": qualified_correct,
        "unqualified_total": total - qualified_total,
        "minimum_total": min_total,
        "minimum_per_strategy": min_per_strategy,
        "minimum_strategies": min_strategies,
        "eligible_strategies": eligible,
        "strategy_comparison_ready": comparison_ready,
        "strategies": strategies,
    }


def _summarize_intraday_predictions(start: date, end: date, reports_dir: Path = REPORTS_DIR) -> Dict[str, Any]:
    """Summarize the production 09:30→10:30→14:30 validation ledger.

    The 14:30 snapshot repeats the 09:30 correction already present at 10:30,
    so only the latest available snapshot for each trade date is counted.
    These composite market-direction checks are deliberately kept separate
    from strategy-attributed samples used for weight evolution.
    """
    samples: List[Dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        payload = None
        source_slot = ""
        compact = cursor.strftime("%Y%m%d")
        for slot in ("1430", "1030"):
            path = reports_dir / f"investor_intraday_outlook_{compact}_{slot}.json"
            if not path.exists():
                continue
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                source_slot = slot
                break
        if payload:
            seen_slots: set[str] = set()
            for item in payload.get("corrections") or []:
                slot = str(item.get("slot") or "").strip()
                if not slot or slot in seen_slots:
                    continue
                seen_slots.add(slot)
                result = str(item.get("result") or "数据不足，无法验证")
                samples.append(
                    {
                        "date": cursor.isoformat(),
                        "slot": slot,
                        "source_slot": source_slot,
                        "predicted": str(item.get("predicted") or "unknown"),
                        "observed": str(item.get("observed") or "unknown"),
                        "result": result,
                    }
                )
        cursor += timedelta(days=1)

    exact = sum(1 for item in samples if item["result"] == "方向正确")
    close = sum(1 for item in samples if item["result"] == "方向接近但强度有偏差")
    wrong = sum(1 for item in samples if item["result"] == "方向错误")
    unavailable = len(samples) - exact - close - wrong
    evaluated = exact + close + wrong
    return {
        "total": evaluated,
        "correct": exact,
        "close": close,
        "wrong": wrong,
        "unavailable": unavailable,
        "exact_rate": round(exact / evaluated * 100, 1) if evaluated else 0.0,
        "usable_rate": round((exact + close) / evaluated * 100, 1) if evaluated else 0.0,
        "days": len({item["date"] for item in samples}),
        "samples": samples,
        "attributable_to_strategy": False,
    }


def _load_audit_summary() -> Dict[str, Any]:
    path = REPORTS_DIR / "investor_assistant_capability_audit_latest.json"
    if not path.exists():
        return {"available": False, "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "path": str(path), "error": str(exc)}
    blocked = [item for item in payload.get("items", []) if item.get("status") == "blocked"]
    warnings = [item for item in payload.get("items", []) if item.get("status") == "warn"]
    return {
        "available": True,
        "overall": payload.get("overall"),
        "blocked_count": payload.get("blocked_count", len(blocked)),
        "warning_count": payload.get("warning_count", len(warnings)),
        "blocked_items": [item.get("name") for item in blocked],
        "warning_items": [item.get("name") for item in warnings],
        "generated_at": payload.get("generated_at", ""),
    }


def _load_longterm_summary() -> Dict[str, Any]:
    summary = summarize_longterm_snapshot(load_longterm_snapshot())
    return {"summary": summary, "text": build_longterm_snapshot_text(summary)}


def build_weekly_report(days: int = 7, end_date: str = "") -> Dict[str, Any]:
    end = date.fromisoformat(end_date) if end_date else date.today()
    start, end = _week_range(days=days, end=end)
    events = _load_week_events(start, end)
    event_summary = _summarize_events(events)
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "events": event_summary,
        "concept_momentum": build_concept_momentum_candidates(
            event_summary.get("theme_counts") or [],
            report_date=end.isoformat(),
            top_n=5,
        ),
        "predictions": _summarize_predictions(start, end),
        "intraday_predictions": _summarize_intraday_predictions(start, end),
        "longterm": _load_longterm_summary(),
        "audit": _load_audit_summary(),
    }
    report["text"] = format_weekly_report(report)
    return report



def _fmt_pairs(items: List[Tuple[str, int]], empty: str) -> str:
    if not items:
        return empty
    return "、".join(f"{name}（{count}条）" for name, count in items)


def _candidate_theme_chain(item: Dict[str, Any], momentum: Dict[str, Any]) -> str:
    concepts = [str(value or "") for value in item.get("concepts") or []]
    themes = []
    for theme in momentum.get("themes") or []:
        keywords = THEME_CONCEPT_KEYWORDS.get(str(theme), [])
        if any(keyword.lower() in concept.lower() for keyword in keywords for concept in concepts):
            themes.append(theme_label(theme))
    return f"{join_cn(themes, '主题待确认')} → {join_cn(concepts, '概念待确认')}"


def _display_compact_date(value: object) -> str:
    raw = "".join(char for char in str(value or "") if char.isdigit())
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) >= 8 else "未取得"


def format_weekly_report(report: Dict[str, Any]) -> str:
    events = report.get("events", {}) or {}
    predictions = report.get("predictions", {}) or {}
    intraday = report.get("intraday_predictions", {}) or {}
    longterm = report.get("longterm", {}) or {}
    verified = int(predictions.get("total", 0) or 0)
    qualified = int(predictions.get("qualified_total", 0) or 0)
    if verified:
        prediction_summary = (
            f"市场方向已验证 {verified} 条，整体正确率 {predictions.get('win_rate', 0)}%。"
        )
    elif int(intraday.get("total", 0) or 0):
        prediction_summary = (
            f"日内方向已验证 {intraday.get('total')} 次，精确方向正确率 {intraday.get('exact_rate')}%；"
            "暂无可归因到单一策略维度的样本，因此不据此调整策略权重。"
        )
    else:
        prediction_summary = "本周暂无完成验证的预测样本，不把零样本写成 0% 正确率。"
    prediction_detail = (
        f"市场预测已验证 {verified} 条，正确 {predictions.get('correct', 0)} 条，整体正确率 {predictions.get('win_rate', 0)}%。"
        if verified
        else "策略归因样本：暂无；本周不输出分策略胜率结论。"
    )
    lines = [
        "📅 OpenClaw A股投资周报",
        f"统计区间：{report.get('period_start')} 至 {report.get('period_end')}",
        "",
        "**本周结论**",
        f"- 本周主线按新鲜事件聚合并去重；{prediction_summary}",
        "- 周报只总结已落地数据；策略归因样本不足时不评价策略优劣。",
        "",
        "**事件与主线**",
        f"- 主题热度：{_fmt_pairs([(theme_label(name), count) for name, count in events.get('theme_counts', [])], '暂无有效主题')}。",
    ]
    top_events = events.get("top_events", []) or []
    if top_events:
        for event in top_events[:5]:
            themes = join_cn((theme_label(t.get("theme")) for t in (event.get("themes") or [])[:3]), "待确认")
            lines.append(f"- **{event_summary_cn(event.get('title'), (t.get('theme') for t in event.get('themes') or []))}**｜{themes}｜{event_impact_label(event.get('severity'))}")
    else:
        lines.append("- 本周没有通过质量阈值的重点事件。")

    momentum = report.get("concept_momentum") or {}
    candidates = momentum.get("candidates") or []
    lines.extend(["", "**主题到个股验证**"])
    if candidates:
        for item in candidates[:5]:
            lines.append(
                f"- **{item.get('name')}（{item.get('code')}）**｜{_candidate_theme_chain(item, momentum)}"
            )
            lines.append(
                f"  动量：{trend_label(item.get('trend'))}｜20日 {pct(item.get('momentum_20d'), signed=True)}｜60日 {pct(item.get('momentum_60d'), signed=True)}；仅列入下周观察，开盘后还需量价确认。"
            )
        lines.append(
            f"- 数据日：概念 {_display_compact_date(momentum.get('concept_date'))}｜动量 {_display_compact_date(momentum.get('momentum_date'))}。"
        )
    else:
        lines.append("- 本周主题尚未形成同时通过概念强弱与20/60日动量的候选，不为凑数推荐个股。")

    lines.extend([
        "",
        "**预测复盘**",
        f"- {prediction_detail}",
    ])
    intraday_total = int(intraday.get("total", 0) or 0)
    if intraday_total:
        lines.append(
            f"- 日内方向闭环：{intraday.get('days', 0)} 个交易日、{intraday_total} 次验证；"
            f"方向正确 {intraday.get('correct', 0)} 次、接近 {intraday.get('close', 0)} 次、错误 {intraday.get('wrong', 0)} 次；"
            f"精确方向正确率 {intraday.get('exact_rate', 0)}%，含接近结果的可用率 {intraday.get('usable_rate', 0)}%。"
        )
        lines.append("- 日内方向结果属于复合市场判断，未归因到技术面、基本面、情绪面或地缘维度，不进入权重更新。")
    strategies = predictions.get("strategies", []) or []
    strategy_labels = {"technical": "技术", "sentiment": "情绪", "fundamental": "基本面", "geopolitical": "地缘"}
    if predictions.get("strategy_comparison_ready"):
        comparable = [item for item in strategies if item.get("eligible") and item.get("win_rate") is not None]
        lines.append(
            "- 画像化分策略："
            + "；".join(
                f"{strategy_labels.get(str(item.get('strategy')), item.get('strategy'))} "
                f"{item.get('correct')}/{item.get('total')}（{item.get('win_rate')}%）"
                for item in comparable
            )
        )
    elif qualified:
        progress = "、".join(
            f"{strategy_labels.get(str(item.get('strategy')), item.get('strategy'))}"
            f"{item.get('total', 0)}/{item.get('minimum', predictions.get('minimum_per_strategy', 5))}"
            for item in strategies
        )
        lines.append(
            f"- 策略归因仍在采样：本周画像化 {qualified}/{predictions.get('minimum_total', 20)}；{progress}。"
            "未达到完整门槛，不输出分策略胜率或优劣排序。"
        )
    elif verified:
        lines.append(
            f"- 策略归因样本：暂无；本周 {predictions.get('unqualified_total', verified)} 条属于历史或未画像化市场预测，"
            "只计入总体方向复盘，不输出分策略胜率。"
        )

    lines.extend([
        "",
        "**长线组合**",
        _format_longterm_weekly(longterm.get("summary") or {}),
        "",
        "**下周行动**",
        "- 候选必须经过“事件主题 → 概念板块 → 个股动量 → 开盘量价确认”，不直接按新闻标题追涨。",
        "- 优先处理集中度、组合回撤和数据过期；实时账户链路降级时不生成交易完成结论。",
        "",
        "**数据边界**",
    ])
    lines.append(f"- 统计区间为 {report.get('period_start')} 至 {report.get('period_end')}；报告生成时间 {report.get('generated_at')}。")
    lines.append("- 实时账户或行情链路降级时，只保留已落地历史数据，不推断交易已经完成。")
    return "\n".join(lines)


def _format_longterm_weekly(summary: Dict[str, Any]) -> str:
    if not summary.get("available"):
        return "- 长线组合快照不可用。"
    nav = float(summary.get("nav", 0) or 0)
    cash = float(summary.get("cash", 0) or 0)
    cash_ratio = float(summary.get("cash_ratio", 0) or 0) * 100
    holdings = int(summary.get("holdings_count", summary.get("holdings", 0)) or 0)
    rejected = int(summary.get("rejected_actions_count", summary.get("rejected_actions", summary.get("rejected", 0))) or 0)
    return f"- 数据日 {summary.get('as_of') or '未知'}；净值 {nav:,.2f}，现金 {cash:,.2f}（{cash_ratio:.1f}%），持仓 {holdings} 只；{longterm_plan_status(summary)}，风控拒绝 {rejected} 笔。"

def save_weekly_report(report: Dict[str, Any]) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"investor_weekly_report_{stamp}.md"
    latest = REPORTS_DIR / "investor_weekly_report_latest.md"
    path.write_text(str(report.get("text", "")), encoding="utf-8")
    latest.write_text(str(report.get("text", "")), encoding="utf-8")
    json_path = REPORTS_DIR / "investor_weekly_report_latest.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(path)

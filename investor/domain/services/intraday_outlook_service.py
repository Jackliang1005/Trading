#!/usr/bin/env python3
"""09:30 -> 10:30 -> 14:30 evidence-backed intraday prediction loop."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import db
from domain.services.decision_monitor_service import build_decision_monitor
from domain.services.report_style_service import money, pct, risk_label, source_label


REPORTS_DIR = Path("/root/.openclaw/workspace/reports")
BENCHMARK_NAMES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
}


def _slot(value: str) -> str:
    raw = str(value or "").replace(":", "").strip()
    if raw not in {"0930", "1030", "1430"}:
        raise ValueError("slot must be 0930, 1030 or 1430")
    return raw


def _market_state(monitor: Dict[str, Any], current: datetime | None = None, require_fresh: bool = False) -> Dict[str, Any]:
    now = current or datetime.now()
    if require_fresh and (not monitor.get("calendar_open") or not monitor.get("trading_session")):
        return {"available": False, "reason": "非交易时段或非交易日", "benchmarks": [], "direction": "unknown", "level": "不可判断"}
    rows = []
    for code, quote in (monitor.get("benchmarks") or {}).items():
        if not quote.get("available") or not quote.get("change_available"):
            continue
        as_of = str(quote.get("as_of") or "").replace("-", "").replace("/", "")
        if require_fresh and (not as_of or not as_of.startswith(now.strftime("%Y%m%d"))):
            continue
        rows.append({"code": code, "name": BENCHMARK_NAMES.get(code, code), "change_pct": float(quote.get("change_pct") or 0)})
    changes = [item["change_pct"] for item in rows]
    average = sum(changes) / len(changes) if changes else None
    down_count = sum(1 for value in changes if value <= -1)
    severe_down_count = sum(1 for value in changes if value <= -2)
    up_count = sum(1 for value in changes if value >= 1)
    if average is None:
        direction, level = "unknown", "不可判断"
    elif average <= -1.5 or severe_down_count >= 3:
        direction, level = "down", "明显偏弱"
    elif average <= -0.4 or down_count >= 3:
        direction, level = "down", "偏弱"
    elif average >= 0.8 or up_count >= 3:
        direction, level = "up", "偏强"
    else:
        direction, level = "sideways", "震荡"
    return {
        "available": len(rows) >= 2,
        "reason": "" if len(rows) >= 2 else "当日宽基指数行情不足",
        "benchmarks": rows,
        "average_change_pct": round(average, 3) if average is not None else None,
        "down_count": down_count,
        "severe_down_count": severe_down_count,
        "direction": direction,
        "level": level,
    }


def _snapshot_path(trade_date: str, slot: str, reports_dir: Path) -> Path:
    return reports_dir / f"investor_intraday_outlook_{trade_date.replace('-', '')}_{slot}.json"


def _load_snapshot(trade_date: str, slot: str, reports_dir: Path) -> Dict[str, Any] | None:
    path = _snapshot_path(trade_date, slot, reports_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _prediction_result(predicted: str, observed: str) -> str:
    if predicted == "unknown" or observed == "unknown":
        return "数据不足，无法验证"
    if predicted == observed:
        return "方向正确"
    if "sideways" in {predicted, observed}:
        return "方向接近但强度有偏差"
    return "方向错误"


def _prediction_confidence(market: Dict[str, Any]) -> float:
    """Return a conservative confidence for the technical persistence baseline."""
    benchmark_count = len(market.get("benchmarks") or [])
    strength = min(abs(float(market.get("average_change_pct") or 0)) / 2.0, 1.0)
    coverage = min(max(benchmark_count - 2, 0) / 2.0, 1.0)
    return round(min(0.75, 0.50 + 0.10 * strength + 0.10 * coverage), 2)


def _record_intraday_prediction(report: Dict[str, Any], existing: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
    """Persist one idempotent, strategy-attributed forecast for later evaluation."""
    direction = str(report.get("prediction_direction") or "unknown")
    slot = str(report.get("slot") or "")
    if slot not in {"0930", "1030"} or direction == "unknown":
        return None
    existing_record = (existing or {}).get("prediction_record") or {}
    if existing_record.get("id"):
        return existing_record
    market = report.get("market") or {}
    trade_date = str(report.get("trade_date") or "")
    attribution = report.get("prediction_attribution") or {}
    strategy = str(attribution.get("strategy_used") or "technical")
    basis = str(attribution.get("basis") or "宽基指数当日涨跌、方向一致性与时段延续性")
    confidence = float(attribution.get("confidence") or _prediction_confidence(market))
    prediction_id = db.add_prediction(
        target=f"intraday_market:{trade_date}:{slot}",
        target_name=f"A股宽基日内方向 {slot}",
        direction=direction,
        confidence=confidence,
        reasoning=basis,
        strategy_used=strategy,
        model_used="intraday-market-state-v1",
        timeframe="intraday",
        actual_price=None,
    )
    return {
        "id": prediction_id,
        "strategy_used": strategy,
        "basis": basis,
        "confidence": confidence,
    }


def _evaluate_recorded_prediction(correction: Dict[str, Any], previous: Dict[str, Any] | None, market: Dict[str, Any]) -> None:
    record = (previous or {}).get("prediction_record") or {}
    prediction_id = int(record.get("id") or 0)
    result = str(correction.get("result") or "")
    if not prediction_id or result == "数据不足，无法验证":
        return
    score = {"方向正确": 1.0, "方向接近但强度有偏差": 0.5, "方向错误": 0.0}.get(result, 0.0)
    actual_change = float(market.get("average_change_pct") or 0)
    db.update_prediction_result(
        prediction_id,
        actual_price=0.0,
        actual_change=actual_change,
        is_correct=result == "方向正确",
        score=score,
        note=f"日内闭环：{correction.get('slot')} 预测，当前观察为 {correction.get('observed')}；{result}",
    )
    correction["prediction_id"] = prediction_id
    correction["strategy_used"] = str(record.get("strategy_used") or "technical")
    correction["score"] = score


def _clearance_assessment(monitor: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    positions = monitor.get("tracked_positions") or []
    if not market.get("available") or not monitor.get("trading_session") or not monitor.get("calendar_open"):
        return {
            "need_clear": False,
            "answer": "无法判断：缺少当日交易时段的宽基指数与逐仓证据，不给清仓结论。",
            "weak_count": 0,
            "position_count": len(positions),
            "weak_ratio": 0.0,
            "weak_codes": [],
            "severe_market": False,
        }
    weak = []
    for item in positions:
        quote = item.get("quote") or {}
        change = float(quote.get("change_pct") or 0) if quote.get("change_available") else None
        relative = item.get("relative_change_pct")
        if (change is not None and change <= -2) or (relative is not None and float(relative) <= -1.5):
            weak.append(item)
    weak_ratio = len(weak) / len(positions) if positions else 0.0
    severe_market = bool(
        market.get("available")
        and (float(market.get("average_change_pct") or 0) <= -2 or int(market.get("severe_down_count") or 0) >= 3)
    )
    need_clear = severe_market and len(positions) > 0 and weak_ratio >= 0.6
    if need_clear:
        answer = "需要进入清仓评估：先清理已明显走弱的交易仓，高集中持仓若无承接则继续降权；不做无差别市价清仓。"
    elif severe_market:
        answer = "暂不建议无差别清仓：市场明显偏弱，但逐仓触发不足；先降低弱势仓和集中度。"
    else:
        answer = "当前不需要整体清仓：继续逐仓执行止损与相对强弱纪律。"
    return {
        "need_clear": need_clear,
        "answer": answer,
        "weak_count": len(weak),
        "position_count": len(positions),
        "weak_ratio": round(weak_ratio, 3),
        "weak_codes": [item.get("code") for item in weak if item.get("code")],
        "severe_market": severe_market,
    }


def build_intraday_outlook(slot: str, now: datetime | None = None, reports_dir: Path = REPORTS_DIR, save: bool = True) -> Dict[str, Any]:
    normalized = _slot(slot)
    current = now or datetime.now()
    trade_date = current.strftime("%Y-%m-%d")
    monitor = build_decision_monitor(slot=f"{normalized[:2]}:{normalized[2:]} 日内预测")
    market = _market_state(monitor, current=current, require_fresh=True)
    previous_0930 = _load_snapshot(trade_date, "0930", reports_dir)
    previous_1030 = _load_snapshot(trade_date, "1030", reports_dir)
    existing_current = {"0930": previous_0930, "1030": previous_1030}.get(normalized)

    if not market.get("available"):
        prediction = "unknown"
        prediction_text = "当日宽基指数行情不足或当前不是交易时段，本次不预测方向。"
    elif normalized == "0930":
        prediction = market.get("direction", "unknown")
        prediction_text = f"预计当日市场以{market.get('level')}为主；09:45后用宽基指数和持仓相对强弱验证。"
    elif normalized == "1030":
        prediction = market.get("direction", "unknown")
        prediction_text = f"根据10:30实际走势，预计午后延续{market.get('level')}格局；若指数方向反转则再次降级。"
    else:
        prediction = market.get("direction", "unknown")
        prediction_text = f"14:30市场状态为{market.get('level')}，尾盘以风险处置和预测复盘为主，不再追逐新题材。"

    corrections = []
    history = ()
    if normalized == "1030":
        history = (("09:30", previous_0930),)
    elif normalized == "1430":
        history = (("09:30", previous_0930), ("10:30", previous_1030))
    for label, previous in history:
        if previous:
            predicted = str(previous.get("prediction_direction") or "unknown")
            corrections.append({"slot": label, "predicted": predicted, "observed": market.get("direction"), "result": _prediction_result(predicted, str(market.get("direction")))})

    clearance = _clearance_assessment(monitor, market) if normalized == "1430" else {}
    report = {
        "slot": normalized,
        "trade_date": trade_date,
        "generated_at": current.strftime("%Y-%m-%d %H:%M:%S"),
        "market": market,
        "prediction_direction": prediction,
        "prediction_text": prediction_text,
        "corrections": corrections,
        "clearance": clearance,
        "monitor": monitor,
    }
    if normalized in {"0930", "1030"} and prediction != "unknown":
        report["prediction_attribution"] = {
            "strategy_used": "technical",
            "basis": "宽基指数当日涨跌、方向一致性与时段延续性",
            "confidence": _prediction_confidence(market),
        }
    if save:
        previous_by_label = {"09:30": previous_0930, "10:30": previous_1030}
        for correction in corrections:
            previous = previous_by_label.get(str(correction.get("slot") or ""))
            _evaluate_recorded_prediction(correction, previous, market)
        prediction_record = _record_intraday_prediction(report, existing=existing_current)
        if prediction_record:
            report["prediction_record"] = prediction_record
        reports_dir.mkdir(parents=True, exist_ok=True)
        path = _snapshot_path(trade_date, normalized, reports_dir)
        report["text"] = format_intraday_outlook(report)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        report["report_path"] = str(path)
    else:
        report["text"] = format_intraday_outlook(report)
    return report


def format_intraday_outlook(report: Dict[str, Any]) -> str:
    slot = str(report.get("slot") or "")
    market = report.get("market") or {}
    monitor = report.get("monitor") or {}
    title = {"0930": "09:30 开盘预测", "1030": "10:30 走势修正", "1430": "14:30 预测复盘与仓位决策"}.get(slot, "日内预测")
    lines = [f"📈 {title}", f"时间：{report.get('generated_at')}", "", "**市场结论**", f"- {report.get('prediction_text')}"]
    if market.get("available"):
        index_text = "；".join(f"{item.get('name')} {float(item.get('change_pct') or 0):+.2f}%" for item in market.get("benchmarks") or [])
        lines.append(f"- 宽基指数：{index_text}；平均 {float(market.get('average_change_pct') or 0):+.2f}%。")
    else:
        lines.append("- 宽基指数行情不可用，本次预测按不可验证处理。")
    attribution = report.get("prediction_attribution") or {}
    if attribution:
        lines.append(
            f"- 预测归因：技术面（置信度 {float(attribution.get('confidence') or 0):.0%}）；"
            f"依据为{attribution.get('basis')}。"
        )

    corrections = report.get("corrections") or []
    if corrections:
        lines.extend(["", "**预测修正**"])
        for item in corrections:
            attribution_text = f"｜归因 {item.get('strategy_used')}" if item.get("strategy_used") else ""
            lines.append(f"- {item.get('slot')} 预测：{item.get('result')}{attribution_text}。")

    positions = monitor.get("tracked_positions") or []
    lines.extend(["", "**持仓处理**"])
    if not monitor.get("trading_session") or not monitor.get("calendar_open"):
        lines.append("- 当前不是交易时段，不采用上一交易日涨跌生成逐仓动作。")
    elif not positions:
        lines.append("- 没有可用的逐仓行情，不生成减仓或清仓结论。")
    for item in positions[:8] if monitor.get("trading_session") and monitor.get("calendar_open") else []:
        quote = item.get("quote") or {}
        change = f"{float(quote.get('change_pct') or 0):+.2f}%" if quote.get("change_available") else "行情不足"
        lines.append(f"- **{item.get('name')}（{item.get('code')}）**｜仓位 {pct(float(item.get('weight') or 0)*100)}｜日内 {change}｜{source_label(item.get('source'))}")
        lines.append(f"  建议：{item.get('suggestion') or '等待有效行情。'}")

    if slot == "1430":
        clearance = report.get("clearance") or {}
        lines.extend(["", "**是否需要清仓**", f"- {clearance.get('answer')}"])
        if clearance.get("weak_codes"):
            lines.append("- 已明显走弱：" + "、".join(str(code) for code in clearance.get("weak_codes") or []))

    lines.extend(["", "**风险与数据状态**"])
    flags = [risk_label(flag) for flag in monitor.get("risk_flags") or []]
    if flags:
        lines.append("- " + "、".join(flags) + "。")
    if not monitor.get("trading_session") or not monitor.get("calendar_open"):
        lines.append("- 非交易时段：已拒绝使用上一交易日行情作实时判断。")
    elif monitor.get("quote_error"):
        lines.append("- 部分实时行情链路降级；缺失标的不生成价格触发结论。")
    else:
        lines.append(f"- 已读取 {monitor.get('quote_available_count', 0)} 条行情；本报告不会自动下单。")
    return "\n".join(lines)

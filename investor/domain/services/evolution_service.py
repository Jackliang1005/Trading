#!/usr/bin/env python3
"""Evolution services."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Dict, List

import db
from domain.policies.confidence_policy import calculate_rule_confidence, should_disable_rule

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
STRATEGY_CONFIG_PATH = os.path.join(BASE_DIR, "data", "strategy_config.json")
PROMPT_TEMPLATE_PATH = os.path.join(BASE_DIR, "data", "system_prompt.md")

DEFAULT_STRATEGY_CONFIG = {
    "version": 2,
    "updated_at": "",
    "weights": {
        "technical": 0.30,
        "fundamental": 0.25,
        "sentiment": 0.20,
        "geopolitical": 0.25,
    },
    "weight_history": [],
    "auto_adjust_enabled": True,
    "min_weight": 0.10,
    "max_weight": 0.60,
    "adjust_step": 0.05,
    "min_evolution_samples": 20,
    "min_strategy_samples": 5,
    "min_evolution_strategies": 2,
}

EVOLUTION_EVIDENCE_PROFILES = {
    "technical": "technical_price_regime_v1",
    "sentiment": "sentiment_flow_news_v1",
}


def load_strategy_config() -> Dict:
    if os.path.exists(STRATEGY_CONFIG_PATH):
        with open(STRATEGY_CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        if config.get("version", 1) < 2:
            if "geopolitical" not in config.get("weights", {}):
                old_weights = config["weights"].copy()
                config["weights"] = {
                    "technical": 0.30,
                    "fundamental": 0.25,
                    "sentiment": 0.20,
                    "geopolitical": 0.25,
                }
                config["version"] = 2
                config.setdefault("weight_history", []).append(
                    {
                        "date": datetime.now().strftime("%Y-%m-%d"),
                        "old_weights": old_weights,
                        "new_weights": config["weights"].copy(),
                        "reason": "v1→v2迁移：新增geopolitical策略维度",
                    }
                )
                save_strategy_config(config)
                print("  📌 策略配置已从v1迁移到v2，新增geopolitical维度")
            else:
                config["version"] = 2
        return config
    return DEFAULT_STRATEGY_CONFIG.copy()


def save_strategy_config(config: Dict):
    os.makedirs(os.path.dirname(STRATEGY_CONFIG_PATH), exist_ok=True)
    config["updated_at"] = datetime.now().isoformat()
    with open(STRATEGY_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _bounded_normalize(weights: Dict[str, float], min_weight: float, max_weight: float) -> Dict[str, float]:
    """Project weights onto a bounded simplex without violating limits."""
    result = {name: max(min_weight, min(max_weight, float(value))) for name, value in weights.items()}
    for _ in range(20):
        delta = 1.0 - sum(result.values())
        if abs(delta) < 1e-9:
            break
        eligible = [
            name
            for name, value in result.items()
            if (delta > 0 and value < max_weight - 1e-9) or (delta < 0 and value > min_weight + 1e-9)
        ]
        if not eligible:
            break
        share = delta / len(eligible)
        for name in eligible:
            result[name] = max(min_weight, min(max_weight, result[name] + share))
    rounded = {name: round(value, 3) for name, value in result.items()}
    residual = round(1.0 - sum(rounded.values()), 3)
    if residual:
        candidates = sorted(
            rounded,
            key=lambda name: (max_weight - rounded[name]) if residual > 0 else (rounded[name] - min_weight),
            reverse=True,
        )
        for name in candidates:
            candidate = round(rounded[name] + residual, 3)
            if min_weight <= candidate <= max_weight:
                rounded[name] = candidate
                break
    return rounded


def _rebalance_eligible_weights(
    weights: Dict[str, float],
    adjustments: Dict[str, float],
    eligible_names: List[str],
    min_weight: float,
    max_weight: float,
) -> Dict[str, float]:
    """Reallocate only inside the evidence-qualified subset, preserving its mass."""
    result = {name: float(value) for name, value in weights.items()}
    names = [name for name in eligible_names if name in result]
    if len(names) < 2:
        return {name: round(value, 3) for name, value in result.items()}

    target_mass = sum(result[name] for name in names)
    proposed = {
        name: max(min_weight, min(max_weight, result[name] + float(adjustments.get(name, 0) or 0)))
        for name in names
    }
    for _ in range(20):
        delta = target_mass - sum(proposed.values())
        if abs(delta) < 1e-9:
            break
        candidates = [
            name for name in names
            if (delta > 0 and proposed[name] < max_weight - 1e-9)
            or (delta < 0 and proposed[name] > min_weight + 1e-9)
        ]
        if not candidates:
            break
        share = delta / len(candidates)
        for name in candidates:
            proposed[name] = max(min_weight, min(max_weight, proposed[name] + share))

    rounded_subset = {name: round(value, 3) for name, value in proposed.items()}
    residual = round(round(target_mass, 3) - sum(rounded_subset.values()), 3)
    if residual:
        candidates = sorted(
            names,
            key=lambda name: (
                max_weight - rounded_subset[name]
                if residual > 0
                else rounded_subset[name] - min_weight
            ),
            reverse=True,
        )
        for name in candidates:
            candidate = round(rounded_subset[name] + residual, 3)
            if min_weight <= candidate <= max_weight:
                rounded_subset[name] = candidate
                break
    for name in names:
        result[name] = rounded_subset[name]
    return {name: round(value, 3) for name, value in result.items()}


def _performance_evidence(perf: List[Dict], config: Dict) -> Dict:
    def profile_is_qualified(item: Dict) -> bool:
        strategy = str(item.get("strategy_used") or "").strip()
        expected = EVOLUTION_EVIDENCE_PROFILES.get(strategy, "")
        profiles = {value.strip() for value in str(item.get("evidence_profiles") or "").split(",") if value.strip()}
        return bool(expected and profiles == {expected})

    def profiled(item: Dict, field: str, fallback: str):
        del fallback  # Legacy totals must never substitute for evidence-qualified totals.
        if not profile_is_qualified(item):
            return 0
        return item.get(field) or 0

    total = sum(int(profiled(item, "profiled_total", "total") or 0) for item in perf)
    min_total = int(config.get("min_evolution_samples", 20) or 20)
    min_per_strategy = int(config.get("min_strategy_samples", 5) or 5)
    min_strategies = int(config.get("min_evolution_strategies", 2) or 2)
    eligible = [
        item for item in perf
        if int(profiled(item, "profiled_total", "total") or 0) >= min_per_strategy
    ]
    reasons = []
    if total < min_total:
        reasons.append(f"已验证样本 {total}/{min_total}")
    if len(eligible) < min_strategies:
        reasons.append(f"达到单策略样本门槛的维度 {len(eligible)}/{min_strategies}")
    return {
        "ready": not reasons,
        "total": total,
        "minimum_total": min_total,
        "minimum_per_strategy": min_per_strategy,
        "minimum_strategies": min_strategies,
        "eligible_strategies": [str(item.get("strategy_used") or "") for item in eligible],
        "evidence_profiles": {
            str(item.get("strategy_used") or ""): str(item.get("evidence_profiles") or "")
            for item in eligible
        },
        "reasons": reasons,
    }


def _recent_intraday_evidence(lookback_days: int = 14) -> Dict:
    """Expose composite direction checks without treating them as strategy samples."""
    from domain.services.weekly_report_service import _summarize_intraday_predictions

    end = datetime.now().date()
    start = end - timedelta(days=max(1, lookback_days) - 1)
    return _summarize_intraday_predictions(start, end)


def build_evolution_readiness(
    lookback_days: int = 14,
    as_of: date | None = None,
) -> Dict:
    """Read-only evidence status for user-facing advisor reports."""
    end_day = as_of or datetime.now().date()
    start_day = end_day - timedelta(days=max(1, lookback_days))
    performance = db.get_strategy_performance(start_day.isoformat(), end_day.isoformat())
    evidence = _performance_evidence(performance, load_strategy_config())
    pending_rows = db.get_unchecked_predictions(before_date=end_day.isoformat())
    pending_by_strategy: Dict[str, int] = {}
    for row in pending_rows:
        profile = str(row.get("evidence_profile") or "").strip()
        strategy = str(row.get("strategy_used") or "").strip()
        if not profile or not strategy:
            continue
        pending_by_strategy[strategy] = pending_by_strategy.get(strategy, 0) + 1

    strategies = []
    seen = set()
    for item in performance:
        strategy = str(item.get("strategy_used") or "").strip()
        profile = str(item.get("evidence_profiles") or "").strip()
        profiled_total = int(item.get("profiled_total") or 0)
        if not strategy or (not profile and not pending_by_strategy.get(strategy)):
            continue
        strategies.append(
            {
                "strategy": strategy,
                "evidence_profile": profile,
                "verified": profiled_total,
                "minimum": int(evidence["minimum_per_strategy"]),
                "pending": int(pending_by_strategy.get(strategy, 0)),
            }
        )
        seen.add(strategy)
    for strategy, pending in pending_by_strategy.items():
        if strategy in seen:
            continue
        strategies.append(
            {
                "strategy": strategy,
                "evidence_profile": "",
                "verified": 0,
                "minimum": int(evidence["minimum_per_strategy"]),
                "pending": int(pending),
            }
        )
        seen.add(strategy)
    for strategy, profile in EVOLUTION_EVIDENCE_PROFILES.items():
        if strategy in seen:
            continue
        strategies.append(
            {
                "strategy": strategy,
                "evidence_profile": profile,
                "verified": 0,
                "minimum": int(evidence["minimum_per_strategy"]),
                "pending": 0,
            }
        )
    strategies.sort(key=lambda item: item["strategy"])
    return {
        **evidence,
        "period": f"{start_day.isoformat()} ~ {end_day.isoformat()}",
        "lookback_days": int(lookback_days),
        "pending": sum(pending_by_strategy.values()),
        "strategies": strategies,
        "remaining_total": max(0, int(evidence["minimum_total"]) - int(evidence["total"])),
        "maturity_rule": "新样本需走满3个真实交易日并通过价格锚点校验后才计入",
    }


def adjust_strategy_weights(lookback_days: int = 14) -> Dict:
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    print(f"⚖️ 策略权重调整 [近{lookback_days}天: {start_date} ~ {end_date}]")

    config = load_strategy_config()
    if not config.get("auto_adjust_enabled", True):
        print("  ℹ️ 自动调整已禁用")
        result = dict(config)
        result["_evolution_evidence"] = {"ready": False, "reasons": ["自动调整已禁用"]}
        result["_weights_changed"] = False
        return result

    perf = db.get_strategy_performance(start_date, end_date)
    evidence = _performance_evidence(perf, config)
    if not evidence["ready"]:
        print("  ℹ️ 证据不足，保持权重不变：" + "；".join(evidence["reasons"]))
        result = dict(config)
        result["_evolution_evidence"] = evidence
        result["_weights_changed"] = False
        return result

    old_weights = config["weights"].copy()
    eligible_perf = [p for p in perf if str(p.get("strategy_used") or "") in evidence["eligible_strategies"]]
    def metric(item: Dict, profiled_field: str, fallback_field: str):
        value = item.get(profiled_field)
        return item.get(fallback_field) if value is None else value

    eligible_total = sum(int(metric(p, "profiled_total", "total") or 0) for p in eligible_perf)
    pooled_win_rate = (
        sum(int(metric(p, "profiled_correct", "correct") or 0) for p in eligible_perf) / eligible_total * 100
        if eligible_total
        else 50.0
    )

    step = config.get("adjust_step", 0.05)
    min_w = config.get("min_weight", 0.10)
    max_w = config.get("max_weight", 0.60)
    adjustments = {}

    for p in eligible_perf:
        name = p.get("strategy_used", "")
        win_rate = metric(p, "profiled_win_rate", "win_rate")
        win_rate = 50 if win_rate is None else float(win_rate)
        if name in config["weights"]:
            if win_rate > pooled_win_rate + 5:
                adjustments[name] = step
            elif win_rate < pooled_win_rate - 5:
                adjustments[name] = -step
            else:
                adjustments[name] = 0

    config["weights"] = _rebalance_eligible_weights(
        config["weights"],
        adjustments,
        evidence["eligible_strategies"],
        min_w,
        max_w,
    )

    # 权重无实质变化时跳过写入，避免 weight_history 膨胀
    if old_weights == config["weights"]:
        print("  ℹ️ 权重无变化，跳过记录")
        result = dict(config)
        result["_evolution_evidence"] = evidence
        result["_weights_changed"] = False
        return result

    config.setdefault("weight_history", []).append(
        {
            "date": end_date,
            "old_weights": old_weights,
            "new_weights": config["weights"].copy(),
            "reason": f"基于近{lookback_days}天表现自动调整",
            "performance": [dict(p) for p in perf],
        }
    )
    config["weight_history"] = config["weight_history"][-20:]

    for name, weight in config["weights"].items():
        db.update_strategy_weight(name, weight)

    save_strategy_config(config)
    print(f"  旧权重: {old_weights}")
    print(f"  新权重: {config['weights']}")
    print(f"  调整: {adjustments}")
    result = dict(config)
    result["_evolution_evidence"] = evidence
    result["_weights_changed"] = True
    return result


def _normalize_rule_signature(rule_text: str) -> str:
    """提取规则的语义签名，用于去重。
    例如 "策略'technical'近期失败3次..." 和 "策略'technical'近期失败11次..."
    应映射到同一个签名 "strategy_failure::technical"。
    """
    import re

    text = str(rule_text or "")
    # 策略失败规则：提取策略名
    m = re.match(r"策略'(\w+)'近期失败\d+次", text)
    if m:
        return f"strategy_failure::{m.group(1)}"
    # 目标连续失败规则：提取目标和方向
    m = re.match(r"对(\w+)连续\d+次(看涨|看跌|中性)预测失败", text)
    if m:
        return f"target_failure::{m.group(1)}::{m.group(2)}"
    # 高置信失败规则
    if "高置信度" in text and "预测失败" in text:
        return "high_confidence_failure"
    return text


def _get_rule_signatures(conn) -> dict:
    """返回 {signature: (rule_id, rule_text)} 映射。"""
    rows = conn.execute("SELECT id, rule_text FROM rules WHERE enabled=1").fetchall()
    sig_map = {}
    for row in rows:
        sig = _normalize_rule_signature(row["rule_text"])
        # 保留最新的（id 最大的）规则
        if sig not in sig_map or int(row["id"]) > int(sig_map[sig][0]):
            sig_map[sig] = (row["id"], row["rule_text"])
    return sig_map


def update_rules_from_failures(lookback_days: int = 7) -> List[Dict]:
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    print(f"📏 规则库更新 [近{lookback_days}天]")

    performance = db.get_strategy_performance(start_date, end_date)
    evidence = _performance_evidence(performance, load_strategy_config())
    if not evidence.get("ready"):
        print("  ℹ️ 画像化证据不足，不从历史未画像化失败样本生成规则：" + "；".join(evidence.get("reasons") or []))
        return []
    eligible = set(evidence.get("eligible_strategies") or [])
    predictions = [
        item for item in db.get_checked_predictions_in_range(start_date, end_date)
        if str(item.get("strategy_used") or "") in eligible
        and str(item.get("evidence_profile") or "")
            == EVOLUTION_EVIDENCE_PROFILES.get(str(item.get("strategy_used") or ""), "")
        and str(item.get("prediction_run_id") or "").strip()
    ]
    failures = [p for p in predictions if not p.get("is_correct")]
    if not failures:
        print("  ℹ️ 无失败案例，无需更新规则")
        return []

    new_rules = []
    conn = db.get_conn()
    existing_sigs = _get_rule_signatures(conn)

    high_conf_failures = [f for f in failures if (f.get("confidence") or 0) > 0.7]
    if len(high_conf_failures) >= 2:
        rule_text = f"近期{len(high_conf_failures)}次高置信度(>70%)预测失败，应降低整体置信度阈值"
        sig = _normalize_rule_signature(rule_text)
        if sig not in existing_sigs:
            rid = db.add_rule(rule_text, "reflection", "general", 0.6)
            new_rules.append({"id": rid, "rule": rule_text, "signature": sig})
            existing_sigs[sig] = (rid, rule_text)
            print(f"  📝 新规则: {rule_text}")

    by_target = {}
    for failure in failures:
        target = failure.get("target", "")
        by_target.setdefault(target, []).append(failure)

    for target, target_failures in by_target.items():
        if len(target_failures) >= 2:
            directions = [f.get("direction", "") for f in target_failures]
            if len(set(directions)) == 1:
                dir_str = "看涨" if directions[0] == "up" else "看跌" if directions[0] == "down" else "中性"
                rule_text = f"对{target}连续{len(target_failures)}次{dir_str}预测失败，应反向思考或暂停预测"
                sig = _normalize_rule_signature(rule_text)
                if sig in existing_sigs:
                    old_id, old_text = existing_sigs[sig]
                    conn.execute(
                        "UPDATE rules SET rule_text=?, last_updated=datetime('now') WHERE id=?",
                        (rule_text, old_id),
                    )
                    print(f"  🔄 更新规则 [{old_id}]: {rule_text}")
                else:
                    rid = db.add_rule(rule_text, "reflection", "general", 0.5)
                    new_rules.append({"id": rid, "rule": rule_text, "signature": sig})
                    existing_sigs[sig] = (rid, rule_text)
                    print(f"  📝 新规则: {rule_text}")

    strategy_failures = {}
    for failure in failures:
        strategy = failure.get("strategy_used", "unknown")
        strategy_failures[strategy] = strategy_failures.get(strategy, 0) + 1

    for strategy, count in strategy_failures.items():
        if count >= 3 and strategy != "unknown":
            rule_text = f"策略'{strategy}'近期失败{count}次，需审查该策略的适用条件"
            sig = _normalize_rule_signature(rule_text)
            if sig in existing_sigs:
                old_id, old_text = existing_sigs[sig]
                conn.execute(
                    "UPDATE rules SET rule_text=?, last_updated=datetime('now') WHERE id=?",
                    (rule_text, old_id),
                )
                print(f"  🔄 更新规则 [{old_id}]: {rule_text}")
            else:
                rid = db.add_rule(rule_text, "reflection", "general", 0.55)
                new_rules.append({"id": rid, "rule": rule_text, "signature": sig})
                existing_sigs[sig] = (rid, rule_text)
                print(f"  📝 新规则: {rule_text}")

    conn.commit()

    # 更新规则置信度（复用已有连接）
    all_rules = conn.execute(
        "SELECT id, confidence, times_applied, times_helpful FROM rules WHERE enabled=1"
    ).fetchall()
    for rule in all_rules:
        applied = int(rule["times_applied"] or 0)
        helpful = int(rule["times_helpful"] or 0)
        if applied > 5:
            new_conf = calculate_rule_confidence(applied, helpful, default_confidence=0.5)
            conn.execute(
                "UPDATE rules SET confidence=?, last_updated=datetime('now') WHERE id=?",
                (round(new_conf, 2), rule["id"]),
            )
    conn.commit()

    # 重新读取以判断低置信度规则禁用
    refreshed_rules = conn.execute(
        "SELECT id, confidence, times_applied FROM rules WHERE enabled=1"
    ).fetchall()
    disable_ids = [
        int(r["id"])
        for r in refreshed_rules
        if should_disable_rule(r["confidence"], r["times_applied"], min_confidence=0.2, min_applied=10)
    ]
    disabled = 0
    if disable_ids:
        placeholders = ",".join("?" * len(disable_ids))
        disabled = conn.execute(
            f"UPDATE rules SET enabled=0 WHERE id IN ({placeholders})",
            disable_ids,
        ).rowcount
    conn.commit()
    conn.close()

    if disabled:
        print(f"  🗑️ 禁用 {disabled} 条低置信度规则")
    print(f"  ✅ 新增 {len(new_rules)} 条规则 (含 {sum(1 for r in new_rules if r.get('signature'))} 条去重)")
    return new_rules


def update_few_shot_examples(lookback_days: int = 14) -> Dict:
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    print(f"📝 Few-shot 案例库更新 [近{lookback_days}天]")

    performance = db.get_strategy_performance(start_date, end_date)
    evidence = _performance_evidence(performance, load_strategy_config())
    if not evidence.get("ready"):
        print("  ℹ️ 画像化证据不足，不从历史未画像化样本生成案例：" + "；".join(evidence.get("reasons") or []))
        return {"added": 0, "removed": 0, "evidence_ready": False}
    eligible = set(evidence.get("eligible_strategies") or [])
    predictions = [
        item for item in db.get_checked_predictions_in_range(start_date, end_date)
        if str(item.get("strategy_used") or "") in eligible
        and str(item.get("evidence_profile") or "")
            == EVOLUTION_EVIDENCE_PROFILES.get(str(item.get("strategy_used") or ""), "")
        and str(item.get("prediction_run_id") or "").strip()
    ]
    if not predictions:
        print("  ℹ️ 无已检查的预测")
        return {"added": 0, "removed": 0}

    added = 0
    removed = 0

    good_preds = [p for p in predictions if (p.get("score") or 0) >= 70]
    for pred in good_preds[:5]:
        scenario = f"{pred.get('target_name', pred['target'])}分析 ({pred.get('created_at', '')[:10]})"
        input_text = f"分析{pred.get('target_name', pred['target'])}的走势"
        output_text = pred.get("reasoning", "")
        if output_text and len(output_text) > 50:
            db.add_few_shot_example(
                category="good_analysis",
                scenario=scenario,
                input_text=input_text,
                output_text=output_text,
                score=pred.get("score", 70),
            )
            added += 1

    bad_preds = [p for p in predictions if (p.get("score") or 100) < 30]
    for pred in bad_preds[:3]:
        scenario = f"失败案例：{pred.get('target_name', pred['target'])} ({pred.get('created_at', '')[:10]})"
        input_text = f"分析{pred.get('target_name', pred['target'])}的走势"
        output_text = f"[错误分析] {pred.get('reasoning', '')}\n[回测结果] {pred.get('check_note', '')}"
        if pred.get("reasoning"):
            db.add_few_shot_example(
                category="bad_analysis",
                scenario=scenario,
                input_text=input_text,
                output_text=output_text,
                score=pred.get("score", 20),
            )

    conn = db.get_conn()
    for category in ["good_analysis", "bad_analysis"]:
        all_examples = conn.execute(
            "SELECT id FROM few_shot_examples WHERE category=? AND enabled=1 ORDER BY score DESC",
            (category,),
        ).fetchall()
        if len(all_examples) > 10:
            old_ids = [e["id"] for e in all_examples[10:]]
            conn.execute(
                f"UPDATE few_shot_examples SET enabled=0 WHERE id IN ({','.join('?' * len(old_ids))})",
                old_ids,
            )
            removed += len(old_ids)
    conn.commit()
    conn.close()

    result = {"added": added, "removed": removed}
    print(f"  ✅ 新增 {added} 个案例, 移除 {removed} 个旧案例")
    return result


def generate_system_prompt() -> str:
    config = load_strategy_config()
    strategies = db.get_strategies(enabled_only=True)
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    recent_performance = db.get_strategy_performance(start_date, end_date)
    performance_evidence = _performance_evidence(recent_performance, config)
    qualified_rates = {
        str(item.get("strategy_used") or ""): float(item.get("profiled_win_rate") or 0)
        for item in recent_performance
        if str(item.get("strategy_used") or "") in set(performance_evidence.get("eligible_strategies") or [])
    } if performance_evidence.get("ready") else {}
    rules = db.get_rules(enabled_only=True)
    if not performance_evidence.get("ready"):
        rules = [item for item in rules if str(item.get("source") or "") != "reflection"]
    good_examples = db.get_few_shot_examples("good_analysis", limit=3)
    bad_examples = db.get_few_shot_examples("bad_analysis", limit=2)
    if not performance_evidence.get("ready"):
        good_examples = []
        bad_examples = []

    weights_str = ", ".join(
        f"{s['name']}({config['weights'].get(s['name'], s['weight']):.0%})"
        for s in strategies
    )

    prompt = f"""你是 OpenClaw 投资助手（大龙虾），一个持续学习和自我提高的A股投资分析助手。

## 当前策略偏好
{weights_str}

## 分析框架
根据策略权重，在分析时应：
"""
    for strategy in strategies:
        weight = config["weights"].get(strategy["name"], strategy["weight"])
        prompt += f"- **{strategy['name']}** (权重 {weight:.0%}): {strategy['description']}\n"
        if strategy["name"] in qualified_rates:
            prompt += f"  画像化样本近期胜率: {qualified_rates[strategy['name']]:.1f}%\n"
    if not performance_evidence.get("ready"):
        prompt += "- 策略绩效证据未达20/5/2门槛；忽略数据库中的历史未画像化胜率，不据此偏向任何策略。\n"

    if rules:
        prompt += "\n## 投资规则（必须遵守）\n"
        for rule in rules[:15]:
            prompt += f"- [{rule['category']}] {rule['rule_text']}\n"

    if good_examples:
        prompt += "\n## 分析示例（参考风格）\n"
        for ex in good_examples:
            prompt += f"\n### {ex['scenario']}\n"
            prompt += f"**问题：** {ex['input_text'][:200]}\n"
            prompt += f"**分析：** {ex['output_text'][:500]}\n"

    if bad_examples:
        prompt += "\n## 避免以下错误\n"
        for ex in bad_examples:
            prompt += f"- ⚠️ {ex['scenario']}: {ex['output_text'][:200]}\n"

    prompt += """
## 地缘宏观分析框架
在分析时必须考虑以下全球宏观因素：
1. **全球市场联动**：隔夜美股（道琼斯、标普500、纳斯达克）走势对A股开盘的影响；港股恒生指数的参考意义
2. **大宗商品传导**：原油价格波动→化工/航空/运输板块；黄金走势→避险情绪；铜价→经济景气度
3. **地缘政治风险**：地区冲突→原油供应→能源价格→市场情绪；制裁→供应链→相关行业
4. **央行政策**：美联储/欧央行/中国央行的利率决议、货币政策信号对市场流动性的影响
5. **汇率因素**：美元指数走强→新兴市场资金外流；人民币汇率波动→北向资金流向

## 横盘市场判断指引
- 当市场处于横盘整理状态（近10日累计涨跌幅<1%，日均波动<0.8%）时，应优先考虑预测"neutral"
- 当预期涨跌幅在±0.3%以内时，应预测"neutral"而非强行给出方向
- 只有在出现明确的方向性信号（重大政策、突发事件、技术突破等）时才预测"up"或"down"

## 输出要求
1. 每次分析必须明确给出方向（看涨/看跌/中性）、置信度(0-1)和预测涨跌幅
2. 推理过程必须结合多个策略维度，包括全球市场和宏观因素
3. 标注使用了哪些策略和数据源
4. 风险提示不可缺少
5. 所有数据必须来自实时获取，禁止使用记忆中的历史价格
"""
    os.makedirs(os.path.dirname(PROMPT_TEMPLATE_PATH), exist_ok=True)
    with open(PROMPT_TEMPLATE_PATH, "w", encoding="utf-8") as f:
        f.write(prompt)
    return prompt


def evolve() -> Dict:
    print(f"🧬 进化流程开始 [{datetime.now().strftime('%Y-%m-%d %H:%M')}]")
    print("\n" + "=" * 50)
    config = adjust_strategy_weights()
    print("\n" + "=" * 50)
    new_rules = update_rules_from_failures()
    print("\n" + "=" * 50)
    fewshot_result = update_few_shot_examples()
    print("\n" + "=" * 50)
    print("📝 重新生成 System Prompt...")
    prompt = generate_system_prompt()
    print(f"  ✅ Prompt 已更新 ({len(prompt)} 字符)")
    print("\n🧬 进化流程完成")
    evidence = config.get("_evolution_evidence", {}) or {}
    intraday_evidence = _recent_intraday_evidence()
    weights_changed = bool(config.get("_weights_changed"))
    material_change = bool(weights_changed or new_rules or fewshot_result.get("added") or fewshot_result.get("removed"))
    if material_change:
        conclusion = "本次已形成有验证证据的策略更新。"
    elif not evidence.get("ready"):
        conclusion = "本次仅完成策略审计，不构成策略进化；验证样本不足，权重、规则与案例库保持不变。"
    else:
        conclusion = "验证证据已达到门槛，但未发现足以改变权重、规则或案例库的新信息。"
    evidence_reason = "；".join(evidence.get("reasons") or []) or (
        f"已验证样本 {evidence.get('total', 0)} 条，达到调整门槛" if evidence else "证据状态未知"
    )
    text = "\n".join(
        [
            "🧬 OpenClaw 策略审计与进化结果",
            "",
            "**结论**",
            f"- {conclusion}",
            f"- 证据：{evidence_reason}。",
            "",
            "**变更结果**",
            f"- 策略权重：{'已调整' if weights_changed else '未调整'}。",
            f"- 新增规则：{len(new_rules)} 条。",
            f"- Few-shot 案例：新增 {fewshot_result.get('added', 0)} 条，移除 {fewshot_result.get('removed', 0)} 条。",
            (
                f"- 日内方向复盘：已验证 {intraday_evidence.get('total', 0)} 次；"
                "属于复合市场判断，未归因到单一策略维度，不进入权重更新。"
            ),
            "",
            "**当前权重**",
            "- " + "；".join(f"{name} {weight:.1%}" for name, weight in config.get("weights", {}).items()),
            "",
            "**边界**",
            "- 只有完成回测验证的预测才进入权重、规则和案例更新；零样本不会被解释为 0% 胜率。",
        ]
    )
    return {
        "weights": config.get("weights", {}),
        "weights_changed": weights_changed,
        "new_rules": len(new_rules),
        "fewshot": fewshot_result,
        "prompt_length": len(prompt),
        "evidence": evidence,
        "intraday_evidence": intraday_evidence,
        "material_change": material_change,
        "text": text,
    }

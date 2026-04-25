#!/usr/bin/env python3
"""Evolution services."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
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


def adjust_strategy_weights(lookback_days: int = 14) -> Dict:
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    print(f"⚖️ 策略权重调整 [近{lookback_days}天: {start_date} ~ {end_date}]")

    config = load_strategy_config()
    if not config.get("auto_adjust_enabled", True):
        print("  ℹ️ 自动调整已禁用")
        return config

    perf = db.get_strategy_performance(start_date, end_date)
    if not perf:
        print("  ℹ️ 无足够数据进行调整")
        return config

    old_weights = config["weights"].copy()
    avg_win_rate = sum(p.get("win_rate", 50) or 50 for p in perf) / len(perf) if perf else 50

    step = config.get("adjust_step", 0.05)
    min_w = config.get("min_weight", 0.10)
    max_w = config.get("max_weight", 0.60)
    adjustments = {}

    for p in perf:
        name = p.get("strategy_used", "")
        win_rate = p.get("win_rate", 50) or 50
        if name in config["weights"]:
            if win_rate > avg_win_rate + 5:
                adjustments[name] = step
            elif win_rate < avg_win_rate - 5:
                adjustments[name] = -step
            else:
                adjustments[name] = 0

    for name, adj in adjustments.items():
        new_weight = config["weights"].get(name, 0.33) + adj
        config["weights"][name] = max(min_w, min(max_w, new_weight))

    total = sum(config["weights"].values())
    if total > 0:
        config["weights"] = {k: round(v / total, 3) for k, v in config["weights"].items()}

    # 权重无实质变化时跳过写入，避免 weight_history 膨胀
    if old_weights == config["weights"]:
        print("  ℹ️ 权重无变化，跳过记录")
        return config

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
    return config


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

    predictions = db.get_checked_predictions_in_range(start_date, end_date)
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

    predictions = db.get_checked_predictions_in_range(start_date, end_date)
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
    rules = db.get_rules(enabled_only=True)
    good_examples = db.get_few_shot_examples("good_analysis", limit=3)
    bad_examples = db.get_few_shot_examples("bad_analysis", limit=2)

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
        if strategy.get("win_rate"):
            prompt += f"  近期胜率: {strategy['win_rate']:.1f}%\n"

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
    return {
        "weights": config.get("weights", {}),
        "new_rules": len(new_rules),
        "fewshot": fewshot_result,
        "prompt_length": len(prompt),
    }

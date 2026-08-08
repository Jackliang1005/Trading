from __future__ import annotations

import json
from datetime import datetime

import db
from domain.services import prediction_orchestrator as orchestrator
from domain.services.prediction_prompt_service import build_prediction_prompt
from domain.services import prediction_service
from domain.services.prediction_service import sanitize_strategy_predictions, save_predictions


def _prediction(code: str, name: str, price: float) -> dict:
    return {
        "code": code,
        "name": name,
        "current_price": price,
        "trend_3d": "ranging",
        "predicted_return_3d": 0.2,
        "kline_day1": {"open": price, "high": price * 1.01, "low": price * 0.99, "close": price, "pattern": "十字星"},
        "kline_day2": {"open": price, "high": price * 1.01, "low": price * 0.99, "close": price, "pattern": "十字星"},
        "kline_day3": {"open": price, "high": price * 1.01, "low": price * 0.99, "close": price, "pattern": "十字星"},
        "buy_point": price * 0.99,
        "sell_point": price * 1.02,
        "stop_loss": price * 0.96,
        "confidence": 0.55,
        "strategy_used": "geopolitical",
        "reasoning": "test evidence",
    }


def test_prompt_contains_evolution_rules_target_whitelist_and_strategy_boundary():
    prompt = build_prediction_prompt(
        {"quotes": [{"code": "sh000001", "name": "上证指数", "price": 3800}]},
        rag_context="",
        few_shot="",
        system_prompt="只采用已验证规则A",
        targets=[{"code": "sh000001", "name": "上证指数"}],
        strategy_name="technical",
        strategy_focus="只使用价格结构",
    )

    assert "只采用已验证规则A" in prompt
    assert "sh000001(上证指数)" in prompt
    assert "禁止新增、改写或猜测代码" in prompt
    assert "本轮唯一策略为 `technical`" in prompt
    assert "只使用价格结构" in prompt


def test_sanitizer_rejects_unknown_targets_and_wrong_source_prices():
    targets = [{"code": "sh000001", "name": "上证指数"}]
    snapshot = {"quotes": [{"code": "sh000001", "price": 3800}]}
    predictions = [
        _prediction("sh000001", "wrong name", 3801),
        _prediction("sh688000", "hallucinated", 1800),
        _prediction("sh000001", "duplicate with wrong anchor", 3350),
    ]

    accepted = sanitize_strategy_predictions(
        predictions,
        targets,
        snapshot,
        strategy_name="sentiment",
        evidence_profile="sentiment_flow_news_v1",
        model_used="test-model",
        prediction_run_id="run-1",
    )

    assert len(accepted) == 1
    assert accepted[0]["code"] == "sh000001"
    assert accepted[0]["name"] == "上证指数"
    assert accepted[0]["current_price"] == 3800
    assert accepted[0]["strategy_used"] == "sentiment"
    assert accepted[0]["evidence_profile"] == "sentiment_flow_news_v1"
    assert accepted[0]["prediction_run_id"] == "run-1"


def test_prediction_context_replaces_error_quotes_with_fallback(monkeypatch):
    monkeypatch.setattr(
        prediction_service,
        "fetch_market_quotes",
        lambda codes: [
            {"code": "sh000001", "price": 3800, "source": "fallback"},
            {"code": "sz399001", "price": 14000, "source": "fallback"},
            {"code": "sz399006", "price": 3500, "source": "fallback"},
        ],
    )
    payload = prediction_service._hydrate_prediction_quotes(
        {"quotes": [{"code": "sh000001", "error": "primary failed"}]}
    )

    assert [item["code"] for item in payload["quotes"]] == ["sh000001", "sz399001", "sz399006"]
    assert payload["_quote_sources"] == ["fallback"]
    assert payload["_quote_coverage"] == "3/3"


def test_generate_predictions_builds_two_independent_evidence_chains(monkeypatch):
    targets = [
        {"code": "sh000001", "name": "上证指数"},
        {"code": "sz399001", "name": "深证成指"},
        {"code": "sz399006", "name": "创业板指"},
    ]
    snapshot = {
        "_source": "research_packets",
        "_packet_hits": 2,
        "quotes": [
            {"code": "sh000001", "name": "上证指数", "price": 3800},
            {"code": "sz399001", "name": "深证成指", "price": 14000},
            {"code": "sz399006", "name": "创业板指", "price": 3500},
        ],
        "flow": {"north": 10},
        "news_eastmoney": [{"title": "政策预期改善"}],
    }
    raw = [_prediction(t["code"], t["name"], snapshot["quotes"][i]["price"]) for i, t in enumerate(targets)]
    raw.append(_prediction("sh688000", "幻觉标的", 1800))
    calls = []
    persisted = []

    monkeypatch.setattr(orchestrator, "build_prediction_runtime_context", lambda: {
        "snapshot_data": snapshot,
        "rag_context": "",
        "few_shot": "",
        "system_prompt": "verified rule",
    })
    monkeypatch.setattr(orchestrator, "get_position_prediction_targets", lambda: [])
    monkeypatch.setattr(orchestrator, "get_all_prediction_targets", lambda include_positions=True: targets)

    def call_strategy(prompt, model, allow_rule_fallback, system_role=""):
        strategy = "sentiment" if "`sentiment`" in prompt else "technical"
        calls.append((strategy, allow_rule_fallback, prompt, system_role))
        return json.dumps(raw, ensure_ascii=False), "test-model"

    def persist(items, model):
        persisted.extend(items)
        for idx, item in enumerate(items, 1):
            item["_persist_status"] = "saved"
            item["_prediction_id"] = idx
        return list(range(1, len(items) + 1))

    monkeypatch.setattr(orchestrator, "_call_strategy_prediction", call_strategy)
    monkeypatch.setattr(orchestrator, "save_predictions", persist)

    ids = orchestrator.generate_predictions()

    assert len(ids) == 6
    assert {(name, fallback) for name, fallback, _, _ in calls} == {
        ("technical", True),
        ("sentiment", False),
    }
    prompt_by_strategy = {name: prompt for name, _, prompt, _ in calls}
    role_by_strategy = {name: role for name, _, _, role in calls}
    assert "政策预期改善" not in prompt_by_strategy["technical"]
    assert "市场状态检测" in prompt_by_strategy["technical"]
    assert "政策预期改善" in prompt_by_strategy["sentiment"]
    assert "只允许依据价格" in role_by_strategy["technical"]
    assert "行情价格只用于数值锚定" in role_by_strategy["sentiment"]
    assert {item["code"] for item in persisted} == {item["code"] for item in targets}
    assert {item["strategy_used"] for item in persisted} == {"technical", "sentiment"}
    assert {item["evidence_profile"] for item in persisted} == {
        "technical_price_regime_v1",
        "sentiment_flow_news_v1",
    }
    assert len({item["prediction_run_id"] for item in persisted}) == 1


def test_both_evolution_strategies_are_bounded_to_indices():
    assert orchestrator.STRATEGY_TASKS["technical"]["indices_only"] is True
    assert orchestrator.STRATEGY_TASKS["sentiment"]["indices_only"] is True


def test_llm_timeout_falls_back_only_for_technical_chain(monkeypatch):
    monkeypatch.setattr(orchestrator, "resolve_available_provider", lambda: ("deepseek", "key"))
    monkeypatch.setattr(
        orchestrator,
        "call_prediction_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("deadline")),
    )
    monkeypatch.setattr(orchestrator, "render_rule_based_prediction_json", lambda: "[]")

    assert orchestrator._call_strategy_prediction("prompt", "model", True) == ("[]", "rule_based_v1")
    assert orchestrator._call_strategy_prediction("prompt", "model", False) == ("", "")


def test_prediction_persistence_is_idempotent_per_day_strategy_and_target(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "investor.db"))
    db.init_db()
    item = _prediction("sh000001", "上证指数", 3800)
    item.update(
        {
            "strategy_used": "technical",
            "evidence_profile": "technical_price_regime_v1",
            "prediction_run_id": "run-1",
            "model_used": "test-model",
        }
    )

    run_date = datetime.now().strftime("%Y-%m-%d")
    first = save_predictions([dict(item)], model="test-model", run_date=run_date)
    second_item = dict(item)
    second = save_predictions([second_item], model="test-model", run_date=run_date)

    assert len(first) == 1
    assert second == []
    assert second_item["_persist_status"] == "duplicate"
    assert db.get_prediction_keys_for_date(run_date) == {("sh000001", "technical", "3d")}

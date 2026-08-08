from datetime import datetime

import db
from domain.services import evolution_service as service


def _config():
    return {
        "weights": {"technical": 0.25, "fundamental": 0.25, "sentiment": 0.25, "geopolitical": 0.25},
        "auto_adjust_enabled": True,
        "min_weight": 0.10,
        "max_weight": 0.60,
        "adjust_step": 0.05,
        "min_evolution_samples": 20,
        "min_strategy_samples": 5,
        "min_evolution_strategies": 2,
        "weight_history": [],
    }


def test_production_strategy_config_uses_neutral_evidence_gated_baseline():
    config = service.load_strategy_config()
    quarantine = next(
        item for item in reversed(config["weight_history"])
        if (item.get("evidence") or {}).get("legacy_weights_quarantined")
    )
    expected_baseline = {
        "technical": 0.30,
        "fundamental": 0.25,
        "sentiment": 0.20,
        "geopolitical": 0.25,
    }

    assert quarantine["new_weights"] == expected_baseline
    if config["weight_history"][-1] is quarantine:
        assert config["weights"] == expected_baseline
    assert sum(config["weights"].values()) == 1.0
    assert config["min_evolution_samples"] == 20
    assert config["min_strategy_samples"] == 5
    assert config["min_evolution_strategies"] == 2
    assert config["weight_history"][-1]["evidence"]["legacy_weights_quarantined"] is True


def test_weight_adjustment_requires_enough_verified_evidence(monkeypatch):
    config = _config()
    monkeypatch.setattr(service, "load_strategy_config", lambda: config)
    monkeypatch.setattr(
        service.db,
        "get_strategy_performance",
        lambda start, end: [{"strategy_used": "technical", "total": 1, "correct": 0, "win_rate": 0.0}],
    )
    monkeypatch.setattr(service, "save_strategy_config", lambda value: (_ for _ in ()).throw(AssertionError("must not save")))

    result = service.adjust_strategy_weights()

    assert result["weights"] == config["weights"]
    assert result["_weights_changed"] is False
    assert result["_evolution_evidence"]["ready"] is False


def test_bounded_normalize_preserves_sum_and_bounds():
    result = service._bounded_normalize(
        {"technical": 0.05, "fundamental": 0.20, "sentiment": 0.70, "geopolitical": 0.20},
        0.10,
        0.60,
    )
    assert sum(result.values()) == 1.0
    assert all(0.10 <= value <= 0.60 for value in result.values())


def test_profiled_rebalance_preserves_unmeasured_strategy_weights():
    weights = {"technical": 0.30, "sentiment": 0.20, "fundamental": 0.25, "geopolitical": 0.25}

    result = service._rebalance_eligible_weights(
        weights,
        {"technical": 0.05, "sentiment": 0},
        ["technical", "sentiment"],
        0.10,
        0.60,
    )

    assert result["fundamental"] == 0.25
    assert result["geopolitical"] == 0.25
    assert result["technical"] + result["sentiment"] == 0.50
    assert result["technical"] > weights["technical"]
    assert result["sentiment"] < weights["sentiment"]


def test_adjustment_changes_only_evidence_qualified_subset(monkeypatch):
    config = _config()
    original = config["weights"].copy()
    saved = []
    updated = []
    monkeypatch.setattr(service, "load_strategy_config", lambda: config)
    monkeypatch.setattr(
        service.db,
        "get_strategy_performance",
        lambda start, end: [
            {
                "strategy_used": "technical",
                "profiled_total": 12,
                "profiled_correct": 10,
                "profiled_win_rate": 83.3,
                "evidence_profiles": "technical_price_regime_v1",
            },
            {
                "strategy_used": "sentiment",
                "profiled_total": 12,
                "profiled_correct": 4,
                "profiled_win_rate": 33.3,
                "evidence_profiles": "sentiment_flow_news_v1",
            },
        ],
    )
    monkeypatch.setattr(service, "save_strategy_config", lambda value: saved.append(value.copy()))
    monkeypatch.setattr(service.db, "update_strategy_weight", lambda name, weight: updated.append((name, weight)))

    result = service.adjust_strategy_weights()

    assert result["_weights_changed"] is True
    assert result["weights"]["fundamental"] == original["fundamental"]
    assert result["weights"]["geopolitical"] == original["geopolitical"]
    assert result["weights"]["technical"] > original["technical"]
    assert result["weights"]["sentiment"] < original["sentiment"]
    assert sum(result["weights"].values()) == 1.0
    assert saved


def test_unprofiled_legacy_samples_do_not_unlock_evolution():
    perf = [
        {"strategy_used": "technical", "total": 30, "profiled_total": 0},
        {"strategy_used": "sentiment", "total": 20, "profiled_total": 0},
    ]

    evidence = service._performance_evidence(perf, _config())

    assert evidence["ready"] is False
    assert evidence["total"] == 0


def test_two_profiled_strategy_chains_make_evolution_gate_reachable():
    perf = [
        {
            "strategy_used": "technical",
            "profiled_total": 12,
            "profiled_correct": 7,
            "profiled_win_rate": 58.3,
            "evidence_profiles": "technical_price_regime_v1",
        },
        {
            "strategy_used": "sentiment",
            "profiled_total": 12,
            "profiled_correct": 6,
            "profiled_win_rate": 50.0,
            "evidence_profiles": "sentiment_flow_news_v1",
        },
    ]

    evidence = service._performance_evidence(perf, _config())

    assert evidence["ready"] is True
    assert evidence["total"] == 24
    assert evidence["eligible_strategies"] == ["technical", "sentiment"]
    assert evidence["evidence_profiles"]["technical"] == "technical_price_regime_v1"


def test_persisted_profiled_samples_reach_gate_without_legacy_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "investor.db"))
    db.init_db()
    for strategy, profile in (
        ("technical", "technical_price_regime_v1"),
        ("sentiment", "sentiment_flow_news_v1"),
    ):
        for index in range(12):
            prediction_id = db.add_prediction(
                target=f"target-{index % 3}",
                direction="neutral",
                confidence=0.5,
                strategy_used=strategy,
                model_used="test",
                actual_price=100,
                trend_3d="ranging",
                evidence_profile=profile,
                prediction_run_id=f"run-{index // 3}",
            )
            db.update_prediction_result(
                prediction_id,
                actual_price=100,
                actual_change=0,
                is_correct=index % 2 == 0,
                score=60,
            )
    today = datetime.now().strftime("%Y-%m-%d")

    performance = db.get_strategy_performance(today, today)
    evidence = service._performance_evidence(performance, _config())

    assert evidence["ready"] is True
    assert evidence["total"] == 24
    assert set(evidence["eligible_strategies"]) == {"technical", "sentiment"}


def test_profile_label_without_a_valid_run_identity_cannot_unlock_evolution(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "investor.db"))
    db.init_db()
    rows = [
        ("technical", "technical_price_regime_v1", ""),
        ("technical", "sentiment_flow_news_v1", "wrong-profile"),
        ("technical", "technical_price_regime_v1", "qualified-run"),
    ]
    for index, (strategy, profile, run_id) in enumerate(rows):
        prediction_id = db.add_prediction(
            target=f"target-{index}",
            direction="neutral",
            confidence=0.5,
            strategy_used=strategy,
            model_used="test",
            actual_price=100,
            trend_3d="ranging",
            evidence_profile=profile,
            prediction_run_id=run_id,
        )
        db.update_prediction_result(prediction_id, actual_price=100, actual_change=0, is_correct=True, score=60)

    today = datetime.now().strftime("%Y-%m-%d")
    performance = db.get_strategy_performance(today, today)

    assert performance[0]["total"] == 3
    assert performance[0]["profiled_total"] == 1
    assert performance[0]["profiled_correct"] == 1
    assert performance[0]["evidence_profiles"] == "technical_price_regime_v1"


def test_rule_learning_ignores_unprofiled_legacy_failures(monkeypatch):
    monkeypatch.setattr(service, "load_strategy_config", _config)
    monkeypatch.setattr(
        service.db,
        "get_strategy_performance",
        lambda start, end: [
            {
                "strategy_used": "technical",
                "total": 30,
                "correct": 0,
                "profiled_total": 0,
                "evidence_profiles": None,
            }
        ],
    )
    monkeypatch.setattr(
        service.db,
        "get_checked_predictions_in_range",
        lambda start, end: (_ for _ in ()).throw(AssertionError("must stop before loading legacy failures")),
    )

    assert service.update_rules_from_failures() == []


def test_few_shot_learning_ignores_unprofiled_legacy_samples(monkeypatch):
    monkeypatch.setattr(service, "load_strategy_config", _config)
    monkeypatch.setattr(
        service.db,
        "get_strategy_performance",
        lambda start, end: [
            {
                "strategy_used": "sentiment",
                "total": 30,
                "correct": 30,
                "profiled_total": 0,
                "evidence_profiles": None,
            }
        ],
    )
    monkeypatch.setattr(
        service.db,
        "get_checked_predictions_in_range",
        lambda start, end: (_ for _ in ()).throw(AssertionError("must stop before loading legacy examples")),
    )

    result = service.update_few_shot_examples()

    assert result == {"added": 0, "removed": 0, "evidence_ready": False}


def test_readiness_exposes_verified_and_pending_profiled_samples(monkeypatch):
    monkeypatch.setattr(service, "load_strategy_config", _config)
    monkeypatch.setattr(
        service.db,
        "get_strategy_performance",
        lambda start, end: [
            {
                "strategy_used": "technical",
                "profiled_total": 3,
                "evidence_profiles": "technical_price_regime_v1",
            }
        ],
    )
    monkeypatch.setattr(
        service.db,
        "get_unchecked_predictions",
        lambda before_date=None: [
            {
                "strategy_used": "technical",
                "evidence_profile": "technical_price_regime_v1",
            },
            {
                "strategy_used": "sentiment",
                "evidence_profile": "sentiment_flow_news_v1",
            },
            {"strategy_used": "technical", "evidence_profile": ""},
        ],
    )

    result = service.build_evolution_readiness(as_of=datetime(2026, 8, 8).date())

    assert result["total"] == 3
    assert result["pending"] == 2
    assert result["remaining_total"] == 17
    assert {item["strategy"] for item in result["strategies"]} == {"technical", "sentiment"}


def test_empty_readiness_still_names_expected_evidence_chains(monkeypatch):
    monkeypatch.setattr(service, "load_strategy_config", _config)
    monkeypatch.setattr(service.db, "get_strategy_performance", lambda start, end: [])
    monkeypatch.setattr(service.db, "get_unchecked_predictions", lambda before_date=None: [])

    result = service.build_evolution_readiness(as_of=datetime(2026, 8, 8).date())

    assert result["total"] == 0
    assert [(item["strategy"], item["verified"], item["minimum"]) for item in result["strategies"]] == [
        ("sentiment", 0, 5),
        ("technical", 0, 5),
    ]


def test_evolve_labels_zero_sample_run_as_audit_not_evolution(monkeypatch):
    config = _config()
    config["_weights_changed"] = False
    config["_evolution_evidence"] = {"ready": False, "total": 0, "reasons": ["已验证样本 0/20"]}
    monkeypatch.setattr(service, "adjust_strategy_weights", lambda: config)
    monkeypatch.setattr(service, "update_rules_from_failures", lambda: [])
    monkeypatch.setattr(service, "update_few_shot_examples", lambda: {"added": 0, "removed": 0})
    monkeypatch.setattr(service, "generate_system_prompt", lambda: "prompt")
    monkeypatch.setattr(
        service,
        "_recent_intraday_evidence",
        lambda: {"total": 3, "attributable_to_strategy": False},
    )

    result = service.evolve()

    assert result["material_change"] is False
    assert "不构成策略进化" in result["text"]
    assert "零样本不会被解释为 0% 胜率" in result["text"]
    assert "日内方向复盘：已验证 3 次" in result["text"]
    assert "不进入权重更新" in result["text"]

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

from domain.services import assistant_service
from domain.services import evolution_service
from domain.services import feishu_query_service
from domain.services import prediction_service
from domain.services import reflection_runtime_service
import knowledge_base


def _strategies():
    return [
        {
            "name": "technical",
            "weight": 0.10,
            "win_rate": 99.0,
            "total_predictions": 99,
            "description": "技术分析",
        },
        {
            "name": "sentiment",
            "weight": 0.427,
            "win_rate": 88.0,
            "total_predictions": 88,
            "description": "情绪分析",
        },
    ]


def _not_ready():
    return {
        "ready": False,
        "total": 0,
        "minimum_total": 20,
        "minimum_per_strategy": 5,
        "strategies": [
            {"strategy": "technical", "verified": 0, "minimum": 5},
            {"strategy": "sentiment", "verified": 0, "minimum": 5},
        ],
    }


def test_system_prompt_ignores_persisted_legacy_strategy_win_rates(monkeypatch, tmp_path):
    monkeypatch.setattr(evolution_service.db, "get_strategies", lambda enabled_only=True: _strategies())
    monkeypatch.setattr(
        evolution_service.db,
        "get_rules",
        lambda enabled_only=True: [
            {"category": "general", "rule_text": "策略'technical'近期失败99次", "source": "reflection"}
        ],
    )
    monkeypatch.setattr(
        evolution_service.db,
        "get_few_shot_examples",
        lambda *args, **kwargs: [
            {"scenario": "旧案例", "input_text": "旧输入", "output_text": "旧未画像化分析内容"}
        ],
    )
    monkeypatch.setattr(
        evolution_service.db,
        "get_strategy_performance",
        lambda start, end: [
            {"strategy_used": "technical", "total": 99, "win_rate": 99.0, "profiled_total": 0},
            {"strategy_used": "sentiment", "total": 88, "win_rate": 88.0, "profiled_total": 0},
        ],
    )
    monkeypatch.setattr(evolution_service, "PROMPT_TEMPLATE_PATH", str(tmp_path / "system_prompt.md"))

    prompt = evolution_service.generate_system_prompt()

    assert "近期胜率: 99.0%" not in prompt
    assert "近期胜率: 88.0%" not in prompt
    assert "忽略数据库中的历史未画像化胜率" in prompt
    assert "近期失败99次" not in prompt
    assert "旧未画像化分析内容" not in prompt
    assert "technical(30%)" in prompt
    assert "sentiment(20%)" in prompt


def test_dashboard_shows_profiled_progress_not_legacy_win_rate(monkeypatch):
    monkeypatch.setattr(assistant_service.db, "get_strategies", lambda: _strategies())
    monkeypatch.setattr(assistant_service.db, "get_rules", lambda: [])
    monkeypatch.setattr(assistant_service.db, "get_overall_stats", lambda: {})
    monkeypatch.setattr(assistant_service, "build_evolution_readiness", _not_ready)
    monkeypatch.setattr(assistant_service, "load_prediction_snapshot_data", lambda: {})
    monkeypatch.setattr(assistant_service, "load_longterm_snapshot", lambda: {})
    monkeypatch.setattr(assistant_service, "summarize_longterm_snapshot", lambda value: {"available": False})

    text = assistant_service.dashboard()

    assert "胜率 99.0%" not in text
    assert "胜率 88.0%" not in text
    assert "画像化样本 0/5" in text
    assert "历史未画像化胜率不参与展示" in text


def test_rag_context_uses_config_weights_without_legacy_rates(monkeypatch):
    class EmptyKnowledgeBase:
        def search_all(self, query, n_per_type=5):
            return {}

    monkeypatch.setattr(knowledge_base, "KnowledgeBase", EmptyKnowledgeBase)
    monkeypatch.setattr(
        knowledge_base.db,
        "get_rules",
        lambda enabled_only=True: [
            {"category": "general", "rule_text": "策略'technical'近期失败99次", "source": "reflection", "confidence": 0.9}
        ],
    )
    monkeypatch.setattr(knowledge_base.db, "get_strategies", lambda enabled_only=True: _strategies())
    monkeypatch.setattr(evolution_service, "build_evolution_readiness", _not_ready)

    text = knowledge_base.build_rag_context("测试")

    assert "99.0%" not in text
    assert "88.0%" not in text
    assert "technical: 30%" in text
    assert "sentiment: 20%" in text
    assert "禁止把历史未画像化胜率写入分析上下文" in text
    assert "近期失败99次" not in text


def test_few_shot_prompt_is_empty_until_profiled_gate_is_ready(monkeypatch):
    monkeypatch.setattr(evolution_service, "build_evolution_readiness", _not_ready)
    monkeypatch.setattr(
        knowledge_base.db,
        "get_few_shot_examples",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not load legacy examples")),
    )

    assert knowledge_base.build_few_shot_prompt() == ""


def test_prediction_strategy_distribution_uses_audited_config_not_stale_db_weights(monkeypatch):
    stale = [
        {"name": "technical", "weight": 0.05},
        {"name": "fundamental", "weight": 0.05},
        {"name": "sentiment", "weight": 0.80},
        {"name": "geopolitical", "weight": 0.10},
    ]
    monkeypatch.setattr(prediction_service.db, "get_strategies", lambda enabled_only=True: stale)
    monkeypatch.setattr(
        evolution_service,
        "load_strategy_config",
        lambda: {"weights": {"technical": 0.30, "fundamental": 0.25, "sentiment": 0.20, "geopolitical": 0.25}},
    )
    monkeypatch.setattr(
        prediction_service,
        "PREDICTION_TARGETS",
        [{"code": "000001.SH"}, {"code": "000300.SH"}],
    )
    monkeypatch.setattr(prediction_service, "get_position_prediction_targets", lambda: [])

    distribution = prediction_service._get_strategy_distribution()

    assert distribution["000001.SH"] == "geopolitical"
    assert distribution["000300.SH"] == "sentiment"


def test_daily_prediction_breakdown_hides_unqualified_strategy_comparison(monkeypatch):
    rows = [
        {
            "target": "000001.SH",
            "target_name": "上证指数",
            "strategy_used": "technical",
            "is_correct": False,
            "score": 20,
            "predicted_change": -1,
            "actual_change": 1,
            "evidence_profile": "",
            "prediction_run_id": "",
            "created_at": "2026-08-08 09:30:00",
        },
        {
            "target": "000300.SH",
            "target_name": "沪深300",
            "strategy_used": "sentiment",
            "is_correct": True,
            "score": 80,
            "predicted_change": 1,
            "actual_change": 1,
            "evidence_profile": "",
            "prediction_run_id": "",
            "created_at": "2026-08-08 09:31:00",
        },
    ]
    monkeypatch.setattr(reflection_runtime_service.db, "get_checked_predictions_in_range", lambda start, end: rows)

    text = reflection_runtime_service._build_prediction_breakdown_table({"checked": 2})

    assert "按标的统计" in text
    assert "按策略统计" not in text
    assert "策略归因边界" in text
    assert "未达到完整门槛，不输出分策略胜率" in text


def test_strategy_command_discloses_quarantined_legacy_weights():
    text = feishu_query_service._query_strategy()

    assert "历史未画像化样本形成的权重已隔离" in text
    assert "20/5/2门槛" in text

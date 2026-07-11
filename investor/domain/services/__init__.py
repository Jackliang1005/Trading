"""Domain services — canonical service layer index.

每个服务模块的公开函数在此集中 re-export，作为服务层的统一入口。
"""

# ── Analysis Context ──
from domain.services.analysis_context_service import (
    normalize_analysis_context_summary,
    normalize_trade_decision_summary,
)

# ── Assistant Service ──
from domain.services.assistant_service import (
    analyze,
    dashboard,
    record_feedback,
    record_prediction,
)

# ── Evolution Service ──
from domain.services.evolution_service import (
    adjust_strategy_weights,
    evolve,
    generate_system_prompt,
    load_strategy_config,
    update_few_shot_examples,
    update_rules_from_failures,
)

# ── Feishu Bridge Service ──
from domain.services.feishu_bridge_service import (
    build_bridge_response,
)

# ── Feishu Query Service ──
from domain.services.feishu_query_service import (
    handle_feishu_query,
)

# ── Legacy Entry Service ──
from domain.services.legacy_entry_service import (
    build_dashboard,
    cron_daily_collect,
    cron_daily_predict,
    cron_daily_reflect,
    cron_monthly_audit,
    cron_sector_scan,
    cron_weekly_evolve,
    init_system,
)

# ── Live Monitor Service ──
from domain.services.live_monitor_service import (
    run_live_monitor,
)

# ── Live Monitor View Service ──
from domain.services.live_monitor_view_service import (
    format_today_summary_text,
    get_today_account,
    get_today_buys,
    get_today_candidates,
    get_today_summary,
    run_trading_monitor,
)

# ── Longterm Portfolio Service ──
from domain.services.longterm_portfolio_service import (
    build_longterm_snapshot_text,
    load_longterm_snapshot,
    summarize_longterm_snapshot,
)

# ── Prediction Orchestrator ──
from domain.services.prediction_orchestrator import (
    call_llm_for_prediction,
    generate_predictions,
    parse_predictions,
    render_rule_based_prediction_json,
)

# ── Prediction Prompt Service ──
from domain.services.prediction_prompt_service import (
    build_market_context_text,
    build_prediction_context,
    build_prediction_prompt,
)

# ── Prediction Service ──
from domain.services.prediction_service import (
    PREDICTION_TARGETS,
    load_prediction_snapshot_data,
    save_predictions,
)

# ── Reflection Analysis Service ──
from domain.services.reflection_analysis_service import (
    analyze_failure_patterns,
    format_weekly_report,
)

# ── Reflection Runtime Service ──
from domain.services.reflection_runtime_service import (
    backtest_predictions,
    build_trading_summary_report,
    daily_reflection,
    get_reflection_context_summary,
    load_reflection_context,
)

# ── Reflection Service ──
from domain.services.reflection_service import (
    monthly_audit,
    weekly_attribution,
)

__all__ = [
    # analysis_context
    "normalize_analysis_context_summary",
    "normalize_trade_decision_summary",
    # assistant
    "analyze",
    "build_dashboard",
    "record_feedback",
    "record_prediction",
    # evolution
    "adjust_strategy_weights",
    "evolve",
    "generate_system_prompt",
    "load_strategy_config",
    "update_few_shot_examples",
    "update_rules_from_failures",
    # feishu_bridge
    "build_bridge_response",
    # feishu_query
    "handle_feishu_query",
    # legacy_entry
    "cron_daily_collect",
    "cron_daily_predict",
    "cron_daily_reflect",
    "cron_monthly_audit",
    "cron_sector_scan",
    "cron_weekly_evolve",
    "init_system",
    # live_monitor
    "run_live_monitor",
    # live_monitor_view
    "format_today_summary_text",
    "get_today_account",
    "get_today_buys",
    "get_today_candidates",
    "get_today_summary",
    "run_trading_monitor",
    # longterm_portfolio
    "build_longterm_snapshot_text",
    "load_longterm_snapshot",
    "summarize_longterm_snapshot",
    # prediction_orchestrator
    "call_llm_for_prediction",
    "generate_predictions",
    "parse_predictions",
    "render_rule_based_prediction_json",
    # prediction_prompt
    "build_market_context_text",
    "build_prediction_context",
    "build_prediction_prompt",
    # prediction_service
    "PREDICTION_TARGETS",
    "load_prediction_snapshot_data",
    "save_predictions",
    # reflection_analysis
    "analyze_failure_patterns",
    "format_weekly_report",
    # reflection_runtime
    "backtest_predictions",
    "build_trading_summary_report",
    "daily_reflection",
    "get_reflection_context_summary",
    "load_reflection_context",
    # reflection_service
    "monthly_audit",
    "weekly_attribution",
]

from __future__ import annotations

import json

import skill_api
from domain.services import feishu_query_service as query


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_fixed_rule_replay_returns_benchmark_costs_and_honest_day_metric(monkeypatch):
    closes = [100 + index * 0.5 for index in range(40)] + [120 - index * 0.4 for index in range(40)]
    rows = [[f"2026-01-{index + 1:02d}", "0", str(close)] for index, close in enumerate(closes)]
    payload = {"data": {"sh603986": {"day": rows}}}
    monkeypatch.setattr(skill_api.urllib.request, "urlopen", lambda url, timeout: _Response(payload))

    result = skill_api._backtest("603986", days=250)

    assert result["ok"] is True
    assert result["strategy"] == "MA5_above_MA20_long_only"
    assert result["cost_bps_per_side"] == 10
    assert result["net_total_return_pct"] <= result["total_return_pct"]
    assert result["excess_return_pct_points"] == round(
        result["net_total_return_pct"] - result["benchmark_return_pct"], 2
    )
    assert result["signal_changes"] == result["entries"] + result["exits"]
    assert result["active_days"] > 0
    assert 0 <= result["active_day_up_rate_pct"] <= 100
    assert "win_rate_pct" not in result


def test_backtest_query_calls_result_a_rule_replay_not_strategy_proof(monkeypatch):
    monkeypatch.setattr(query, "_skill_code_from_query", lambda text: ("603986", ""))
    monkeypatch.setattr(
        query,
        "_skill_request",
        lambda payload: {
            "ok": True,
            "code": "603986",
            "start": "2025-08-01",
            "end": "2026-08-01",
            "bars": 250,
            "entries": 5,
            "exits": 5,
            "net_total_return_pct": 30.0,
            "benchmark_return_pct": 25.0,
            "excess_return_pct_points": 5.0,
            "net_annualized_return_pct": 31.0,
            "benchmark_annualized_return_pct": 26.0,
            "net_max_drawdown_pct": -12.0,
            "benchmark_max_drawdown_pct": -20.0,
            "net_sharpe": 1.2,
            "active_days": 120,
            "active_day_up_rate_pct": 54.0,
            "cost_bps_per_side": 10,
        },
    )

    text = query._query_skill_backtest("/回测 603986")

    assert "固定规则历史回放" in text
    assert "同期买入并持有 25.00%" in text
    assert "超额 5.00 个百分点" in text
    assert "不是交易胜率" in text
    assert "复权、公司行动处理未做第二数据源交叉验证" in text
    assert "样本外或滚动验证" in text
    assert "不能证明 OpenClaw 当前策略有效" in text
    assert "历史胜率" not in text


def test_strategy_query_uses_live_evidence_progress_instead_of_history_rates(monkeypatch, tmp_path):
    config = {
        "weights": {"technical": 0.3, "sentiment": 0.2},
        "auto_adjust_enabled": True,
        "weight_history": [
            {
                "date": "2026-08-08",
                "performance": [
                    {"strategy_used": "technical", "total": 99, "correct": 98, "win_rate": 99.0}
                ],
            }
        ],
    }
    config_path = tmp_path / "strategy_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(query.os.path, "exists", lambda path: True)
    monkeypatch.setattr("builtins.open", lambda path, *args, **kwargs: config_path.open(*args, **kwargs))
    monkeypatch.setattr(
        query,
        "build_evolution_readiness",
        lambda as_of: {
            "ready": False,
            "total": 1,
            "minimum_total": 20,
            "strategies": [
                {"strategy": "technical", "verified": 1, "minimum": 5},
                {"strategy": "sentiment", "verified": 0, "minimum": 5},
            ],
            "maturity_rule": "走满3个交易日后计入",
        },
    )

    text = query._query_strategy()

    assert "自动校准开关：已启用（受证据门槛约束）" in text
    assert "画像化成熟样本 1/20" in text
    assert "技术趋势：成熟验真 1/5" in text
    assert "市场情绪：成熟验真 0/5" in text
    assert "99.0%" not in text
    assert "胜率" not in text

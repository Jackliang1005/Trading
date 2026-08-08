import json
from datetime import date
from datetime import datetime

from domain.services import advisor_brief_service
from domain.services import reflection_runtime_service
from domain.services.decision_outcome_service import (
    _fetch_tencent_kline,
    build_decision_outcomes,
    load_decision_snapshots,
    recent_outcome_summary,
    save_decision_outcomes,
)


def _snapshot(generated_at, code, price, state="reduce_priority"):
    return {
        "generated_at": generated_at,
        "slot": generated_at[11:16],
        "benchmarks": {
            "000001.SH": {
                "price": 4000,
                "as_of": generated_at[:10].replace("-", "") + generated_at[11:19].replace(":", ""),
            }
        },
        "tracked_positions": [
            {
                "code": code,
                "name": "测试持仓",
                "decision_state": state,
                "action_level": "prepare",
                "benchmark_code": "000001.SH",
                "quote": {
                    "price": price,
                    "as_of": generated_at[:10].replace("-", "") + generated_at[11:19].replace(":", ""),
                },
            }
        ],
    }


def _write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_tencent_kline_parser_requires_and_preserves_exact_trade_date(monkeypatch):
    payload = {
        "data": {
            "sh603986": {
                "day": [["2026-08-06", "363.000", "385.000", "394.940", "363.000", "618612.000"]]
            }
        }
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setattr("domain.services.decision_outcome_service.urllib.request.urlopen", lambda request, timeout: Response())

    rows = _fetch_tencent_kline("603986.SH", "2026-08-06", "2026-08-06")

    assert rows == [
        {
            "date": "2026-08-06",
            "open": 363.0,
            "close": 385.0,
            "high": 394.94,
            "low": 363.0,
            "volume": 618612.0,
            "source": "tencent_kline",
        }
    ]


def test_all_same_day_snapshots_are_loaded_and_latest_duplicate_is_removed(tmp_path):
    first = _snapshot("2026-08-10 09:35:00", "600001.SH", 10)
    second = _snapshot("2026-08-10 10:30:00", "600002.SH", 20)
    _write(tmp_path / "investor_decision_monitor_20260810_093500_09_35.json", first)
    _write(tmp_path / "investor_decision_monitor_20260810_103000_10_30.json", second)
    _write(tmp_path / "investor_decision_monitor_latest.json", second)

    snapshots = load_decision_snapshots("2026-08-10", reports_dir=tmp_path)

    assert [item["generated_at"] for item in snapshots] == ["2026-08-10 09:35:00", "2026-08-10 10:30:00"]


def test_downside_advice_is_validated_against_close_and_benchmark(tmp_path):
    snapshot = _snapshot("2026-08-10 09:35:00", "600001.SH", 10)
    _write(tmp_path / "investor_decision_monitor_20260810_093500_09_35.json", snapshot)
    bars = {
        "600001.SH": [{"date": "2026-08-10", "close": 9.7}],
        "000001.SH": [{"date": "2026-08-10", "close": 4020}],
    }

    summary = build_decision_outcomes(
        "2026-08-10",
        reports_dir=tmp_path,
        price_loader=lambda code, start, end: bars.get(code, []),
    )

    assert summary["snapshot_count"] == 1
    assert summary["evaluated_count"] == 1
    assert summary["confirmed_count"] == 1
    assert summary["profiled_evaluated_count"] == 1
    assert summary["legacy_evaluated_count"] == 0
    assert summary["outcomes"][0]["stock_return_pct"] == -3.0
    assert summary["outcomes"][0]["close_source"] == "historical_price_loader"
    assert summary["outcomes"][0]["benchmark_return_pct"] == 0.5
    assert summary["outcomes"][0]["relative_return_pct"] == -3.5
    assert summary["outcomes"][0]["verdict"] == "downside_confirmed"


def test_rising_stock_and_relative_strength_do_not_confirm_downside_advice(tmp_path):
    snapshot = _snapshot("2026-08-10 09:35:00", "600001.SH", 10)
    _write(tmp_path / "investor_decision_monitor_20260810_093500_09_35.json", snapshot)
    bars = {
        "600001.SH": [{"date": "2026-08-10", "close": 10.2}],
        "000001.SH": [{"date": "2026-08-10", "close": 4020}],
    }

    summary = build_decision_outcomes(
        "2026-08-10",
        reports_dir=tmp_path,
        price_loader=lambda code, start, end: bars.get(code, []),
    )

    assert summary["not_confirmed_count"] == 1
    assert summary["outcomes"][0]["verdict"] == "downside_not_confirmed"


def test_observe_state_is_not_misrepresented_as_a_directional_call(tmp_path):
    snapshot = _snapshot("2026-08-10 09:35:00", "600001.SH", 10, state="observe")
    _write(tmp_path / "investor_decision_monitor_20260810_093500_09_35.json", snapshot)

    summary = build_decision_outcomes("2026-08-10", reports_dir=tmp_path, price_loader=lambda *args: [])

    assert summary["directional_count"] == 0
    assert summary["evaluated_count"] == 0


def test_stale_signal_timestamp_is_not_evaluated_with_an_unrelated_close(tmp_path):
    snapshot = _snapshot("2026-08-10 09:35:00", "600001.SH", 10)
    snapshot["tracked_positions"][0]["quote"]["as_of"] = "20260807093500"
    _write(tmp_path / "investor_decision_monitor_20260810_093500_09_35.json", snapshot)

    summary = build_decision_outcomes(
        "2026-08-10",
        reports_dir=tmp_path,
        price_loader=lambda *args: [{"date": "2026-08-10", "close": 9}],
    )

    assert summary["unavailable_count"] == 1
    assert summary["outcomes"][0]["close_price"] is None


def test_saved_daily_outcomes_can_be_aggregated_without_live_price_calls(tmp_path):
    first = {"as_of": "2026-08-10", "evaluated_count": 2, "confirmed_count": 1, "not_confirmed_count": 1}
    second = {"as_of": "2026-08-09", "evaluated_count": 1, "confirmed_count": 1, "mixed_count": 1}
    save_decision_outcomes(first, reports_dir=tmp_path)
    save_decision_outcomes(second, reports_dir=tmp_path)

    summary = recent_outcome_summary(date(2026, 8, 10), days=7, reports_dir=tmp_path)

    assert summary["sessions"] == 2
    assert summary["evaluated_count"] == 3
    assert summary["confirmed_count"] == 2
    assert summary["not_confirmed_count"] == 1
    assert summary["mixed_count"] == 1


def test_reflection_discloses_price_validation_without_calling_it_realized_return(tmp_path, monkeypatch):
    snapshot = _snapshot(datetime.now().strftime("%Y-%m-%d 09:35:00"), "600001.SH", 10)
    path = tmp_path / "investor_decision_monitor_latest.json"
    _write(path, snapshot)
    monkeypatch.setattr(reflection_runtime_service, "DECISION_MONITOR_PATH", path)
    outcome = {
        "directional_count": 1,
        "evaluated_count": 1,
        "confirmed_count": 1,
        "not_confirmed_count": 0,
        "mixed_count": 0,
        "outcomes": [
            {
                "generated_at": snapshot["generated_at"],
                "code": "600001.SH",
                "stock_return_pct": -2.0,
                "relative_return_pct": -2.5,
                "verdict": "downside_confirmed",
            }
        ],
    }

    text = reflection_runtime_service._decision_monitor_attribution({}, outcome_summary=outcome)

    assert "收盘价格验证：明确降风险建议 1 条" in text
    assert "建议后至收盘 -2.00%" in text
    assert "不代表实际成交、收益或长期策略有效性" in text


def test_reflection_uses_last_snapshot_for_trades_but_discloses_all_saved_times(tmp_path, monkeypatch):
    day = datetime.now().strftime("%Y-%m-%d")
    first = _snapshot(f"{day} 09:35:00", "600001.SH", 10)
    second = _snapshot(f"{day} 10:30:00", "600001.SH", 9.8)
    second["tracked_positions"][0]["execution_hint"] = {"actionable": True, "suggested_qty": 100}
    _write(tmp_path / f"investor_decision_monitor_{day.replace('-', '')}_093500_09_35.json", first)
    _write(tmp_path / f"investor_decision_monitor_{day.replace('-', '')}_103000_10_30.json", second)
    _write(tmp_path / "investor_decision_monitor_latest.json", second)
    monkeypatch.setattr(reflection_runtime_service, "DECISION_MONITOR_PATH", tmp_path / "investor_decision_monitor_latest.json")

    text = reflection_runtime_service._decision_monitor_attribution({})

    assert "建议时间：" + day + " 10:30:00" in text
    assert "当日共保存 2 个建议时点" in text
    assert "价格验证覆盖全部时点" in text


def test_advisor_home_view_summarizes_saved_recent_position_advice_outcomes(tmp_path, monkeypatch):
    save_decision_outcomes(
        {
            "as_of": "2026-08-10",
            "evaluated_count": 3,
            "confirmed_count": 2,
            "not_confirmed_count": 1,
            "mixed_count": 0,
            "profiled_evaluated_count": 3,
            "profiled_confirmed_count": 2,
            "profiled_not_confirmed_count": 1,
            "profiled_mixed_count": 0,
            "legacy_evaluated_count": 0,
        },
        reports_dir=tmp_path,
    )
    monkeypatch.setattr(advisor_brief_service, "build_risk_report", lambda: {"available": False, "advisor_policy": {}})
    monkeypatch.setattr(
        advisor_brief_service,
        "build_evolution_readiness",
        lambda as_of=None: {"ready": False, "total": 0, "minimum_total": 20, "strategies": []},
    )

    brief = advisor_brief_service.build_advisor_brief(
        now=datetime(2026, 8, 10, 20, 40),
        reports_dir=tmp_path,
    )

    assert brief["decision_outcomes"]["evaluated_count"] == 3
    assert "完成 3 条当前分层逐仓降风险建议的收盘价格验证" in brief["text"]
    assert "下行得到确认 2 条，未确认 1 条" in brief["text"]
    assert "不代表实际成交、收益或长期策略有效性" in brief["text"]


def test_advisor_keeps_legacy_outcomes_out_of_current_quality_claim(tmp_path, monkeypatch):
    save_decision_outcomes(
        {
            "as_of": "2026-08-10",
            "evaluated_count": 6,
            "confirmed_count": 2,
            "not_confirmed_count": 4,
            "mixed_count": 0,
            "profiled_evaluated_count": 0,
            "legacy_evaluated_count": 6,
        },
        reports_dir=tmp_path,
    )
    monkeypatch.setattr(advisor_brief_service, "build_risk_report", lambda: {"available": False, "advisor_policy": {}})
    monkeypatch.setattr(
        advisor_brief_service,
        "build_evolution_readiness",
        lambda as_of=None: {"ready": False, "total": 0, "minimum_total": 20, "strategies": []},
    )

    brief = advisor_brief_service.build_advisor_brief(
        now=datetime(2026, 8, 10, 20, 40),
        reports_dir=tmp_path,
    )

    assert "6 条旧版未分层建议完成价格回放" in brief["text"]
    assert "其中下行确认 2 条、未确认 4 条" in brief["text"]
    assert "不计入当前分层建议质量" in brief["text"]
    assert "当前分层逐仓降风险建议" not in brief["text"]

import json
from datetime import datetime

from domain.services import reflection_runtime_service as service


def test_reflection_matches_sell_direction_quantity_and_action_level(tmp_path, monkeypatch):
    path = tmp_path / "investor_decision_monitor_latest.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().strftime("%Y-%m-%d 10:30:00"),
                "slot": "10:30 走势修正",
                "tracked_positions": [
                    {
                        "code": "600001.SH",
                        "decision_state": "reduce_priority",
                        "action_level": "prepare",
                        "execution_hint": {"actionable": True, "suggested_qty": 100},
                    },
                    {
                        "code": "603986.SH",
                        "decision_state": "reduce_priority",
                        "action_level": "verify",
                        "execution_hint": {"actionable": False, "suggested_qty": 0},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "DECISION_MONITOR_PATH", path)
    trading_summary = {
        "today_trades": [
            {"stock_code": "600001.SH", "order_type": "sell", "trade_volume": 50, "trade_price": 10},
            {"stock_code": "600001.SH", "order_type": "buy", "trade_volume": 20, "trade_price": 9.8},
            {"stock_code": "603986.SH", "order_type": "sell", "trade_volume": 100, "trade_price": 400},
        ]
    }

    text = service._decision_monitor_attribution(trading_summary)

    assert "时点：10:30 走势修正" in text
    assert "准备级减仓候选：600001.SH 100 股" in text
    assert "已验证卖出 50 股，状态 部分完成" in text
    assert "同日另有买入 20 股" in text
    assert "方向与降风险建议冲突" in text
    assert "核验级风险项：603986.SH" in text
    assert "不把未成交视为执行失败" in text
    assert "603986.SH 卖出 100 股" in text


def test_buy_only_never_counts_as_reduction_completion(tmp_path, monkeypatch):
    path = tmp_path / "investor_decision_monitor_latest.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().strftime("%Y-%m-%d 09:35:00"),
                "slot": "09:35 开盘风险检查",
                "tracked_positions": [
                    {
                        "code": "600001.SH",
                        "decision_state": "reduce_priority",
                        "action_level": "prepare",
                        "execution_hint": {"actionable": True, "suggested_qty": 100},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "DECISION_MONITOR_PATH", path)

    text = service._decision_monitor_attribution(
        {"today_trades": [{"stock_code": "600001.SH", "order_type": "buy", "trade_volume": 100, "trade_price": 10}]}
    )

    assert "时点：09:35 风险检查" in text
    assert "已验证卖出 0 股，状态 无卖出证据" in text
    assert "方向与降风险建议冲突" in text
    assert "状态 已完成" not in text

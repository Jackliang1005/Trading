from domain.services import morning_brief_service as service


def _payload(generated_at):
    return {
        "generated_at": generated_at,
        "risk": {"available": False},
        "overseas": {},
        "global": {},
        "global_impact": {},
        "longterm": {},
    }


def test_weekend_morning_brief_targets_next_trading_day(monkeypatch):
    monkeypatch.setattr(service, "is_cn_trading_day", lambda day: (False, "test_calendar"))
    monkeypatch.setattr(service, "next_trading_day", lambda day: "2026-08-10")

    text = service.format_morning_brief(_payload("2026-08-08 08:30:00"))

    assert "计划交易日：2026-08-10（当前为非交易日" in text
    assert "2026-08-10 开盘执行顺序" in text
    assert "所有 09:25/09:35/10:30 动作均属于计划交易日 2026-08-10" in text


def test_trading_day_premarket_brief_targets_same_day(monkeypatch):
    monkeypatch.setattr(service, "is_cn_trading_day", lambda day: (True, "test_calendar"))

    context = service._morning_session_context("2026-08-10 08:30:00")

    assert context["target_date"] == "2026-08-10"
    assert context["status"] == "当日交易计划"
    assert context["is_current_trading_day"] is True


def test_post_close_manual_brief_targets_following_trading_day(monkeypatch):
    monkeypatch.setattr(service, "is_cn_trading_day", lambda day: (True, "test_calendar"))
    monkeypatch.setattr(service, "next_trading_day", lambda day: "2026-08-11")

    context = service._morning_session_context("2026-08-10 18:00:00")

    assert context["target_date"] == "2026-08-11"
    assert "已收盘" in context["status"]
    assert context["is_current_trading_day"] is False


def test_overseas_timestamps_are_labeled_as_source_market_time(monkeypatch):
    monkeypatch.setattr(service, "is_cn_trading_day", lambda day: (True, "test_calendar"))
    payload = _payload("2026-08-10 08:30:00")
    payload["overseas"] = {
        "us": {
            "available": True,
            "items": [
                {"name": "道琼斯", "change_pct": 0.3, "as_of": "2026-08-07 16:50:36"},
                {"name": "标普500", "change_pct": 0.6, "as_of": "2026-08-07 16:50:24"},
            ],
        }
    }

    text = service.format_morning_brief(payload)

    assert "市场交易日 2026-08-07" in text
    assert "源站市场时间 2026-08-07 16:50:36、2026-08-07 16:50:24" in text
    assert "未统一换算为北京时间" in text
    assert "数据时间 2026-08-07" not in text

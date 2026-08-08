from domain.services import feishu_query_service as service
from domain.services import assistant_status_service as assistant_status


def test_health_keeps_local_status_when_token_is_missing(monkeypatch):
    monkeypatch.setattr(service, "_query_assistant_status", lambda: "LOCAL STATUS")
    monkeypatch.setattr(service, "_resolve_token", lambda: "")

    text = service.handle_feishu_query("/健康")

    assert "LOCAL STATUS" in text
    assert "QMT2HTTP_API_TOKEN" in text
    assert "账户读取与策略日志本次无法核验" in text


def test_health_combines_gateway_positions_and_logs(monkeypatch):
    monkeypatch.setattr(service, "_query_assistant_status", lambda: "LOCAL STATUS")
    monkeypatch.setattr(service, "_resolve_token", lambda: "token")
    monkeypatch.setattr(service, "_query_health", lambda account, token: f"HEALTH {account}")

    def fake_http_get(base_url, path, token, timeout=0):
        assert path == "/api/stock/positions"
        return {"ok": True, "payload": {"success": True, "data": [{"stock_code": "000001"}]}}

    monkeypatch.setattr(service, "_http_get", fake_http_get)
    monkeypatch.setattr(
        service,
        "_collect_trade_logs",
        lambda base_url, token, days=1: [{"ok": True, "line_count": 18, "error_hits": 0}],
    )

    text = service.handle_feishu_query("/健康 国金")

    assert "LOCAL STATUS" in text
    assert "HEALTH guojin" in text
    assert "实时接口返回 1 条持仓记录" in text
    assert "读取 18 行，未命中异常关键词" in text
    assert "不会触发下单或改变仓位" in text


def test_log_errors_before_latest_healthy_heartbeat_are_recovered():
    summary = service._summarize_log_errors(
        [
            "subscribeQuote ERROR invalid stockcode",
            "signal-server heartbeat status=ok positions=4",
        ]
    )

    assert summary["error_hits"] == 0
    assert summary["recovered_error_hits"] == 1


def test_log_errors_after_latest_healthy_heartbeat_remain_active():
    summary = service._summarize_log_errors(
        [
            "signal-server heartbeat status=ok positions=4",
            "ProviderUnavailable Exception: 无法连接行情服务",
        ]
    )

    assert summary["error_hits"] == 1
    assert summary["recovered_error_hits"] == 0
    assert summary["error_categories"] == {"连接链路": 1}
    assert summary["error_signatures"] == ["行情服务不可用"]


def test_assistant_status_uses_automatic_health_probe_state():
    assert assistant_status.REPORT_FILES["health"] == str(assistant_status.HEALTH_STATE_PATH)


def test_log_payload_checks_every_file_without_cross_file_recovery():
    summary = service._summarize_log_payload(
        {
            "entries": [
                {"content": ["ERROR connection refused"]},
                {"content": ["signal-server heartbeat status=ok positions=4"]},
            ]
        }
    )

    assert summary["file_count"] == 2
    assert summary["line_count"] == 2
    assert summary["error_hits"] == 1
    assert summary["recovered_error_hits"] == 0
    assert summary["error_categories"] == {"连接链路": 1}


def test_log_payload_surfaces_qmt_root_cause_signatures():
    summary = service._summarize_log_payload(
        {
            "entries": [
                {
                    "content": [
                        "Traceback (most recent call last):",
                        "RuntimeError: QMT trader connect failed: -1",
                    ]
                },
                {"content": ["Exception: 无法连接行情服务！"]},
            ]
        }
    )

    assert summary["error_hits"] == 2
    assert summary["error_signatures"] == ["QMT交易连接失败", "行情服务不可用"]


def test_assistant_status_names_audit_warning_and_time():
    text = assistant_status.format_assistant_status(
        {
            "generated_at": "2026-08-08 18:20:00",
            "units": {},
            "timers": {},
            "reports": {},
            "audit": {
                "blocked_count": 0,
                "warning_count": 1,
                "blocked": [],
                "warning": ["holdings_account_monitor"],
                "generated_at": "2026-08-08 16:25:35 +0800",
            },
        }
    )

    assert "警告项：持仓与账户监控" in text
    assert "审计时间：2026-08-08 16:25:35 +0800" in text

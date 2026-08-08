from domain.services import feishu_query_service as service


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

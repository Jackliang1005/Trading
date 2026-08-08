from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_push_wrapper_uses_current_openclaw_binary_before_legacy_nvm_copy():
    script = (ROOT / "scripts" / "run_investor_command_push.sh").read_text(encoding="utf-8")

    assert 'OPENCLAW_BIN="${OPENCLAW_BIN:-/usr/local/bin/openclaw}"' in script
    assert '"$OPENCLAW_BIN" message send' in script
    path_line = next(line for line in script.splitlines() if line.startswith("export PATH="))
    assert path_line.index("/usr/local/bin") < path_line.index("/root/.nvm/versions/node/v22.22.0/bin")


def test_services_pin_current_openclaw_binary():
    webhook = (ROOT / "investor" / "deploy" / "feishu-webhook.service").read_text(encoding="utf-8")
    event_dropin = (
        ROOT
        / "investor"
        / "deploy"
        / "systemd"
        / "investor-event-watch.service.d"
        / "20-openclaw-cli.conf"
    ).read_text(encoding="utf-8")

    for unit in (webhook, event_dropin):
        assert "Environment=OPENCLAW_BIN=/usr/local/bin/openclaw" in unit
        path_line = next(line for line in unit.splitlines() if line.startswith("Environment=PATH="))
        assert path_line.index("/usr/local/bin") < path_line.index("/root/.nvm/versions/node/v22.22.0/bin")


def test_webhook_fallback_uses_resolved_current_binary():
    source = (ROOT / "investor" / "feishu_webhook_server.py").read_text(encoding="utf-8")

    assert 'os.environ.get("OPENCLAW_BIN") or "/usr/local/bin/openclaw"' in source
    assert '_openclaw_binary(), "message", "send"' in source


def test_event_fallback_uses_resolved_current_binary():
    source = (ROOT / "investor" / "domain" / "services" / "event_service.py").read_text(encoding="utf-8")

    assert 'os.environ.get("OPENCLAW_BIN") or "/usr/local/bin/openclaw"' in source
    assert '_openclaw_binary(), "message", "send"' in source

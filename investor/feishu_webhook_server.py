#!/usr/bin/env python3
"""
统一飞书 Webhook 服务 — 替代 trading 的 feishu_trading_webhook.py。

监听 8788 端口 /feishu/trading，同时处理：
  - 交易指令 (T 开头) → TradingCommandService
  - 投资查询 (/持仓 /预测 /风险等) → investor query service
  - 自然语言投资查询 → investor query service

回复方式: openclaw message send --channel feishu（与 trading 一致）

启动:
  systemctl enable --now feishu-webhook
  python3 feishu_webhook_server.py --port 8788
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict
from urllib.parse import urlparse

# ── config ──────────────────────────────────────────────────
TRADING_BASE_DIR = Path(os.environ.get("TRADING_BASE_DIR", "/root/.openclaw/workspace/trading")).resolve()
INVESTOR_DIR = Path(os.environ.get("INVESTOR_ROOT", "/root/.openclaw/workspace/investor")).resolve()
LOG_PATH = INVESTOR_DIR / "logs" / "feishu_webhook.log"
DEFAULT_FEISHU_TARGET = "ou_f7d5ef82efd4396dea7a604691c56f75"

FEISHU_VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
FEISHU_ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")

# ── event dedup ─────────────────────────────────────────────
_SEEN_EVENTS: Dict[str, float] = {}
_SEEN_EVENTS_MAX = 1000
_SEEN_EVENTS_TTL = 600

# ── investor imports ────────────────────────────────────────
sys.path.insert(0, str(INVESTOR_DIR))
from domain.services.feishu_query_service import handle_feishu_query

# ── trading imports (lazy, only if needed) ──────────────────
_trading_service = None

def _get_trading_service():
    global _trading_service
    if _trading_service is None:
        sys.path.insert(0, str(Path(os.environ.get(
            "FEISHU_TRADING_SKILL_DIR",
            "/root/.openclaw/workspace/skills/feishu-trading-webhook/scripts",
        ))))
        from trading_command_core import TradingCommandService
        _trading_service = TradingCommandService(TRADING_BASE_DIR)
    return _trading_service


# ── investor command detection ──────────────────────────────
INVESTOR_KEYWORDS = (
    "持仓", "账户", "成交", "委托", "预测", "胜率", "风险", "策略",
    "复盘", "摘要", "监控", "候选", "买入", "敞口", "集中度", "回撤", "ETF", "etf",
    "权重", "简报", "报告",
)
INVESTOR_PREFIX_RE = re.compile(
    r"^(/持仓|/账户|/成交|/委托|/预测|/胜率|/风险|/策略|/复盘|/摘要|/监控|/候选|/买入|/etf|/ETF|/帮助)"
)

def _is_investor_query(text: str) -> bool:
    if INVESTOR_PREFIX_RE.match(text):
        return True
    return any(k in text for k in INVESTOR_KEYWORDS)


# ── logging ─────────────────────────────────────────────────
def log_line(text: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {text}\n")


def _send_feishu(target: str, message: str) -> bool:
    """Send message to Feishu via openclaw."""
    if not message or not message.strip():
        return False
    # Ensure target has user: or chat: prefix
    clean = str(target or "").strip()
    if clean and not clean.startswith(("user:", "chat:")):
        clean = f"user:{clean}"
    cmd = [
        "openclaw", "message", "send",
        "--channel", "feishu",
        "--target", clean,
        "-m", message,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        ok = result.returncode == 0
        if not ok:
            log_line(f"_send_feishu failed: rc={result.returncode} stderr={result.stderr[:200]}")
        return ok
    except Exception as exc:
        log_line(f"_send_feishu exception: {exc}")
        return False


# ── signature ───────────────────────────────────────────────
def compute_signature(timestamp: str, nonce: str, body: str) -> str:
    content = timestamp + nonce + FEISHU_ENCRYPT_KEY + body
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def is_duplicate_event(event_id: str) -> bool:
    if not event_id:
        return False
    now = time.time()
    if len(_SEEN_EVENTS) > _SEEN_EVENTS_MAX:
        expired = [eid for eid, ts in _SEEN_EVENTS.items() if now - ts > _SEEN_EVENTS_TTL]
        for eid in expired:
            del _SEEN_EVENTS[eid]
    if event_id in _SEEN_EVENTS:
        return True
    _SEEN_EVENTS[event_id] = now
    return False


# ── message extraction ──────────────────────────────────────
def _extract_text(payload: dict) -> str:
    """Extract message text from Feishu v2 event payload."""
    event = payload.get("event", {}) if isinstance(payload, dict) else {}
    if isinstance(event, dict):
        message = event.get("message", {})
        if isinstance(message, dict):
            content = message.get("content", "")
            if isinstance(content, str) and content:
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        text = parsed.get("text", "")
                        if text:
                            return str(text).strip()
                except (json.JSONDecodeError, TypeError):
                    return content.strip()
    # fallback: query field
    query = payload.get("query", "")
    if query and isinstance(query, str):
        return query.strip()
    return ""


def _extract_sender_id(payload: dict) -> str:
    """Extract sender open_id from event."""
    event = payload.get("event", {}) if isinstance(payload, dict) else {}
    sender = event.get("sender", {}) if isinstance(event, dict) else {}
    sender_id_obj = sender.get("sender_id", {}) if isinstance(sender, dict) else {}
    return (
        sender_id_obj.get("open_id")
        or sender_id_obj.get("user_id")
        or DEFAULT_FEISHU_TARGET
    )


# ── handler ─────────────────────────────────────────────────
class ReuseAddrHTTPServer(HTTPServer):
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        # Support both /feishu/trading (backward compat) and /feishu/event
        if parsed.path not in ("/feishu/trading", "/feishu/event", "/feishu/investor"):
            self._send_json(404, {"ok": False, "error": "not_found", "path": parsed.path})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length)
        raw_str = raw.decode("utf-8")

        try:
            payload = json.loads(raw_str)
        except Exception:
            self._send_json(400, {"ok": False, "error": "invalid_json"})
            return

        # --- challenge ---
        challenge = payload.get("challenge")
        if challenge:
            token = payload.get("token", "")
            if FEISHU_VERIFICATION_TOKEN and token != FEISHU_VERIFICATION_TOKEN:
                log_line(f"challenge token mismatch: got={token}")
                self._send_json(403, {"ok": False, "error": "token_mismatch"})
                return
            self._send_json(200, {"challenge": challenge})
            return

        # --- signature ---
        if FEISHU_ENCRYPT_KEY:
            header_ts = self.headers.get("X-Lark-Request-Timestamp", "")
            header_nonce = self.headers.get("X-Lark-Request-Nonce", "")
            header_sig = self.headers.get("X-Lark-Signature", "")
            if header_sig:
                expected = compute_signature(header_ts, header_nonce, raw_str)
                if not hmac.compare_digest(header_sig, expected):
                    log_line(f"signature mismatch")
                    self._send_json(403, {"ok": False, "error": "signature_mismatch"})
                    return

        # --- verification token ---
        header_token = payload.get("token", "")
        if FEISHU_VERIFICATION_TOKEN and header_token:
            if header_token != FEISHU_VERIFICATION_TOKEN:
                self._send_json(403, {"ok": False, "error": "token_mismatch"})
                return

        # --- dedup ---
        header_obj = payload.get("header", {})
        event_id = header_obj.get("event_id", "")
        if is_duplicate_event(event_id):
            log_line(f"duplicate event_id={event_id}, skipping")
            self._send_json(200, {"ok": True, "duplicate": True})
            return

        # --- extract ---
        text = _extract_text(payload)
        sender_id = _extract_sender_id(payload)

        if not text:
            log_line(f"empty text, sender={sender_id}")
            self._send_json(200, {"ok": True, "ignored": True})
            return

        log_line(f"event_id={event_id} sender={sender_id} text={text[:100]}")

        # --- route: investor query ---
        if _is_investor_query(text):
            try:
                reply = handle_feishu_query(text)
            except Exception as exc:
                reply = f"投资查询出错: {exc}"
                log_line(f"investor error: {exc}")

            sent = _send_feishu(sender_id, reply)
            log_line(f"investor replied={sent} text={text[:60]}")
            self._send_json(200, {"ok": True, "route": "investor", "replied": sent})
            return

        # --- route: trading command (backward compat) ---
        if text.startswith("T") or text.startswith("t"):
            try:
                svc = _get_trading_service()
                reply = svc.handle_command(text)
                sent = svc.send_reply(sender_id, reply)
            except Exception as exc:
                reply = f"交易指令处理失败：{exc}"
                sent = _send_feishu(sender_id, reply)
                log_line(f"trading error: {exc}")

            log_line(f"trading replied={sent} text={text[:60]}")
            self._send_json(200, {"ok": True, "route": "trading", "replied": sent})
            return

        # --- unknown message ---
        log_line(f"ignored non-command: {text[:80]}")
        self._send_json(200, {"ok": True, "route": "none", "ignored": True})

    def log_message(self, format, *args):
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="统一飞书 Webhook（Investor + Trading）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    sig_status = "已启用" if FEISHU_ENCRYPT_KEY else "未配置"
    token_status = "已启用" if FEISHU_VERIFICATION_TOKEN else "未配置"
    print(f"签名校验: {sig_status}")
    print(f"Token校验: {token_status}")
    print(f"事件去重: TTL={_SEEN_EVENTS_TTL}s max={_SEEN_EVENTS_MAX}")
    print(f"路由规则: /持仓等→investor | T开头→trading")
    print(f"日志: {LOG_PATH}")
    print(f"监听: http://{args.host}:{args.port}/feishu/trading")

    server = ReuseAddrHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down...")
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

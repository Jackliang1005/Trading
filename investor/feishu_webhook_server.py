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
import urllib.request
from urllib.parse import urlparse

# ── config ──────────────────────────────────────────────────
TRADING_BASE_DIR = Path(os.environ.get("TRADING_BASE_DIR", "/root/.openclaw/workspace/trading")).resolve()
INVESTOR_DIR = Path(os.environ.get("INVESTOR_ROOT", "/root/.openclaw/workspace/investor")).resolve()
LOG_PATH = INVESTOR_DIR / "logs" / "feishu_webhook.log"
DEFAULT_FEISHU_TARGET = "ou_f7d5ef82efd4396dea7a604691c56f75"
OPENCLAW_FEISHU_SEND_TIMEOUT = int(os.environ.get("OPENCLAW_FEISHU_SEND_TIMEOUT", "180"))

FEISHU_VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
FEISHU_ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")

# ── event dedup ─────────────────────────────────────────────
_SEEN_EVENTS: Dict[str, float] = {}
_SEEN_EVENTS_MAX = 1000
_SEEN_EVENTS_TTL = 600

# ── image+command pairing cache ─────────────────────────────
# Feishu sends text and image separately. Cache one, wait for the other.
# Keyed by sender_id.
_PENDING_IMAGES: Dict[str, tuple] = {}      # sender_id -> (image_key, timestamp)
_PENDING_COMMANDS: Dict[str, tuple] = {}    # sender_id -> (command_type, timestamp)
_PAIR_TIMEOUT = 60  # seconds to wait for the counterpart

# ── investor imports ────────────────────────────────────────
sys.path.insert(0, str(INVESTOR_DIR))
from domain.services.feishu_query_service import handle_feishu_query
from domain.services.report_style_service import (
    build_report_card,
    is_diagnostic_message,
    report_quality_issues,
)
sys.path.insert(0, str(TRADING_BASE_DIR))
from trading_core_new.longterm.notifier import push_feishu_rich, record_feishu_delivery

# ── trading imports (lazy, only if needed) ──────────────────
_trading_service = None
_longterm_manual_handler = None

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


def _get_longterm_manual_handler():
    global _longterm_manual_handler
    if _longterm_manual_handler is None:
        sys.path.insert(0, str(TRADING_BASE_DIR))
        from trading_core_new.longterm.cli import handle_feishu_manual_command
        _longterm_manual_handler = handle_feishu_manual_command
    return _longterm_manual_handler


_longterm_settle_module = None


def _handle_longterm_settle(image_key: str, text: str, sender_id: str) -> str:
    """Download Feishu image, extract trades via vision LLM, apply to portfolio."""
    global _longterm_settle_module
    if _longterm_settle_module is None:
        sys.path.insert(0, str(TRADING_BASE_DIR))
        from trading_core_new.longterm import cli as _m
        from trading_core_new.longterm import repository as _repo_m
        _longterm_settle_module = (_m, _repo_m)

    cli_mod, repo_mod = _longterm_settle_module

    # Download image from Feishu
    image_bytes = _download_feishu_image(image_key)
    if not image_bytes:
        return "无法从飞书下载交割截图，请确认图片已上传"

    # Save to temp
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_path = Path(tmp.name)
    tmp.write(image_bytes)
    tmp.close()

    try:
        # Extract trades using vision LLM
        repo = repo_mod.LongTermRepository()
        repo.init_if_missing()
        trade_date = datetime.now().strftime("%Y-%m-%d")
        records = cli_mod._extract_trades_from_image(tmp_path, trade_date)

        if not records:
            return "未能从截图中识别出成交记录，请确认截图清晰并包含完整交易信息（代码、数量、价格、方向）"

        # Summary
        buys = [r for r in records if r.side == "buy"]
        sells = [r for r in records if r.side == "sell"]
        lines = [f"📸 从截图中识别到 {len(records)} 条成交记录:"]
        total_buy = total_sell = 0.0
        for r in sells:
            amt = r.price * r.quantity
            total_sell += amt
            lines.append(f"  🔴 卖出 {r.code} {r.quantity}股 @{r.price:.3f}")
        for r in buys:
            amt = r.price * r.quantity
            total_buy += amt
            lines.append(f"  🟢 买入 {r.code} {r.quantity}股 @{r.price:.3f}")
        lines.append(f"  卖出总额: ¥{total_sell:,.2f}")
        lines.append(f"  买入总额: ¥{total_buy:,.2f}")

        # Apply
        result = cli_mod._apply_manual_records(
            repo, records=records, as_of=trade_date, source="feishu-settle-image"
        )
        next_state = result["portfolio"]
        lines.append(f"\n✅ 已回填: NAV ¥{next_state.nav:,.2f} 持仓{len(next_state.positions)}只 现金¥{next_state.cash:,.2f}")
        return "\n".join(lines)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _handle_longterm_test(image_key: str, text: str, sender_id: str) -> str:
    """Preview-only: download image, extract trades, show result WITHOUT applying."""
    global _longterm_settle_module
    if _longterm_settle_module is None:
        sys.path.insert(0, str(TRADING_BASE_DIR))
        from trading_core_new.longterm import cli as _m
        from trading_core_new.longterm import repository as _repo_m
        _longterm_settle_module = (_m, _repo_m)

    cli_mod, repo_mod = _longterm_settle_module

    image_bytes = _download_feishu_image(image_key)
    if not image_bytes:
        return "无法从飞书下载截图，请确认图片已上传"

    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_path = Path(tmp.name)
    tmp.write(image_bytes)
    tmp.close()

    try:
        trade_date = datetime.now().strftime("%Y-%m-%d")
        records = cli_mod._extract_trades_from_image(tmp_path, trade_date)

        if not records:
            return "未能从截图中识别出成交记录，请确认截图清晰并包含完整交易信息"

        buys = [r for r in records if r.side == "buy"]
        sells = [r for r in records if r.side == "sell"]
        lines = [f"🧪 测试模式（未修改持仓）"]
        lines.append(f"📸 从截图中识别到 {len(records)} 条成交记录:")
        total_buy = total_sell = 0.0
        for r in sells:
            amt = r.price * r.quantity
            total_sell += amt
            lines.append(f"  🔴 卖出 {r.code} {r.quantity}股 @{r.price:.3f}  ¥{amt:,.2f}")
        for r in buys:
            amt = r.price * r.quantity
            total_buy += amt
            lines.append(f"  🟢 买入 {r.code} {r.quantity}股 @{r.price:.3f}  ¥{amt:,.2f}")
        lines.append(f"  卖出总额: ¥{total_sell:,.2f}")
        lines.append(f"  买入总额: ¥{total_buy:,.2f}")
        lines.append(f"  净额: ¥{total_sell - total_buy:,.2f}")
        lines.append(f"\n⚠️ 以上仅预览，未回填持仓。确认无误后请用 /长线交割 正式执行")
        return "\n".join(lines)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _handle_longterm_position_sync(image_key: str, text: str, sender_id: str) -> str:
    """Download Feishu image, extract positions + cash via vision LLM, sync to portfolio."""
    global _longterm_settle_module
    if _longterm_settle_module is None:
        sys.path.insert(0, str(TRADING_BASE_DIR))
        from trading_core_new.longterm import cli as _m
        from trading_core_new.longterm import repository as _repo_m
        _longterm_settle_module = (_m, _repo_m)

    cli_mod, repo_mod = _longterm_settle_module

    image_bytes = _download_feishu_image(image_key)
    if not image_bytes:
        return "无法从飞书下载持仓截图，请确认图片已上传"

    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_path = Path(tmp.name)
    tmp.write(image_bytes)
    tmp.close()

    try:
        repo = repo_mod.LongTermRepository()
        repo.init_if_missing()
        reply = cli_mod.handle_feishu_position_sync(tmp_path, repo)
        return reply
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


# ── investor command detection ──────────────────────────────
INVESTOR_KEYWORDS = (
    "持仓", "账户", "成交", "委托", "预测", "胜率", "风险", "策略",
    "复盘", "摘要", "监控", "候选", "买入", "交易建议", "持仓建议", "建议", "行情", "技术分析", "技术面", "MACD", "KDJ", "RSI", "布林", "涨停", "连板", "情绪", "回测", "财务", "基本面", "估值", "PE", "PB", "PEG", "ROE", "毛利率", "负债率", "公告", "业绩预告", "减持", "解读", "筛选", "选股", "宏观", "CPI", "PPI", "M2", "社融", "LPR", "社零", "敞口", "集中度", "回撤", "ETF", "etf", "global", "breaking", "全球", "突发", "海外", "审计", "能力审计", "阻塞项", "audit", "capability",
    "权重", "简报", "报告", "健康", "全球", "突发", "海外", "帮助", "菜单", "命令",
)
INVESTOR_PREFIX_RE = re.compile(
    r"^(/持仓|/账户|/成交|/委托|/预测|/胜率|/风险|/策略|/复盘|/摘要|/监控|/候选|/买入|/交易建议|/建议|/行情|/报价|/技术|/情绪|/涨停|/连板|/回测|/backtest|/财务|/基本面|/公告|/解读|/筛选|/选股|/etf|/ETF|/帮助|/菜单|/命令|/健康|/全球|/突发|/海外)"
)
LONGTERM_MANUAL_RE = re.compile(r"^\s*/?(长线成交|长线执行|长线回填|长线帮助|长线命令)\b", re.IGNORECASE)
LONGTERM_SETTLE_RE = re.compile(r"^\s*/?长线交割\b", re.IGNORECASE)
LONGTERM_TEST_RE = re.compile(r"^\s*/?长线测试\b", re.IGNORECASE)
LONGTERM_POSITION_SYNC_RE = re.compile(r"^\s*/?(更新持仓|同步持仓)\b", re.IGNORECASE)

def _is_investor_query(text: str) -> bool:
    if INVESTOR_PREFIX_RE.match(text):
        return True
    return any(k in text for k in INVESTOR_KEYWORDS)


def _is_longterm_manual_command(text: str) -> bool:
    return bool(LONGTERM_MANUAL_RE.match(str(text or "").strip()))


def _is_longterm_settle_command(text: str) -> bool:
    return bool(LONGTERM_SETTLE_RE.match(str(text or "").strip()))


def _is_longterm_test_command(text: str) -> bool:
    return bool(LONGTERM_TEST_RE.match(str(text or "").strip()))


def _is_longterm_position_sync_command(text: str) -> bool:
    return bool(LONGTERM_POSITION_SYNC_RE.match(str(text or "").strip()))


def _pair_image_with_command(sender_id: str, image_key: str = "", text: str = "") -> tuple[str, str] | None:
    """Pair an image with a command. Either arrives first; the second triggers processing.
    Returns (image_key, command_type) when paired, or None if waiting."""
    now = time.time()

    # Cleanup stale entries
    for cache in (_PENDING_IMAGES, _PENDING_COMMANDS):
        stale = [k for k, v in cache.items() if now - v[1] > _PAIR_TIMEOUT]
        for k in stale:
            del cache[k]

    cmd_type = ""
    if _is_longterm_test_command(text):
        cmd_type = "test"
    elif _is_longterm_settle_command(text):
        cmd_type = "settle"
    elif _is_longterm_position_sync_command(text):
        cmd_type = "position_sync"

    # Case 1: text command arrives, check for pending image
    if cmd_type and not image_key:
        if sender_id in _PENDING_IMAGES:
            cached_key, _ = _PENDING_IMAGES.pop(sender_id)
            log_line(f"PAIR: command arrived second, found pending image for {sender_id}")
            return (cached_key, cmd_type)
        # Store command, wait for image
        _PENDING_COMMANDS[sender_id] = (cmd_type, now)
        log_line(f"PAIR: command '{cmd_type}' waiting for image from {sender_id}")
        return None

    # Case 2: image arrives, check for pending command
    if image_key and not cmd_type:
        if sender_id in _PENDING_COMMANDS:
            cached_cmd, _ = _PENDING_COMMANDS.pop(sender_id)
            log_line(f"PAIR: image arrived second, found pending command '{cached_cmd}' for {sender_id}")
            return (image_key, cached_cmd)
        # Store image, wait for command
        _PENDING_IMAGES[sender_id] = (image_key, now)
        log_line(f"PAIR: image waiting for command from {sender_id}")
        return None

    # Case 3: both already in same message (unlikely but handle)
    if cmd_type and image_key:
        return (image_key, cmd_type)

    return None


def _extract_image_key(payload: dict) -> str:
    """Extract image_key from Feishu v2 event payload (message with image)."""
    event = payload.get("event", {}) if isinstance(payload, dict) else {}
    if isinstance(event, dict):
        message = event.get("message", {})
        if isinstance(message, dict):
            content = message.get("content", "")
            if isinstance(content, str) and content:
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        # Single image in message
                        image_key = parsed.get("image_key", "")
                        if image_key:
                            return str(image_key).strip()
                except (json.JSONDecodeError, TypeError):
                    pass
    return ""


def _get_feishu_tenant_token() -> str:
    """Obtain Feishu tenant_access_token."""
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        cfg_path = Path("~/.openclaw/openclaw.json").expanduser()
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        feishu = ((cfg.get("channels", {}) or {}).get("feishu", {}) or {})
        app_id = str(feishu.get("appId", "") or "").strip()
        app_secret = str(feishu.get("appSecret", "") or "").strip()
    if not app_id or not app_secret:
        return ""
    try:
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return str(data.get("tenant_access_token", "") or "").strip()
    except Exception as exc:
        log_line(f"_get_feishu_tenant_token error: {exc}")
        return ""


def _download_feishu_image(image_key: str) -> bytes | None:
    """Download image bytes from Feishu by image_key."""
    token = _get_feishu_tenant_token()
    if not token:
        return None
    try:
        req = urllib.request.Request(
            f"https://open.feishu.cn/open-apis/im/v1/images/{image_key}",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as exc:
        log_line(f"_download_feishu_image error: {exc}")
        return None


# ── logging ─────────────────────────────────────────────────
def log_line(text: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {text}\n")


def _send_feishu(target: str, message: str) -> bool:
    """Send a unified rich card; retain raw diagnostics only on failure paths."""
    if not message or not message.strip():
        return False
    # Ensure target has user: or chat: prefix
    clean = str(target or "").strip()
    if clean and not clean.startswith(("user:", "chat:")):
        clean = f"user:{clean}"
    diagnostic = is_diagnostic_message(message)
    issues = [] if diagnostic else report_quality_issues(message)
    send_text = str(message)
    if issues:
        send_text = "正常查询结果未通过报告质量检查，原始正文未发送。\n" + "\n".join(f"- {item}" for item in issues)
    normal_template = "green" if send_text.startswith("✅") else "orange" if send_text.startswith("⚠️") else "blue"
    card = build_report_card(
        send_text,
        template="red" if diagnostic else "orange" if issues else normal_template,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    if push_feishu_rich(
        send_text,
        card=card,
        diagnostic=diagnostic or bool(issues),
        target=clean,
    ):
        return True
    if issues:
        log_line(f"_send_feishu quality gate blocked fallback: {issues}")
        return False
    cmd = [
        "openclaw", "message", "send",
        "--channel", "feishu",
        "--target", clean,
        "-m", message,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=OPENCLAW_FEISHU_SEND_TIMEOUT,
        )
        ok = result.returncode == 0
        record_feishu_delivery(
            text=message,
            card=card,
            diagnostic=diagnostic,
            target=clean,
            transport="webhook_raw_fallback",
            sent=ok,
        )
        if not ok:
            log_line(f"_send_feishu failed: rc={result.returncode} stderr={result.stderr[:200]}")
        return ok
    except Exception as exc:
        record_feishu_delivery(
            text=message,
            card=card,
            diagnostic=diagnostic,
            target=clean,
            transport="webhook_raw_fallback",
            sent=False,
        )
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
        image_key = _extract_image_key(payload)

        # Debug: log raw payload summary for image messages
        if image_key or (not text):
            msg_type = ""
            event = payload.get("event", {})
            if isinstance(event, dict):
                msg = event.get("message", {})
                if isinstance(msg, dict):
                    msg_type = msg.get("message_type", "")
            log_line(f"DEBUG event_id={event_id} sender={sender_id} text=[{text[:80]}] image_key=[{image_key[:20]}] msg_type={msg_type}")

        # --- pairing: handle pure image (no text) ---
        if not text and image_key:
            paired = _pair_image_with_command(sender_id, image_key=image_key)
            if paired:
                paired_key, cmd_type = paired
                try:
                    if cmd_type == "test":
                        reply = _handle_longterm_test(paired_key, "/长线测试", sender_id)
                    elif cmd_type == "position_sync":
                        reply = _handle_longterm_position_sync(paired_key, "/更新持仓", sender_id)
                    else:
                        reply = _handle_longterm_settle(paired_key, "/长线交割", sender_id)
                except Exception as exc:
                    reply = f"处理失败: {exc}"
                    log_line(f"longterm pair(error) cmd={cmd_type}: {exc}")
                sent = _send_feishu(sender_id, reply)
                log_line(f"longterm paired image-first replied={sent}")
                self._send_json(200, {"ok": True, "route": "longterm_paired_image", "replied": sent})
                return
            # Image stored in pending cache, wait for command
            self._send_json(200, {"ok": True, "route": "pending_image"})
            return

        if not text:
            log_line(f"empty text, sender={sender_id}")
            self._send_json(200, {"ok": True, "ignored": True})
            return

        log_line(f"event_id={event_id} sender={sender_id} text={text[:100]}")

        # --- route: longterm test (preview only) ---
        if _is_longterm_test_command(text):
            paired = _pair_image_with_command(sender_id, image_key=image_key, text=text)
            if paired is None:
                # No image yet, stored command in pending cache
                _send_feishu(sender_id, "已收到 /长线测试 指令，请发送交割截图")
                self._send_json(200, {"ok": True, "route": "longterm_test_waiting"})
                return
            paired_key, _ = paired
            try:
                reply = _handle_longterm_test(paired_key, text, sender_id)
            except Exception as exc:
                reply = f"测试失败: {exc}"
                log_line(f"longterm test error: {exc}")
            sent = _send_feishu(sender_id, reply)
            log_line(f"longterm_test replied={sent}")
            self._send_json(200, {"ok": True, "route": "longterm_test", "replied": sent})
            return

        # --- route: longterm settle from image ---
        if _is_longterm_settle_command(text):
            paired = _pair_image_with_command(sender_id, image_key=image_key, text=text)
            if paired is None:
                _send_feishu(sender_id, "已收到 /长线交割 指令，请发送交割截图")
                self._send_json(200, {"ok": True, "route": "longterm_settle_waiting"})
                return
            paired_key, _ = paired
            try:
                reply = _handle_longterm_settle(paired_key, text, sender_id)
            except Exception as exc:
                reply = f"交割图片处理失败: {exc}"
                log_line(f"longterm settle error: {exc}")
            sent = _send_feishu(sender_id, reply)
            log_line(f"longterm_settle replied={sent}")
            self._send_json(200, {"ok": True, "route": "longterm_settle", "replied": sent})
            return

        # --- route: longterm position sync from image ---
        if _is_longterm_position_sync_command(text):
            paired = _pair_image_with_command(sender_id, image_key=image_key, text=text)
            if paired is None:
                _send_feishu(sender_id, "已收到 /更新持仓 指令，请发送持仓截图")
                self._send_json(200, {"ok": True, "route": "longterm_position_sync_waiting"})
                return
            paired_key, _ = paired
            try:
                reply = _handle_longterm_position_sync(paired_key, text, sender_id)
            except Exception as exc:
                reply = f"持仓截图处理失败: {exc}"
                log_line(f"longterm position_sync error: {exc}")
            sent = _send_feishu(sender_id, reply)
            log_line(f"longterm_position_sync replied={sent}")
            self._send_json(200, {"ok": True, "route": "longterm_position_sync", "replied": sent})
            return

        # --- route: longterm manual command ---
        if _is_longterm_manual_command(text):
            try:
                handler = _get_longterm_manual_handler()
                reply = handler(text)
            except Exception as exc:
                reply = f"长线成交命令处理失败: {exc}"
                log_line(f"longterm manual error: {exc}")
            sent = _send_feishu(sender_id, reply)
            log_line(f"longterm_manual replied={sent} text={text[:60]}")
            self._send_json(200, {"ok": True, "route": "longterm_manual", "replied": sent})
            return

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
    print(f"路由规则: /长线交割+图片→settle | /长线成交→manual | /持仓等→investor | T开头→trading")
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

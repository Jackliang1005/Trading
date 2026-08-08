#!/usr/bin/env bash
set -uo pipefail

TITLE="${1:-investor-command}"
TIMEOUT_SECONDS="${2:-180}"
shift 2 || true
if [[ "$#" -lt 1 ]]; then
  echo "usage: $0 <title> <timeout_seconds> <command...>" >&2
  exit 64
fi

export PATH="/usr/local/bin:/root/.local/share/pnpm:/root/.nvm/versions/node/v22.22.0/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/root/bin:$PATH"
OPENCLAW_BIN="${OPENCLAW_BIN:-/usr/local/bin/openclaw}"
if [[ ! -x "$OPENCLAW_BIN" ]]; then
  OPENCLAW_BIN="$(command -v openclaw || true)"
fi
TARGET="${INVESTOR_FEISHU_TARGET:-user:ou_f7d5ef82efd4396dea7a604691c56f75}"
if [[ -n "$TARGET" && "$TARGET" != user:* && "$TARGET" != chat:* ]]; then
  TARGET="user:$TARGET"
fi
OPENCLAW_FEISHU_SEND_TIMEOUT="${OPENCLAW_FEISHU_SEND_TIMEOUT:-180}"

TMP="$(mktemp /tmp/openclaw_investor_push.XXXXXX)"
STARTED="$(date '+%F %T %Z')"
set +e
timeout "$TIMEOUT_SECONDS" "$@" >"$TMP" 2>&1
RC=$?
set -e
ENDED="$(date '+%F %T %Z')"
BODY="$(sed -n '1,160p' "$TMP")"
rm -f "$TMP"
if [[ -z "$BODY" ]]; then
  BODY="(command produced no output)"
fi

STATUS="ok"
EXPECTED_EXIT_CODES=",${INVESTOR_EXPECTED_EXIT_CODES:-},"
if [[ "$EXPECTED_EXIT_CODES" == *",$RC,"* ]]; then
  STATUS="degraded"
elif [[ "$RC" == "124" ]]; then
  STATUS="timeout"
elif [[ "$RC" != "0" ]]; then
  STATUS="failed"
fi

# Successful maintenance jobs are operational noise, not investor reports.
# Their failures still go through the normal diagnostic card path.
SKIP_NORMAL_PUSH=0
if [[ "$STATUS" == "ok" ]]; then
  case "$TITLE" in
    investor-collect|investor-capability-audit)
      SKIP_NORMAL_PUSH=1
      ;;
    investor-briefing-1320|investor-briefing-1420)
      if grep -q "当前没有读取到候选、委托、成交或ETF持仓" <<<"$BODY"; then
        SKIP_NORMAL_PUSH=1
      fi
      ;;
  esac
fi

MESSAGE="[OpenClaw scheduled] $TITLE
status: $STATUS rc=$RC
started: $STARTED
ended: $ENDED
command: $*

$BODY"

if [[ "$SKIP_NORMAL_PUSH" == "1" ]]; then
  echo "normal success push suppressed for $TITLE" >&2
elif [[ "${INVESTOR_PUSH_DRY_RUN:-0}" == "1" ]]; then
  echo "push_dry_run target=$TARGET" >&2
elif [[ -n "$TARGET" ]]; then
  MSG_TMP="$(mktemp /tmp/openclaw_investor_message.XXXXXX)"
  BODY_TMP="$(mktemp /tmp/openclaw_investor_body.XXXXXX)"
  printf '%s\n' "$MESSAGE" >"$MSG_TMP"
  printf '%s\n' "$BODY" >"$BODY_TMP"
  OPENCLAW_PUSH_TITLE="$TITLE" \
  OPENCLAW_PUSH_STATUS="$STATUS" \
  OPENCLAW_PUSH_RC="$RC" \
  OPENCLAW_PUSH_STARTED="$STARTED" \
  OPENCLAW_PUSH_ENDED="$ENDED" \
  OPENCLAW_PUSH_COMMAND="$*" \
  FEISHU_LONGTERM_TARGET="$TARGET" \
PYTHONPATH="/root/.openclaw/workspace/trading:${PYTHONPATH:-}" \
  python3 - "$MSG_TMP" "$BODY_TMP" <<'PY'
import os
import sys
from pathlib import Path
from trading_core_new.longterm.notifier import push_feishu_rich
sys.path.insert(0, "/root/.openclaw/workspace/investor")
from domain.services.report_style_service import build_report_card, report_quality_issues

msg_path = Path(sys.argv[1])
body_path = Path(sys.argv[2])
fallback_text = msg_path.read_text(encoding="utf-8", errors="replace")
body = body_path.read_text(encoding="utf-8", errors="replace").strip()

def clean(value, default=""):
    text = str(value or default).replace(chr(13), "").strip()
    return text

def clip(value, limit):
    value = clean(value)
    if len(value) <= limit:
        return value
    return value[:limit] + chr(10) + chr(10) + "...[truncated]"

title = clean(os.environ.get("OPENCLAW_PUSH_TITLE"), "investor-command")
display_title = title.replace("_", " ").replace("-", " ").title()
display_title = {
    "investor-closing-brief": "投资助理收盘简报",
    "investor-morning-brief": "投资助理盘前简报",
    "investor-risk-report": "投资助理风险报告",
    "investor-capability-audit": "投资助理能力审计",
    "investor-predict": "09:30 开盘预测",
    "investor-decision-0935": "09:35 开盘风险检查",
    "investor-decision-1030": "10:30 走势修正",
    "investor-outlook-1430": "14:30 预测复盘与仓位决策",
    "investor-weekly-report": "投资助理周报",
    "investor-reflect": "每日交易复盘",
    "investor-briefing-0945": "09:45 东莞策略检查",
    "investor-briefing-1320": "13:20 国金ETF简报",
    "investor-briefing-1420": "14:20 国金ETF简报",
}.get(title, display_title)
status = clean(os.environ.get("OPENCLAW_PUSH_STATUS"), "unknown")
rc = clean(os.environ.get("OPENCLAW_PUSH_RC"), "?")
started = clean(os.environ.get("OPENCLAW_PUSH_STARTED"), "-")
ended = clean(os.environ.get("OPENCLAW_PUSH_ENDED"), "-")
command = clean(os.environ.get("OPENCLAW_PUSH_COMMAND"), "-")
body = clip(body or "(command produced no output)", 5500)
command = clip(command, 900)

template = "green" if status == "ok" else "orange" if status in {"timeout", "degraded"} else "red"
quality_issues = report_quality_issues(body) if status == "ok" else []
diagnostic_elements = [
    {
        "tag": "div",
        "fields": [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**Status**\n{status}  rc={rc}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**Started**\n{started}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**Ended**\n{ended}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**Task**\n{title}"}},
        ],
    },
    {"tag": "hr"},
]
if title != "investor-capability-audit":
    diagnostic_elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**Command**\n`{command}`"}})
content_elements = [
    {"tag": "div", "text": {"tag": "lark_md", "content": body}},
    {"tag": "note", "elements": [{"tag": "plain_text", "content": f"生成时间：{ended}"}]},
]
if quality_issues:
    template = "orange"
    content_elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "**正常报告已被质量门禁拦截，未发送原始正文。**\n" + "\n".join(f"- {item}" for item in quality_issues)}},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": f"任务：{title}｜生成时间：{ended}"}]},
    ]
card = {
    "config": {"wide_screen_mode": True},
    "header": {
        "template": template,
        "title": {"tag": "plain_text", "content": f"OpenClaw - {'报告质量拦截' if quality_issues else display_title}"},
    },
    "elements": (diagnostic_elements if status != "ok" else []) + content_elements,
}
if status == "ok" and not quality_issues:
    report_template = {
        "investor-risk-report": "orange",
        "investor-decision-0935": "orange",
        "investor-outlook-1430": "orange",
        "investor-reflect": "purple",
    }.get(title, "blue")
    card = build_report_card(
        body,
        title=f"OpenClaw - {display_title}",
        template=report_template,
        generated_at=ended,
    )
transport_text = body if status == "ok" else fallback_text
sent = push_feishu_rich(
    transport_text,
    card=card,
    diagnostic=status != "ok" or bool(quality_issues),
)
if sent:
    raise SystemExit(0)
# A body rejected by the quality gate must never escape through the raw-text fallback.
raise SystemExit(42 if quality_issues else 1)
PY
  SEND_RC=$?
  rm -f "$MSG_TMP" "$BODY_TMP"
  if [[ "$SEND_RC" == "42" ]]; then
    echo "report quality gate blocked raw-text fallback for $TITLE" >&2
  elif [[ "$SEND_RC" != "0" ]]; then
    FALLBACK_MESSAGE="$MESSAGE"
    if [[ "$STATUS" == "ok" ]]; then
      FALLBACK_MESSAGE="$BODY"
    fi
    set +e
    timeout "$OPENCLAW_FEISHU_SEND_TIMEOUT" "$OPENCLAW_BIN" message send --channel feishu --target "$TARGET" -m "$FALLBACK_MESSAGE" >/tmp/openclaw_investor_push_send.out 2>&1
    RAW_SEND_RC=$?
    set -e
    printf '%s' "$FALLBACK_MESSAGE" | \
      FEISHU_LONGTERM_TARGET="$TARGET" \
      OPENCLAW_AUDIT_TITLE="$TITLE" \
      OPENCLAW_AUDIT_STATUS="$STATUS" \
      OPENCLAW_AUDIT_SENT="$([[ "$RAW_SEND_RC" == "0" ]] && echo 1 || echo 0)" \
      PYTHONPATH="/root/.openclaw/workspace/trading:${PYTHONPATH:-}" \
      python3 -c 'import os,sys; from trading_core_new.longterm.notifier import record_feishu_delivery; record_feishu_delivery(text=sys.stdin.read(), card=None, diagnostic=os.environ.get("OPENCLAW_AUDIT_STATUS") != "ok", target=os.environ.get("FEISHU_LONGTERM_TARGET", ""), transport="wrapper_raw_fallback", sent=os.environ.get("OPENCLAW_AUDIT_SENT") == "1", report_title=os.environ.get("OPENCLAW_AUDIT_TITLE", ""), message_format="text")' || true
    if [[ "$RAW_SEND_RC" != "0" ]]; then
      echo "openclaw send failed:" >&2
      tail -n 20 /tmp/openclaw_investor_push_send.out >&2 || true
    fi
  fi
fi

printf '%s\n' "$MESSAGE"
exit "$RC"

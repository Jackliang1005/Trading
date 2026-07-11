#!/usr/bin/env bash
set -uo pipefail

TITLE="${1:-investor-command}"
TIMEOUT_SECONDS="${2:-180}"
shift 2 || true
if [[ "$#" -lt 1 ]]; then
  echo "usage: $0 <title> <timeout_seconds> <command...>" >&2
  exit 64
fi

export PATH="/root/.nvm/versions/node/v22.22.0/bin:/root/.local/share/pnpm:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/root/bin:$PATH"
TARGET="${INVESTOR_FEISHU_TARGET:-user:ou_f7d5ef82efd4396dea7a604691c56f75}"
if [[ -n "$TARGET" && "$TARGET" != user:* && "$TARGET" != chat:* ]]; then
  TARGET="user:$TARGET"
fi

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
if [[ "$RC" == "124" ]]; then
  STATUS="timeout"
elif [[ "$RC" != "0" ]]; then
  STATUS="failed"
fi

MESSAGE="[OpenClaw scheduled] $TITLE
status: $STATUS rc=$RC
started: $STARTED
ended: $ENDED
command: $*

$BODY"

if [[ "${INVESTOR_PUSH_DRY_RUN:-0}" == "1" ]]; then
  echo "push_dry_run target=$TARGET" >&2
elif [[ -n "$TARGET" ]]; then
  openclaw message send --channel feishu --target "$TARGET" -m "$MESSAGE" >/tmp/openclaw_investor_push_send.out 2>&1 || {
    echo "openclaw send failed:" >&2
    tail -n 20 /tmp/openclaw_investor_push_send.out >&2 || true
  }
fi

printf '%s\n' "$MESSAGE"
exit "$RC"


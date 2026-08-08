#!/usr/bin/env bash
set -euo pipefail

UNIT_NAME="${1:-unknown.service}"
HOST="$(hostname)"
NOW="$(date '+%F %T')"
MSG="时间：${NOW}
服务：${UNIT_NAME}
主机：${HOST}"

cd /root/.openclaw/workspace/trading
OPENCLAW_FAILURE_MESSAGE="$MSG" python3 - <<'PY' || true
import os
from trading_core_new.longterm.notifier import build_diagnostic_card, push_feishu_rich
message = os.environ.get("OPENCLAW_FAILURE_MESSAGE", "")
push_feishu_rich(message, card=build_diagnostic_card("OpenClaw 服务失败", message), diagnostic=True)
PY

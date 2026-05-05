#!/usr/bin/env bash
set -euo pipefail

UNIT_NAME="${1:-unknown.service}"
HOST="$(hostname)"
NOW="$(date '+%F %T')"
MSG="【systemd失败】unit=${UNIT_NAME} host=${HOST} time=${NOW}"

cd /root/.openclaw/workspace/trading
python3 - <<PY || true
from trading_core_new.longterm.notifier import push_feishu_text
push_feishu_text(${MSG@Q})
PY

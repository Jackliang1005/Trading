#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

if [[ -f "$ROOT_DIR/.env.longterm" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env.longterm"
  set +a
fi

notify_failure() {
  local exit_code="$1"
  local line_no="$2"
  local run_date="${DATE_ARG:-$(date +%F)}"
  local msg="【长线任务失败】evening-decision ${run_date} exit=${exit_code} line=${line_no} host=$(hostname)"
  python3 - <<PY || true
from trading_core_new.longterm.notifier import push_feishu_text
push_feishu_text(${msg@Q})
PY
}

on_err() {
  local exit_code="$?"
  local line_no="$1"
  notify_failure "$exit_code" "$line_no"
  exit "$exit_code"
}

DATE_ARG="${1:-}"
TOP_K="${TOP_K:-15}"
SKIP_SYNC_UNIVERSE="${SKIP_SYNC_UNIVERSE:-0}"
NO_PUSH="${NO_PUSH:-0}"
NO_LLM_SCAN="${NO_LLM_SCAN:-0}"
IGNORE_TRADING_CALENDAR="${IGNORE_TRADING_CALENDAR:-0}"
AUTO_CLEANUP="${LONGTERM_AUTO_CLEANUP:-1}"
trap 'on_err $LINENO' ERR

EXTRA=()
if [[ -n "$DATE_ARG" ]]; then
  EXTRA+=(--date "$DATE_ARG")
fi
CHECK_DATE="${DATE_ARG:-$(date +%F)}"
if [[ "$IGNORE_TRADING_CALENDAR" != "1" ]]; then
  if ! python3 -m trading_core_new.longterm.cli check-trading-day --date "$CHECK_DATE" >/dev/null 2>&1; then
    # Today is non-trading. Check if tomorrow is a trading day (holiday eve).
    TOMORROW=$(date -d "+1 day" +%F 2>/dev/null || date -v+1d +%F 2>/dev/null || echo "")
    if [[ -n "$TOMORROW" ]] && python3 -m trading_core_new.longterm.cli check-trading-day --date "$TOMORROW" >/dev/null 2>&1; then
      echo "today ($CHECK_DATE) is non-trading, but tomorrow ($TOMORROW) is — running with last trading day data"
      EXTRA+=(--ignore-trading-calendar)
    else
      echo "skip evening decision: non-trading day ($CHECK_DATE)"
      exit 0
    fi
  fi
else
  EXTRA+=(--ignore-trading-calendar)
fi
if [[ "$SKIP_SYNC_UNIVERSE" == "1" ]]; then
  EXTRA+=(--skip-sync-universe)
fi
if [[ "$NO_PUSH" == "1" ]]; then
  EXTRA+=(--no-push)
fi
if [[ "$NO_LLM_SCAN" == "1" ]]; then
  EXTRA+=(--no-llm-scan)
fi

python3 -m trading_core_new.longterm.cli evening-decision --top-k "$TOP_K" "${EXTRA[@]}"

if [[ "$AUTO_CLEANUP" == "1" ]]; then
  python3 -m trading_core_new.longterm.cli cleanup-data
fi

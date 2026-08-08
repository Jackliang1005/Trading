#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

# A caller's explicit safety/test overrides must win over dotenv defaults.
CALLER_NO_PUSH_SET="${NO_PUSH+x}"
CALLER_NO_PUSH="${NO_PUSH-}"
CALLER_NO_LLM_SET="${NO_LLM_SCAN+x}"
CALLER_NO_LLM="${NO_LLM_SCAN-}"
CALLER_CLEANUP_SET="${LONGTERM_AUTO_CLEANUP+x}"
CALLER_CLEANUP="${LONGTERM_AUTO_CLEANUP-}"
CALLER_IGNORE_CALENDAR_SET="${IGNORE_TRADING_CALENDAR+x}"
CALLER_IGNORE_CALENDAR="${IGNORE_TRADING_CALENDAR-}"
CALLER_SKIP_SYNC_SET="${SKIP_SYNC_UNIVERSE+x}"
CALLER_SKIP_SYNC="${SKIP_SYNC_UNIVERSE-}"
CALLER_DATA_DIR_SET="${LONGTERM_DATA_DIR+x}"
CALLER_DATA_DIR="${LONGTERM_DATA_DIR-}"

if [[ -f "$ROOT_DIR/.env.longterm" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env.longterm"
  set +a
fi

if [[ "$CALLER_NO_PUSH_SET" == "x" ]]; then export NO_PUSH="$CALLER_NO_PUSH"; fi
if [[ "$CALLER_NO_LLM_SET" == "x" ]]; then export NO_LLM_SCAN="$CALLER_NO_LLM"; fi
if [[ "$CALLER_CLEANUP_SET" == "x" ]]; then export LONGTERM_AUTO_CLEANUP="$CALLER_CLEANUP"; fi
if [[ "$CALLER_IGNORE_CALENDAR_SET" == "x" ]]; then export IGNORE_TRADING_CALENDAR="$CALLER_IGNORE_CALENDAR"; fi
if [[ "$CALLER_SKIP_SYNC_SET" == "x" ]]; then export SKIP_SYNC_UNIVERSE="$CALLER_SKIP_SYNC"; fi
if [[ "$CALLER_DATA_DIR_SET" == "x" ]]; then export LONGTERM_DATA_DIR="$CALLER_DATA_DIR"; fi

export DISABLE_FILE_LOGGING="${DISABLE_FILE_LOGGING:-1}"

notify_failure() {
  local exit_code="$1"
  local line_no="$2"
  local run_date="${DATE_ARG:-$(date +%F)}"
  local msg="日期：${run_date}
任务：长线收盘复盘
退出码：${exit_code}
故障行：${line_no}
主机：$(hostname)"
  OPENCLAW_FAILURE_MESSAGE="$msg" python3 - <<'PY' || true
import os
from trading_core_new.longterm.notifier import build_diagnostic_card, push_feishu_rich
message = os.environ.get("OPENCLAW_FAILURE_MESSAGE", "")
push_feishu_rich(message, card=build_diagnostic_card("长线收盘复盘失败", message), diagnostic=True)
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

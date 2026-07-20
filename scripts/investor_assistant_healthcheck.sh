#!/usr/bin/env bash
set -uo pipefail

OUT_DIR="/root/.openclaw/workspace/reports"
mkdir -p "$OUT_DIR"
TS="$(date +%Y%m%d_%H%M%S)"
REPORT="$OUT_DIR/investor_assistant_health_${TS}.md"
LATEST="$OUT_DIR/investor_assistant_health_latest.md"
export QMT2HTTP_TIMEOUT="${QMT2HTTP_TIMEOUT:-8}"
INVEST_UNITS=(
  feishu-webhook.service
  investor-event-watch.service
  trading-intraday.service
  investor-collect.timer
  investor-morning-brief.timer
  investor-predict.timer
  trading-morning.timer
  investor-decision-0935.timer
  investor-briefing-0945.timer
  investor-decision-1030.timer
  investor-briefing-1320.timer
  investor-briefing-1420.timer
  trading-evening.timer
  investor-closing-brief.timer
  investor-daily-maintain.timer
  investor-reflect.timer
  investor-weekly-report.timer
  qmttrader-v2-concepts.timer
  investor-health-alert.timer
  investor-risk-report.timer
)

section() { printf '\n## %s\n\n' "$1" >> "$REPORT"; }
run_block() {
  local title="$1"; shift
  section "$title"
  {
    echo '```text'
    timeout 45 "$@" 2>&1 || echo "command_exit=$?"
    echo '```'
  } >> "$REPORT"
}

cat > "$REPORT" <<HEAD
# OpenClaw 投资助理健康巡检

- generated_at: $(date '+%F %T %Z')
- host: $(hostname)

HEAD

section "Investment Service Status"
{
  echo '| unit | enabled | active | next/summary |'
  echo '| --- | --- | --- | --- |'
  for unit in "${INVEST_UNITS[@]}"; do
    enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
    active="$(systemctl is-active "$unit" 2>/dev/null || true)"
    next=""
    if [[ "$unit" == *.timer ]]; then
      next="$(systemctl list-timers --all --no-pager "$unit" 2>/dev/null | awk 'NR==2 {print $1" "$2" "$3" "$4}' || true)"
    else
      next="$(systemctl show "$unit" -p MainPID --value 2>/dev/null | sed 's/^/pid=/')"
    fi
    echo "| $unit | $enabled | $active | ${next:-n/a} |"
  done
} >> "$REPORT"

section "Duplicate Process Check"
{
  echo '```text'
  systemd_pid="$(systemctl show -p MainPID --value trading-intraday.service 2>/dev/null || echo 0)"
  pids="$(pgrep -f 'python3 -m trading_core_new.longterm.cli intraday-monitor --interval 300' || true)"
  count="$(printf '%s\n' "$pids" | sed '/^$/d' | wc -l)"
  echo "trading_intraday_systemd_pid=$systemd_pid"
  echo "trading_intraday_process_count=$count"
  if [[ "$count" != "1" ]]; then
    echo "WARN duplicate_or_missing_intraday_monitor"
  fi
  pgrep -af 'python3 -m trading_core_new.longterm.cli intraday-monitor --interval 300' || true
  echo '```'
} >> "$REPORT"

run_block "Investment Timers" bash -lc "systemctl list-timers --all --no-pager | grep -Ei 'investor|trading|qmttrader-v2' || true"
run_block "Scheduled Push Wrappers" bash -lc "for u in investor-collect.service investor-predict.service investor-decision-0935.service investor-briefing-0945.service investor-decision-1030.service investor-briefing-1320.service investor-briefing-1420.service investor-reflect.service; do echo --- \$u; systemctl show \$u -p ExecStart -p EnvironmentFiles --no-pager; done"
run_block "Relevant Processes" bash -lc "ps -eo pid,ppid,lstart,cmd | grep -Ei 'openclaw.*gateway|investor/main.py event-watch|feishu_webhook|trading_core_new.longterm.cli intraday-monitor' | grep -v grep || true"
run_block "Listening Investment Ports" bash -lc "ss -lntp | grep -E '(:8788|:18789|:22)' || true"
run_block "Investor Runtime Check" bash -lc "cd /root/.openclaw/workspace/investor && python3 main.py runtime-check"
run_block "Investor Today Summary" bash -lc "cd /root/.openclaw/workspace/investor && python3 main.py today-summary --text"
run_block "Assistant Menu" bash -lc "cd /root/.openclaw/workspace/investor && python3 main.py assistant-menu"
run_block "Capability Audit" bash -lc "cd /root/.openclaw/workspace && python3 scripts/investor_assistant_audit.py --json"
run_block "Health Alert Dry Run" bash -lc "cd /root/.openclaw/workspace && python3 scripts/investor_health_alert.py --dry-run --timeout 3"
run_block "Qmt2http Remote Recovery Probe" bash -lc "cd /root/.openclaw/workspace && python3 scripts/qmt2http_remote_recovery.py --server guojin --timeout 6"
run_block "Investor Latest Events" bash -lc "cd /root/.openclaw/workspace/investor && python3 main.py event-today | sed -n '1,120p'"
run_block "Trading Longterm Summary" bash -lc "cd /root/.openclaw/workspace/trading && python3 -m trading_core_new.longterm.cli summary"
run_block "Crontab" bash -lc "crontab -l 2>/dev/null || true"
run_block "Recent Investor Logs" bash -lc "find /root/.openclaw/workspace/investor/logs -maxdepth 1 -type f -printf '%TY-%Tm-%Td %TH:%TM %s %p\n' 2>/dev/null | sort -r | sed -n '1,40p'"

cp "$REPORT" "$LATEST"
echo "$REPORT"

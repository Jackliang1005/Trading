#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-system}" # system | user
SRC_DIR="/root/.openclaw/workspace/trading/deploy/systemd"
UNITS=(
  "trading-evening.service"
  "trading-evening.timer"
  "trading-morning.service"
  "trading-morning.timer"
  "trading-investor-health.service"
  "trading-investor-health.timer"
  "trading-unit-failure@.service"
)

chmod +x "${SRC_DIR}/unit_failure_notify.sh"

if [[ "${MODE}" == "user" ]]; then
  DEST_DIR="${HOME}/.config/systemd/user"
  mkdir -p "${DEST_DIR}"
  for u in "${UNITS[@]}"; do
    cp -f "${SRC_DIR}/${u}" "${DEST_DIR}/${u}"
  done
  systemctl --user daemon-reload
  systemctl --user enable --now \
    trading-evening.timer \
    trading-morning.timer \
    trading-investor-health.timer
  systemctl --user list-timers --all | grep -E "trading-(evening|morning|investor-health)" || true
else
  DEST_DIR="/etc/systemd/system"
  for u in "${UNITS[@]}"; do
    cp -f "${SRC_DIR}/${u}" "${DEST_DIR}/${u}"
  done
  systemctl daemon-reload
  systemctl enable --now \
    trading-evening.timer \
    trading-morning.timer \
    trading-investor-health.timer
  systemctl list-timers --all | grep -E "trading-(evening|morning|investor-health)" || true
fi

echo "systemd guardians installed in mode=${MODE}"

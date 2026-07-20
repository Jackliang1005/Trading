#!/usr/bin/env bash
set -euo pipefail

weekday=$(date +%u)
clock=$(date +%H%M)
if [ "$weekday" -gt 5 ] || { [ "$clock" -lt 0930 ] || { [ "$clock" -gt 1130 ] && [ "$clock" -lt 1300 ]; } || [ "$clock" -gt 1500 ]; }; then
  exit 0
fi

cd /root/.openclaw/workspace/investor
/usr/bin/python3 main.py t-monitor dongguan --json
/usr/bin/python3 main.py t-monitor guojin --json

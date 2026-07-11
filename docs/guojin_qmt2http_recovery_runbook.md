# Guojin qmt2http Production Recovery Runbook

Generated: 2026-07-11

## Current Finding

OpenClaw can reach the Guojin qmt2http management surface, but trade data calls are unhealthy:

- `/api/service/qmt-client`: reachable
- `/api/service/strategy`: reachable
- `/api/qmttrader_v2/status`: reachable; production qmttrader_v2 reports running
- `/api/qmttrader_v2/logs`: reachable; current-day production logs are visible
- `/health`: intermittent timeout / status down
- `/api/stock/positions`: timeout or remote close

This means the current blocker is the Guojin trade backend / miniQMT connection, not the OpenClaw Linux workspace and not qmttrader_v2 log discovery.

## Safe Read-Only Probe

```bash
cd /root/.openclaw/workspace
python3 scripts/qmt2http_remote_recovery.py --server guojin --timeout 6
```

## Explicit Recovery Actions

These actions call qmt2http remote management APIs. They do not place trades, but they can start/reconnect the QMT client or touch strategy process management on the Windows production host. Use only when an operator is ready to observe the Windows desktop.

```bash
# reconnect qmt client without password
python3 scripts/qmt2http_remote_recovery.py --server guojin --action reconnect-client --force --timeout 8

# reconnect and provide one-time login password if miniQMT prompts for login
python3 scripts/qmt2http_remote_recovery.py --server guojin --action reconnect-client --force --login-password '<password>' --timeout 15

# ensure strategy service
python3 scripts/qmt2http_remote_recovery.py --server guojin --action ensure-strategy --force --timeout 15

# restart strategy service only if qmttrader_v2 status/logs are stale or strategy process is wrong
python3 scripts/qmt2http_remote_recovery.py --server guojin --action restart-strategy --force --timeout 30
```

## 2026-07-11 Attempt

A non-login `reconnect-client --force` call was attempted. It returned `Remote end closed connection without response` and did not restore `/api/stock/positions`. After waiting, management APIs recovered, but `/health` and `/api/stock/positions` still timed out.

Conclusion: inspect the Guojin Windows production host directly:

1. Check miniQMT client is open and logged in.
2. Check qmt2http console/service is alive.
3. Restart qmt2http if it is wedged.
4. If miniQMT shows disconnected, reconnect/login manually.
5. Re-run the read-only probe above and `python3 scripts/investor_health_alert.py --dry-run --timeout 4`.

## Fast Probe Behavior

`python3 scripts/qmt2http_remote_recovery.py --server guojin --timeout 4` probes health, positions, qmt-client, strategy, and qmttrader_v2 status concurrently. A blocked result for health/positions means the Windows miniQMT/qmt2http realtime trade endpoint is still degraded; cached news, morning brief, closing brief, and event watchlist should continue running.
## Feishu Strategy Control

Use these commands after confirming the account and trading intent:

- `/策略状态 国金`: query qmt2http strategy service status.
- `/策略停止 国金 确认`: POST stop to `/api/service/strategy`.
- `/策略启动 国金 确认`: POST start to `/api/service/strategy`.
- `/策略重启 国金 确认`: POST restart to `/api/service/strategy`.

Mutating commands without `确认` only return the current status and a confirmation prompt.

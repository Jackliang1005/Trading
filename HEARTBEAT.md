# HEARTBEAT.md — 小龙虾运行时监控检查清单

当收到 heartbeat 轮询时，按以下优先级检查（无需全部执行，每次选 1-2 项轮转）：

## P0: 核心通道健康

### 1. qmt2http 双账户可达性
```bash
cd /root/.openclaw/workspace/investor && python3 main.py runtime-check
```
- 国金 (39.105.48.176:8085) health + 交易读口
- 东莞 (150.158.31.115:8085) health + 交易读口
- 任一不可达 → 飞书告警

### 2. 飞书 webhook 服务状态
```bash
systemctl is-active feishu-webhook && systemctl is-enabled feishu-webhook
```
- 应为 active + enabled
- 异常 → 尝试 restart 并告警

## P1: 数据完整性

### 3. 最新长线快照时效
```bash
cd /root/.openclaw/workspace/investor && python3 main.py longterm-summary 2>/dev/null | head -5
```
- 快照日期应为当日或前一交易日
- 超过 2 个交易日未更新 → 告警

### 4. investor 数据库 packet 覆盖率
```bash
cd /root/.openclaw/workspace/investor && python3 main.py dashboard 2>/dev/null | head -20
```
- 检查 research_packets / portfolio_snapshots 计数
- 最新 packet 日期是否为最近交易日

## P2: 定时任务确认

### 5. 最近 cron 执行状态
- 检查今日各时段简报是否正常推送（09:45/13:20/14:20）
- 检查 collect (07:30) / predict (09:30) / reflect (20:30) 是否执行

## 响应约定

- **全部正常** → 回复 `HEARTBEAT_OK`
- **P0 异常** → 仅在首次确认、等级升级或关键影响扩大时告警到飞书；持续中的同一事故不重复发送
- **P1 异常** → 记录到 memory/ + 下次主会话报告
- **非交易时段 (15:30-07:00)** → 仅检查 webhook 服务状态

### 静默与去噪硬规则

- 健康、恢复后保持健康、周末休整、午盘休市均只回复 `HEARTBEAT_OK`，不得发送“服务器正常”“周末休整”“Light check”等飞书消息。
- 不发送距离下一次 collect、predict、evolve 或开盘还有多少小时的倒计时；精确定时任务由 systemd timer 负责。
- 同一事故已经由 `investor-health-alert.timer` 报告后，heartbeat 不再另发摘要；等待事故升级、连续两次健康确认后的恢复，或人工查询。
- 不在 heartbeat 中手动触发 collect、predict、reflect、evolve，也不创建一次性 cron 来替代现有 systemd timer。
- 周末和夜间除新出现的 P0 故障外保持静默；“没有新情况”不是需要推送的情况。

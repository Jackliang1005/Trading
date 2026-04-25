# Packet Backfill Runbook

## 目标

将历史 `market_snapshots` 回填到新结构：

- `research_packets`
- `portfolio_snapshots`

保证：

- 可重复执行（幂等）
- 支持小批量试跑
- 支持按 `snapshot_type` 分批

## 命令入口

统一使用新 CLI：

```bash
python3 main.py backfill-packets [--type daily_close|intraday] [--limit N] [--apply] [--force]
```

说明：

- 默认是 dry-run（不写库）
- `--apply` 才会写库
- `--force` 会跳过“已回填”保护，谨慎使用

## 标准执行顺序

1. 记录基线（dry-run 小样本）

```bash
python3 main.py backfill-packets --type daily_close --limit 5
```

重点看返回字段：

- `coverage_before`
- `coverage_after`
- `skipped_already_backfilled`
- `failed`

2. 小批量真实写入（先 3~10 条）

```bash
python3 main.py backfill-packets --type daily_close --limit 3 --apply
```

期望：

- `failed == 0`
- `success > 0`
- `coverage_after.research_packets_total` 与 `coverage_after.portfolio_snapshots_total` 增长

3. 立刻幂等复跑（dry-run）

```bash
python3 main.py backfill-packets --type daily_close --limit 3
```

期望：

- `skipped_already_backfilled` 接近 `processed`
- `coverage_before` 与 `coverage_after` 保持不变

4. 放大批次执行

```bash
python3 main.py backfill-packets --type daily_close --limit 200 --apply
python3 main.py backfill-packets --type intraday --limit 200 --apply
```

直到增量趋近 0（或全部跳过）。

## 验收清单

- 命令返回 `failed == 0`
- `research_packets_total`、`portfolio_snapshots_total` 随 apply 增长
- 幂等复跑不再增长（仅 skip）
- `monitor-trading` / `today-summary` / `investor_agent context` 正常读取到 `analysis_context_summary`

快速功能回归：

```bash
python3 main.py monitor-trading 20260320
python3 main.py today-summary 20260320 --text
python3 investor_agent.py context "今天候选和买入情况"
```

## 异常处理

- 若 `failed > 0`：
  - 先用 `--limit` 缩小范围
  - 观察失败行对应的 `snapshot#id` 与 `as_of`
  - 优先修复该批数据格式，再继续 apply
- 避免在问题未定位前使用 `--force`

## 当前状态（2026-04-25）

- 回填脚本已上线：`workflows/backfill_packets.py`
- 主 CLI 已接入：`main.py backfill-packets`
- 已完成小批量真实写入验证（`daily_close`）

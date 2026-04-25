# Current Baseline

更新时间：2026-04-25

## 一键验收（推荐）

```bash
python3 main.py smoke-check --strict
```

该命令已内置主线 smoke 步骤，适合作为每次改造后的第一验收口。

## 必过命令（主线）

```bash
python3 -m py_compile main.py investor_agent.py app/cli.py
python3 main.py monitor-trading 20260320
python3 main.py today-summary 20260320 --text
python3 main.py today-account 20260320
python3 investor_agent.py context "今天候选和买入情况"
python3 main.py evolve
python3 reflection.py weekly
python3 reflection.py monthly
python3 main.py packet-maintain --dry-run --limit 20
python3 main.py handoff-sync
python3 main.py daily-maintain --dry-run --limit 20
python3 main.py runtime-check
python3 main.py feishu-query "国金今天持仓"
```

说明：

- `packet-maintain` 默认会写快照到 `docs/packet_maintenance_latest.json`
- 如只想输出终端结果可加 `--no-write`

## 数据迁移（Phase 5）

```bash
python3 main.py backfill-packets --type daily_close --limit 5
python3 main.py backfill-packets --type daily_close --limit 5 --apply
python3 main.py backfill-packets --type daily_close --limit 5
```

验收重点：

- 第二步 `coverage_after` 比 `coverage_before` 有增长
- 第三步以 `skipped_already_backfilled` 为主（幂等）
- `failed == 0`

## 关键契约

- `analysis_context_summary` 在以下入口字段一致：
  - `main.analyze`
  - `monitor-trading`
  - `today-summary`
  - `today-account`
  - `investor_agent context`

- 双账户交易归属在以下入口保持一致输出：
  - `trade_reconciliation.final_account_candidates`
  - `trade_reconciliation.submitted_account_candidates`
  - `trade_reconciliation.filled_account_candidates`
  - `trade_reconciliation.account_trade_matrix`

固定默认字段：

- `as_of_date`
- `packet_hits`
- `packet_types`
- `has_portfolio_snapshot`
- `quote_count`
- `has_flow`
- `has_market_regime`
- `positions_count`
- `today_trade_count`
- `today_order_count`
- `total_unrealized_pnl`

- `trade_decision_summary` 在 `main.analyze` 与 `investor_agent context` 统一字段：
  - `log_date`
  - `strategy`
  - `signal_count`
  - `final_candidate_count`
  - `submitted_buy_count`
  - `filled_buy_count`
  - `watchlist_count`

## 双账户运行约定（2026-04-25）

- `qmt_client.QMTManager` 默认双账户模式：
  - MAIN: `http://39.105.48.176:8085`
  - TRADE: `http://150.158.31.115:8085`
- `trade_only` 服务器允许交易读口：
  - `/api/stock/asset`
  - `/api/stock/positions`
  - `/api/stock/orders`
  - `/api/stock/trades`
  - `/api/trade/records`
- 如需临时强制单账户模式，设置：
  - `QMT2HTTP_DISABLE_TRADE=1`

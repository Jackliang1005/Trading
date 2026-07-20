# 🦞 小龙虾 (XiaoLongXia) — Investor Handoff 手册

> 下次启动前先读此文件，快速恢复上下文。

## 系统定位

自学习 A股投资分析系统，统一名称 **小龙虾 (XiaoLongXia)**，核心循环：**Collect → Predict → Reflect → Evolve**。
跨两个子系统运作：
- **investor/** — 研究/预测/反思/进化/实盘监控/飞书交互
- **trading/** — 长线组合模拟（快照供 investor 消费）

现已集成 **双服务器 QMT 网关**（国金全功能 + 东莞交易专用），支持实盘交易数据采集与展示。

## 当前运行策略（稳定观察模式）

当前阶段以”先稳定运行、后增量开发”为主，不继续扩大主线功能面。
优先关注三项运行指标：

- 飞书查询成功率（含 `/持仓`、`/监控`、`东莞策略日志`、`国金ETF 13:20/14:20`）
- qmt2http 可用率（国金/东莞 health 与交易读口）
- 日志解析命中率（策略状态、ETF打分、买卖动作）

触发原则：

- 上游网络波动、柜台临时不可达 -> 告警优先，不触发自动改代码
- 连续多交易日稳定复现的解析/路由问题 -> 再进入开发修复

---

## 模块地图

```
main.py              CLI 入口，统合调度 (legacy + new-style 命令)
app/cli.py           New-style 命令实现层（监控/交易视图/飞书/修复任务）
qmt_client.py        统一双服务器 QMT 客户端（QMTClient + QMTManager）
data_collector.py    数据采集（双服务器QMT/OpenClaw A股插件/AKShare/东财/RSS/全球指数/大宗商品/宏观新闻）
db.py                SQLite 数据层（prediction_log/strategy/rules/few_shot/snapshots/kb）
cron_setup.py        定时任务配置生成

domain/
├── entities/        领域实体定义
├── services/        服务层（canonical index: __init__.py）
│   ├── assistant_service.py         分析/看板/预测记录
│   ├── prediction_service.py        预测快照加载/落库
│   ├── prediction_prompt_service.py 预测提示词构建
│   ├── prediction_orchestrator.py   预测编排（LLM调用/解析）
│   ├── reflection_service.py        周度归因/月度审计编排
│   ├── reflection_runtime_service.py 回测/每日反思/交易摘要
│   ├── reflection_analysis_service.py 失败模式分析/周报格式化
│   ├── evolution_service.py         策略进化（权重/规则/few-shot/prompt）
│   ├── legacy_entry_service.py      定时任务编排入口
│   ├── live_monitor_service.py      实盘监控编排
│   ├── live_monitor_view_service.py 交易视图（today-*）
│   ├── longterm_portfolio_service.py 长线组合快照聚合
│   ├── feishu_query_service.py      飞书查询处理
│   ├── feishu_bridge_service.py     飞书消息桥接
│   └── analysis_context_service.py  分析上下文归一化
└── policies/        策略规则引擎
    ├── scoring_policy.py
    └── confidence_policy.py

workflows/           工作流编排脚本
├── backfill_packets.py
├── packet_maintenance.py
├── daily_maintenance.py
├── runtime_check.py
├── run_smoke_checks.py
├── scheduled_briefings.py
└── sync_handoff_snapshot.py

live_monitor/        实盘监控子系统
├── collectors/      采集器（health/trade_state/runtime/observability/logs）
├── analyzers/       分析器（heartbeat/error/phase/risk/root_cause）
└── remediation/     修复任务（codex_fix_runner/escalation）

legacy compatibility package removed; legacy command orchestration lives in domain/services/legacy_entry_service.py
```

## CLI 命令

```
python3 main.py <command> [args...]

── 闭环命令 ──
init          初始化 DB + 知识库
collect       采集每日数据（07:30）— 含双服务器QMT账户/持仓/委托/成交/交易摘要
predict       生成预测（09:30）— 含实盘交易上下文
reflect       回测反思（20:30）— 含实盘交易摘要 + 反思报告
evolve        周进化（周日 21:00）
audit         月度审计（每月1日 22:00）
dashboard     状态看板
prompt        查看当前 system prompt
backtest      手动回测
sector-scan   板块扫描 + 美股映射 + 持仓诊断
sync-logs     同步生产机器日志到本地（15:10）

── 监控与交易视图 ──
monitor               实盘监控（qmt2http + qmttrader）
monitor-trading       交易监控视图（候选/买入/持仓读口）
runtime-check         运行诊断（qmt2http 健康/交易读口/日志）
today-candidates      查看最新候选与最终选股
today-buys            查看最新买入提交与成交摘要
today-account         查看双账户读口与对账状态
today-summary         查看候选/买入/账户/告警简报（支持 --text）
longterm-summary      查看长线组合模拟盘聚合摘要

── 飞书与数据维护 ──
record                手动录入预测
feishu-query          Feishu plugin 查询入口（如：国金今天持仓）
feishu-bridge         Feishu plugin 事件桥接入口（--query 或 stdin JSON）
scheduled-briefing    定时交易简报（0945东莞策略 / 1320、1420国金ETF）
packet-maintain       日常 packet 增量维护（daily_close + intraday）
handoff-sync          将 packet 维护快照同步写入 HANDOFF
daily-maintain        日常维护总入口（packet-maintain + handoff-sync）
backfill-packets      将 market_snapshots 回填为 research/portfolio packets
smoke-check           执行主线 smoke 验收命令集合

── 修复任务管理 ──
fix-task-summary      查看修复任务状态摘要
fix-tasks             查看 Codex 修复任务（open/acknowledged/patched/closed/all）
fix-task-show         查看单个修复任务详情
fix-task-context      查看修复任务精简排障上下文
fix-task-pack         查看修复任务自动修复 payload
fix-task-bundle       查看修复任务可投喂 agent 的最小输入
fix-task-export       导出修复任务 bundle 到 JSON 文件
fix-task-run-validation  执行修复任务验证计划
fix-task-validations  查看修复任务验证记录
fix-task-validation-groups  查看修复任务验证批次摘要
fix-task-promote      按最新验证结果推进 patched/closed
fix-task-ack          认领修复任务
fix-task-note         给修复任务追加备注
fix-task-patched      标记修复任务为已打补丁
fix-task-close        关闭修复任务
fix-task-reopen       重新打开修复任务
```

`packet-maintain` 默认会生成：

- `docs/packet_maintenance_latest.json`（最近一次运行快照）

---

## 数据流

```
07:30  collect ─→ market_snapshots(daily_close) ─→ auto_memorize → kb
                   └→ qmt_account / qmt_positions / qmt_orders / qmt_trades / qmt_trading_summary
09:30  predict ─→ 读 snapshot + 实盘交易上下文(持仓盈亏+成交+委托) + RAG + few-shot + system_prompt → LLM → 3日K线+买卖点预测 → prediction_log
09:45  scheduled-briefing ─→ 东莞 NH/MIX 策略日志巡检
13:20  scheduled-briefing ─→ 国金 ETF 午盘简报
14:20  scheduled-briefing ─→ 国金 ETF 尾盘简报
15:10  sync-logs ─→ 同步国金/东莞生产日志到本地
随时   sector-scan ─→ 同花顺热门板块 + 美股板块ETF + 龙头个股 + 双服务器持仓 → 轮动预判 → market_snapshots(sector_scan)
20:30  reflect ─→ 回测 prediction_log + 实盘交易摘要(双服务器汇总) → 打分 → reflection_reports/
周日   evolve  ─→ 调权重 + 提规则 + 管 few-shot → strategy_config.json + system_prompt.md
```

---

## 🆕 双服务器 QMT 架构（qmt_client.py）

### 服务器配置

| 服务器 | 地址 | 用途 |
|--------|------|------|
| 主服务器 (MAIN) | `http://39.105.48.176:8085` | 行情数据 + 交易 |
| 交易专用 (TRADE) | `http://150.158.31.115:8085` | 交易专用 (trade_only) |

### QMTClient（单服务器）

封装单个 QMT2HTTP 服务器的 HTTP 调用：

| 方法 | 端点 | 说明 |
|------|------|------|
| `get_account_asset()` | GET `/api/stock/asset` | 账户资产 |
| `get_positions()` | GET `/api/stock/positions` | 持仓列表 |
| `get_orders()` | GET `/api/stock/orders` | 今日委托 |
| `get_trades()` | GET `/api/stock/trades` | 今日成交 |
| `get_realtime_data(code)` | RPC `get_realtime_data` | 单股实时行情 |
| `get_batch_realtime_data(codes)` | RPC `get_batch_realtime_data` | 批量实时行情 |
| `get_trade_records(record_type)` | GET `/api/trade/records` | 交易记录 |
| `get_stock_sectors(code)` | RPC `get_stock_sectors` | 个股板块 |
| `health()` | GET `/health` | 健康检查 |

### QMTManager（双服务器）

管理两个 QMTClient 实例，提供统一访问：

| 方法 | 说明 |
|------|------|
| `get_all_positions()` | 合并两个服务器的持仓，按 stock_code 去重 |
| `get_all_accounts()` | 返回 `{"main": {...}, "trade": {...}}` |
| `get_all_orders()` | 合并两个服务器的今日委托 |
| `get_all_trades()` | 合并两个服务器的今日成交 |
| `get_market_data(codes)` | 仅从主服务器获取行情 |
| `get_trading_summary()` | 综合摘要：账户+持仓+委托+成交+P&L |
| `health()` | 两个服务器的健康检查 |

### 环境变量

```bash
QMT2HTTP_MAIN_URL=http://39.105.48.176:8085     # 主服务器（行情+交易）
QMT2HTTP_TRADE_URL=http://150.158.31.115:8085    # 交易专用服务器
QMT2HTTP_DONGGUAN_BASE_URL=http://150.158.31.115:8085  # 交易服务器别名（可选）
QMT2HTTP_DISABLE_TRADE=0                         # 设为1可强制单服务器模式
QMT2HTTP_API_TOKEN=998811                        # API Token
QMT2HTTP_BASE_URL=http://39.105.48.176:8085      # 旧版单服务器回退
```

**回退逻辑**：默认使用双服务器（内置 MAIN+TRADE 默认地址）；若需强制单服务器，设置 `QMT2HTTP_DISABLE_TRADE=1`。

### 单例模式

```python
from qmt_client import get_qmt_manager, reset_qmt_manager

qm = get_qmt_manager()    # 全局单例
summary = qm.get_trading_summary()
```

---

## 数据源清单

| 来源 | 用途 | 接口 |
|------|------|------|
| QMT2HTTP 主服务器 | 行情 + 账户/持仓/交易网关 | HTTP RPC/GET |
| QMT2HTTP 交易服务器 | 交易专用网关（trade_only） | HTTP RPC/GET |
| openclaw-data-china-stock | A股/指数实时行情与分钟级市场数据 | OpenClaw 插件 |
| AKShare | A股日线/全球指数/大宗商品/宏观新闻/美股日线 | Python |
| 东方财富 | 行情/资金流向/板块资金/新闻 | skills 复用 |
| 同花顺 | 涨停板块 + 龙头股 | HTTP API |
| RSS | 财联社/新浪/东财新闻 | feedparser |
| DeepSeek / OpenRouter | LLM 预测 | HTTP API |

---

## Feishu 接口策略

- 已移除 `investor` 内旧的 Feishu 直连实现（OpenAPI/Webhook）。
- 统一改为 plugin 调用：
  - `python3 main.py feishu-query "<query>"`
- 查询结果由 `qmt2http` 实时返回，适合飞书机器人问答场景（持仓/委托/成交/健康/日志）。

## 核心表结构（db.py）

| 表 | 用途 | 关键字段 |
|----|------|---------|
| prediction_log | 预测记录+回测结果 | target, trend_3d, direction, confidence, kline_day1/2/3, buy_point, sell_point, stop_loss, score |
| strategy | 4策略权重+胜率 | name, weight, win_rate, avg_score |
| rules | 投资规则库 | rule_text, category, confidence, enabled |
| few_shot_examples | 好/坏分析案例 | category(good/bad), scenario, score |
| market_snapshots | 原始数据快照 | snapshot_type(daily_close/sector_scan), data(JSON) |
| kb_documents + kb_fts | 知识库+全文检索 | doc_type, title, content |
| reflection_reports | 反思报告 | report_type(daily/weekly/monthly) |

## 预测打分逻辑（0-100）— 3日K线+买卖点

- 3日趋势正确 +35（bullish且涨 / bearish且跌 / ranging且波动<1.5%）
- K线精度(收盘价) +25（3天收盘价预测 vs 实际，按误差衰减）
- K线精度(区间) +15（预测的high/low覆盖实际波动区间程度）
- 买卖点有效性 +15（实际价格触及 buy/sell 附近±1%）
- 止损合理性 +10（未被触发且距离合理 2-6%）

旧格式兼容：方向正确 +50，置信度校准 +20，幅度准确度 +30

## 策略进化参数

- 4 策略：technical / fundamental / sentiment / geopolitical
- 权重范围 [0.10, 0.60]，步长 0.05，14天回看
- 胜率偏离均值 ±5% 触发调整
- 规则置信度 < 0.2 且应用 10+ 次 → 自动禁用
- Few-shot 每类保留 top 10（按 score 排序）

---

## 板块轮动模块（sector_scanner.py）

### 美股→A股映射

11 个 S&P 500 板块 ETF（`US_SECTOR_ETFS`）+ 10 只龙头个股（`US_KEY_STOCKS`），
每个映射到对应 A股板块关键词。

### 轮动预判逻辑（`predict_a_sector_rotation`）

1. 美股涨幅前3板块（仅 >0）→ 映射 A股板块，与当前热门交叉判定"持续强势"或"潜在轮动"
2. 美股涨幅 >1% 龙头 → 映射 A股板块
3. 美股跌幅前3板块（仅 <0）→ 对应 A股板块标记"可能承压"

### 持仓诊断

双服务器 QMT 持仓 × 热门板块交叉 → 🟢核心股 / 🟡关联股 / 🔴非热门
行情快照默认来自 `openclaw-data-china-stock`，QMT2HTTP 提供双服务器账户/持仓入口。

### predictor.py 集成

`build_prediction_prompt()` 从多个来源提取数据注入 LLM prompt：
- `sector_scan` snapshot: 美股板块/龙头/轮动预判
- `daily_close` snapshot: QMT双服务器汇总（账户/持仓盈亏/今日成交/待成交委托）
- 市场数据/资金流向/新闻/全球指数/大宗商品/宏观新闻/市场状态

---

## 已知限制 & 优化方向

### 当前限制
- 预测覆盖 3 大指数 + 持仓标的，输出3日K线(OHLC+形态)+买卖点(买入/卖出/止损)
- 回测需等3个交易日后执行（给足时间让K线走完）
- 美股数据来自新浪（`ak.stock_us_daily`），偶尔延迟或缺失
- 同花顺涨停板块 API 非交易时段可能返回空
- QMT 交易网关依赖外部服务器在线
- A股实时行情当前优先依赖 `openclaw-data-china-stock` 插件可用
- LLM 预测质量受 prompt 长度和模型能力限制

### 可优化方向
1. 个股级预测（持仓标的涨跌预判）
2. 技术指标集成（MACD/RSI/布林带等加入 prompt）
3. 多模型集成（多 LLM 投票）
4. 实时盘中预警（异动推送）
5. 仓位管理建议（基于预测+风控的加减仓建议）
6. 情绪分析（社交媒体/雪球/东财股吧 NLP）
7. 回测引擎（历史模拟验证策略）
8. A股板块 ETF 数据补充（对标美股板块 ETF 做更精确映射）
9. 港股通联动分析
10. 宏观经济日历集成（CPI/PMI/利率决议等事件驱动）

---

## 快速验证命令

```bash
cd /root/.openclaw/workspace/investor

# 验证双服务器 QMT 连接
python3 -c "from qmt_client import get_qmt_manager; qm = get_qmt_manager(); print('Dual mode:', qm.dual_mode)"
python3 -c "from qmt_client import get_qmt_manager; qm = get_qmt_manager(); print(qm.get_trading_summary())"

# 验证模块加载
python3 -c "import sector_scanner; print('OK')"
python3 -c "import predictor; print('OK')"
python3 -c "import reflection; print('OK')"

# 验证美股数据
python3 -c "from sector_scanner import fetch_us_sector_performance; print(fetch_us_sector_performance())"

# 完整数据采集（含双服务器QMT数据）
python3 main.py collect

# 完整板块扫描
python3 main.py sector-scan

# 每日反思（含实盘交易摘要）
python3 main.py reflect

# 状态看板
python3 main.py dashboard

# 查看当前 system prompt
python3 main.py prompt
```

---

## Packet 回填快照（2026-04-25）

执行命令（串行）：

```bash
python3 main.py backfill-packets --type daily_close --limit 500 --apply
python3 main.py backfill-packets --type intraday --limit 500 --apply
python3 main.py backfill-packets --type daily_close --limit 1
```

结果摘要：

- `daily_close`：processed=32, success=27, skipped_already_backfilled=5, failed=0
- `intraday`：processed=0, success=0, failed=0
- 覆盖统计（最终）：
  - `research_packets_total=144`
  - `portfolio_snapshots_total=41`
  - `research_packet_dates=30`
  - `portfolio_snapshot_dates=30`

---

## 交易监控主线补充（2026-04-25）

- 双账户归属增强已落地：
  - `trade_reconciliation` 新增
    - `final_account_candidates`
    - `submitted_account_candidates`
    - `filled_account_candidates`
    - `skipped_account_candidates`
    - `account_trade_matrix`
    - `coverage_summary`
    - `skipped_reason_summary`（overall + by_server）
- 交易视图入口已统一输出聚合字段：
  - `monitor-trading`
  - `today-candidates`
  - `today-account`
  - `today-buys`
  - `today-summary`
  - `today-summary --text` 新增“归属覆盖/账户分摊/过滤原因汇总/盘中时序”行
- `analyze/context` 已接交易决策摘要：
  - `main.analyze` 新增 `trade_decision_summary` 与 `trade_decision_focus`
  - `investor_agent context` 直接显示 log_date/strategy/候选/提交/成交/watchlists

---

## 🆕 飞书 Webhook 接入（2026-04-25）

### 架构

飞书事件订阅 → HTTP POST `:8788/feishu/trading` → `feishu_webhook_server.py`
                                                              ├── /持仓 /预测 /风险等 → investor query service
                                                              ├── T 开头 → TradingCommandService（兼容）
                                                              └── openclaw message send → 飞书回复

不经过 openclaw AI agent，webhook 直接处理并回复。

### 部署

```bash
systemctl status feishu-webhook   # 查看状态
systemctl restart feishu-webhook  # 重启
journalctl -u feishu-webhook -f   # 实时日志
```

- 端口: `8788`
- 路径: `/feishu/trading`
- 开机自启: `enabled`
- 日志: `/root/.openclaw/workspace/investor/logs/feishu_webhook.log`

### 飞书支持的命令

| 命令 | 功能 |
|------|------|
| `/持仓` | 双账户实时持仓 |
| `/账户` | 双账户健康状态 |
| `/成交` | 今日成交明细 |
| `/预测` | 近7天预测胜率 |
| `/风险` | 仓位集中度 |
| `/策略` | 策略权重配置 |
| `/复盘` | 最新反思报告 |

*最后更新: 2026-05-17 — Phase 7 旧代码清理与身份文档统一*

---

<!-- packet-maintenance:start -->

## Packet 日常维护快照（自动生成）

- 同步时间：2026-07-20 18:31:01
- run_at：2026-07-20T18:31:01
- dry_run：False
- force：False
- limits：daily_close=200 intraday=200
- merged：processed=97 success=0 skipped_already_backfilled=97 failed=0
- coverage_before：research_packets=404 portfolio_snapshots=106 packet_dates=85 portfolio_dates=85
- coverage_after：research_packets=404 portfolio_snapshots=106 packet_dates=85 portfolio_dates=85

<!-- packet-maintenance:end -->

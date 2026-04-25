# 🦞 OpenClaw Investor — Handoff 手册

> 下次启动优化前，先读此文件，快速恢复上下文。

## 系统定位

自学习 A股投资分析系统，核心循环：**Collect → Predict → Reflect → Evolve**。
自动采集多源数据、LLM 生成预测、回测打分、策略权重/规则/Few-shot 自动进化。
现已集成 **双服务器 QMT 网关**（主服务器 + 交易专用服务器），支持实盘交易数据采集与展示。

## 当前运行策略（稳定观察模式）

当前阶段以“先稳定运行、后增量开发”为主，不继续扩大主线功能面。  
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
main.py              CLI 入口，所有命令调度
qmt_client.py        🆕 统一双服务器 QMT 客户端（QMTClient + QMTManager）
data_collector.py    数据采集（双服务器QMT/OpenClaw A股插件/AKShare/东财/RSS/全球指数/大宗商品/宏观新闻）
predictor.py         LLM 预测生成（含实盘交易上下文：持仓盈亏/今日成交/待成交委托）
reflection.py        回测打分 + 每日/周/月反思报告 + 实盘交易摘要
evolution.py         策略权重调整 + 规则库更新 + Few-shot 管理 + system prompt 生成
knowledge_base.py    FTS5 全文检索 RAG + Few-shot 构建
sector_scanner.py    板块轮动扫描 + 美股→A股映射 + 持仓诊断（使用QMT双服务器）
db.py                SQLite 数据层（prediction_log/strategy/rules/few_shot/snapshots/kb）
investor_agent.py    OpenClaw Agent 桥接接口
cron_setup.py        定时任务配置生成
record_prediction.py 预测记录辅助模块
```

## CLI 命令

```
python3 main.py <command>

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
record        手动录入预测
feishu-query  Feishu plugin 查询入口（如：国金今天持仓）
feishu-bridge Feishu plugin 事件桥接入口（--query 或 stdin JSON）
packet-maintain 日常 packet 增量维护（daily_close + intraday）
handoff-sync  将 packet 维护快照同步写入 HANDOFF
daily-maintain 日常维护总入口（packet-maintain + handoff-sync）
runtime-check 运行诊断（qmt2http 健康/交易读口/日志）
scheduled-briefing 定时交易简报（0945东莞策略 / 1320、1420国金ETF）
```

`packet-maintain` 默认会生成：

- `docs/packet_maintenance_latest.json`（最近一次运行快照）

---

## 数据流

```
07:30  collect ─→ market_snapshots(daily_close) ─→ auto_memorize → kb
                   └→ qmt_account / qmt_positions / qmt_orders / qmt_trades / qmt_trading_summary
09:30  predict ─→ 读 snapshot + 实盘交易上下文(持仓盈亏+成交+委托) + RAG + few-shot + system_prompt → LLM → prediction_log
随时    sector-scan ─→ 同花顺热门板块 + 美股板块ETF + 龙头个股 + 双服务器持仓 → 轮动预判 → market_snapshots(sector_scan)
20:30  reflect ─→ 回测 prediction_log + 实盘交易摘要(双服务器汇总) → 打分 → reflection_reports/
周日    evolve  ─→ 调权重 + 提规则 + 管 few-shot → strategy_config.json + system_prompt.md
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
  - `python3 feishu_plugin_query.py "<query>"`
- 查询结果由 `qmt2http` 实时返回，适合飞书机器人问答场景（持仓/委托/成交/健康/日志）。

## 核心表结构（db.py）

| 表 | 用途 | 关键字段 |
|----|------|---------|
| prediction_log | 预测记录+回测结果 | target, direction, confidence, score, is_correct |
| strategy | 4策略权重+胜率 | name, weight, win_rate, avg_score |
| rules | 投资规则库 | rule_text, category, confidence, enabled |
| few_shot_examples | 好/坏分析案例 | category(good/bad), scenario, score |
| market_snapshots | 原始数据快照 | snapshot_type(daily_close/sector_scan), data(JSON) |
| kb_documents + kb_fts | 知识库+全文检索 | doc_type, title, content |
| reflection_reports | 反思报告 | report_type(daily/weekly/monthly) |

## 预测打分逻辑（0-100）

- 方向正确 +50，近似正确（实际波动<0.1%）+30
- 置信度校准 +20（高置信+正确 → 满分）
- 幅度准确度 +30（预测 vs 实际涨跌幅偏差越小越高）

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
- 预测仅覆盖 3 大指数（上证/深证/创业板），不含个股
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

*最后更新: 2026-04-25 — 飞书 Webhook 接入完成*

---

<!-- packet-maintenance:start -->

## Packet 日常维护快照（自动生成）

- 同步时间：2026-04-25 16:18:16
- run_at：2026-04-25T16:18:16
- dry_run：True
- force：False
- limits：daily_close=2 intraday=2
- merged：processed=2 success=0 skipped_already_backfilled=2 failed=0
- coverage_before：research_packets=144 portfolio_snapshots=41 packet_dates=30 portfolio_dates=30
- coverage_after：research_packets=144 portfolio_snapshots=41 packet_dates=30 portfolio_dates=30

<!-- packet-maintenance:end -->

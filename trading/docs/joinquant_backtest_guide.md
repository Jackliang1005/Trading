# 聚宽自动回测指南

> 最后更新: 2026-05-05

## 1. 概述

`jq_automation_worker.mjs` 是一个基于 Playwright 的聚宽策略自动回测工具。它能：

1. **自动注入策略代码** 到聚宽在线编辑器
2. **触发回测** 并轮询结果
3. **提取回测指标** (收益、夏普、回撤等)
4. **抓取策略日志** (`print()` 输出)
5. **留存截图** 方便回溯

## 2. 环境准备

### 2.1 依赖

```bash
# Node.js 依赖
npm install playwright

# 安装 Chromium
npx playwright install chromium
```

### 2.2 获取聚宽 Cookie

聚宽网站需要登录态。获取 Cookie 的步骤：

1. 打开 Chrome, 访问 https://www.joinquant.com 并登录
2. F12 → Application → Cookies → www.joinquant.com
3. 将所有 Cookie 拼接为字符串: `key1=val1; key2=val2; ...`
4. 保存到 `.joinquant_cookie` 文件

```bash
# 文件格式 (一行)
sessionid=xxx; csrftoken=xxx; jq_remember=xxx; ...
```

### 2.3 可选: 环境变量

```bash
export JOINQUANT_COOKIE="key1=val1; key2=val2"  # 替代 --cookie-file
export JOINQUANT_ALGORITHM_ID="12345"            # 复用已有策略ID
export JQ_START_TIME="2024-01-01 00:00:00"       # 回测起始
export JQ_END_TIME="2026-04-30 23:59:59"         # 回测结束
export JQ_CAPITAL_BASE="150000"                  # 初始资金
export JQ_TIMEOUT_SEC="600"                      # 超时(秒)
```

## 3. 使用方法

### 3.1 基本用法

```bash
node jq_automation_worker.mjs <策略文件.py> --cookie-file .joinquant_cookie
```

### 3.2 示例

```bash
# 运行热度轮动策略回测
node jq_automation_worker.mjs joinquant_heat_rotation.py --cookie-file .joinquant_cookie

# 使用命令行传入 cookie
node jq_automation_worker.mjs joinquant_heat_rotation.py \
  --cookie "sessionid=abc; csrftoken=xyz"

# 指定回测参数
JQ_START_TIME="2023-01-01 00:00:00" \
JQ_END_TIME="2024-12-31 23:59:59" \
JQ_CAPITAL_BASE="500000" \
  node jq_automation_worker.mjs joinquant_heat_rotation.py --cookie-file .joinquant_cookie

# 使用已有的策略 ID (不创建新策略)
JOINQUANT_ALGORITHM_ID="12345" \
  node jq_automation_worker.mjs joinquant_heat_rotation.py --cookie-file .joinquant_cookie
```

### 3.3 工作流程

```
1. 打开策略编辑页 (https://www.joinquant.com/algorithm/index/edit)
   ├── 如果指定 ALGORITHM_ID, 编辑已有策略
   └── 否则创建新策略

2. 等待 Ace 编辑器加载

3. 关闭弹窗 (新手引导、资源提示等)

4. 注入策略代码
   ├── 小文件 (<1200 bytes): 直接注入
   └── 大文件: Base64 分块注入 (每块1500字符)

5. 保存代码 (Meta+S / Ctrl+S)

6. 设置回测参数
   ├── 起始日期 (startTime)
   ├── 结束日期 (endTime)
   └── 初始资金 (daily_backtest_capital_base_box)

7. 触发回测
   ├── 优先 jQuery click (#daily-new-backtest-button)
   ├── 降级 DOM click
   └── 降级文本匹配 click

8. 处理「继续运行」弹窗 (如果触发)

9. 轮询回测结果 (每9秒)
   ├── 导航到回测列表页
   ├── 点击第一个回测结果 → 详情页
   ├── 探针检测: 运行中/完成/失败
   ├── 提取回测指标 (正则匹配)
   ├── 截图留存 (_poll_NNN.png)
   └── 抓取策略日志 (点击「日志输出」标签)

10. 回测完成 → 输出到 stdout + .jq_backtests/{session_id}.json
```

## 4. 输出格式

### 4.1 控制台输出 (stdout)

```
=== BACKTEST COMPLETE ===
{
  "strategy_return": "48.28%",
  "annual_return": "19.15%",
  "sharpe": "0.568",
  "max_drawdown": "28.15%",
  "excess_return": "5.83%",
  "benchmark_return": "40.11%",
  "run_time": "01分07秒 Python3",
  "status_line": "回测完成 ，实际耗时01分07秒 Python3"
}
```

### 4.2 状态文件 (`.jq_backtests/{session_id}.json`)

```json
{
  "state": "success",
  "stage": "completed",
  "session_id": "mos456x3",
  "metrics": { "strategy_return": "48.28%", ... },
  "complete_log": "2024-01-01 00:00:00 - INFO - [INIT] ...",
  "running_elapsed_sec": 67,
  "url": "https://www.joinquant.com/algorithm/backtest/detail?backtestId=..."
}
```

### 4.3 截图文件 (`.jq_backtests/`)

| 文件 | 说明 |
|------|------|
| `{sid}_after_trigger.png` | 触发回测后的页面 |
| `{sid}_poll_001.png` | 第1次轮询时的页面 |
| `{sid}_poll_002.png` | 第2次轮询时的页面 |
| `{sid}_poll_003.png` | ...最后一次轮询 |
| `{sid}_failed.png` | 失败时的截图 |
| `{sid}_timeout.png` | 超时时的截图 |

## 5. 策略代码适配 (Python → 聚宽)

### 5.1 必须保留的导入

```python
import numpy as np
import pandas as pd
```

**注意**: 聚宽回测环境**不需要** `from jqdata import *`, 但加了也不会报错。

### 5.2 API 差异

| 功能 | 生产环境 (trading_core) | 聚宽回测 |
|------|------------------------|---------|
| 行情数据 | `OpenClawChinaStockDataSource` | `get_price()` / `history()` |
| 热度数据 | `同花顺 concept_db` (真实热度分) | 成交量+价格动量 代理热度分 |
| 概念板块 | SQLite `concept_db` | `get_concepts()` (慢, 慎用) |
| 下单 | `BaseStrategy._execute_trades()` | `order_target_value()` / `order_target()` |
| 持仓 | `PortfolioState.positions` | `context.portfolio.positions` |
| 日志 | `logging.getLogger()` | `print()` (输出到日志) |
| 定时调度 | `schedule` 模块 | `run_daily(func, time='14:50')` |
| 全局状态 | dataclass `LongTermSettings` | `g` 对象 |
| 交易日历 | `trading_calendar.py` | `get_trade_days()` |

### 5.3 关键适配点

#### 5.3.1 全局对象: `settings` → `g`

```python
# 生产环境
settings.max_holdings = 5

# 聚宽回测
g.max_holdings = 5
```

#### 5.3.2 数据获取: DataSource → get_price()

```python
# 生产环境 (简化示意)
history = data_source.fetch_batch_daily_history(codes, count=60)

# 聚宽回测
df = get_price(
    codes, count=60, end_date=today,
    frequency='daily', fields=['close', 'volume', 'money'],
    skip_paused=True, fq='pre', panel=False
)
```

#### 5.3.3 下单: BaseStrategy → order_target_value()

```python
# 生产环境
plan = build_rebalance_plan(trade_date, candidates, portfolio, quotes, settings)

# 聚宽回测
for stock, weight in target_weights.items():
    order_target_value(stock, context.portfolio.portfolio_value * weight)
```

#### 5.3.4 热度数据: THS → Heat Proxy

```python
# 生产环境: 使用同花顺真实热度分
heat_score = float(row.get("ths_heat_score", 0.0))

# 聚宽回测: 使用成交量+价格动量代理
heat_score = _compute_heat_proxy(closes, volumes)
```

### 5.4 已知的聚宽环境陷阱

1. **`sum()` 被 shadow**: 聚宽环境可能做了 `from numpy import *`, 导致内置 `sum()` 表现异常。**必须使用显式循环替代 `sum()`**。

   ```python
   # ❌ 错误: sum() 可能返回 dict_values 对象
   total = sum(weights.values())

   # ✅ 正确: 显式循环
   total = 0.0
   for v in weights.values():
       total += float(v)
   ```

2. **`get_current_data()` 不可靠**: 在回测中 `get_current_data()[code]` 可能 KeyError。用 try/except 或用纯代码过滤替代。

3. **科创板(688)市价单**: 聚宽要求科创板市价单指定保护限价。策略应**过滤掉688开头的股票**。

4. **`get_concepts()` 太慢**: 在回测中调用 `get_concepts()` 获取概念板块非常耗时。**避免在每次调仓时调用**。

5. **`log.info()` 输出可能不可见**: 使用 `print()` 代替 `log.info()`, `print()` 的输出会出现在「日志输出」标签页。

6. **代码体积限制**: 策略代码过大时 (>30KB), 注入需要分块。`jq_automation_worker.mjs` 已自动处理。

## 6. 策略回测迭代流程

```bash
# 1. 编辑策略代码
vim joinquant_heat_rotation.py

# 2. 运行回测
node jq_automation_worker.mjs joinquant_heat_rotation.py --cookie-file .joinquant_cookie

# 3. 查看结果 (从 stdout 或 .jq_backtests/ 最新 JSON)
cat .jq_backtests/$(ls -t .jq_backtests/*.json | head -1) | python3 -m json.tool

# 4. 查看策略日志 (找 [DEBUG] 等输出)
cat .jq_backtests/$(ls -t .jq_backtests/*.json | head -1) | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('complete_log','')[:5000])"

# 5. 根据日志调整策略 → 回到步骤1
```

## 7. 调试技巧

### 7.1 在策略中加 `print()` 调试

聚宽回测的 `print()` 输出会完整显示在「日志输出」标签页。在每个关键步骤加 `print()`：

```python
def rebalance(context):
    print('[DEBUG] universe=%d stocks' % len(universe))
    print('[DEBUG] df_price columns: %s' % str(list(df_price.columns)))
    print('[DEBUG] valid_stocks=%d (need >=%d)' % (len(valid_stocks), g.max_holdings))
    print('[DEBUG] stock_scores=%d' % len(stock_scores))
    print('Target stocks: %s' % str(target_stocks))
```

### 7.2 使用极简测试策略验证流水线

```bash
# test_minimal.py 是一个极简策略，用于验证注入-回测流水线是否正常
node jq_automation_worker.mjs test_minimal.py --cookie-file .joinquant_cookie
```

### 7.3 从聚宽网页直接查看日志

1. 打开回测详情页 URL (从 stdout 或 status JSON 获取)
2. 点击左侧「日志输出」
3. 查看 `[DEBUG]` / `[ERROR]` 等标记的输出
4. 如果有弹窗遮挡, 先点「取消」关闭

### 7.4 分析回测交易明细

在聚宽网页上:
- 「交易详情」→ 查看每笔买卖的时间、价格、数量
- 「每日持仓&收益」→ 查看持仓变动
- 「收益概述」→ 查看整体收益曲线

### 7.5 常见失败原因

| 症状 | 可能原因 | 排查方法 |
|------|---------|---------|
| 回测失败 (4秒) | Python 编译/运行错误 | 查看「日志输出」标签的 Traceback |
| 0笔交易 | 过滤条件过严, 没有股票入选 | 查看 `[DEBUG] stock_scores` 是否 = 0 |
| 收益为0 | `get_price()` 返回空 | 查看 `[DEBUG] df_price` 是否 empty=True |
| 耗时异常短 | 策略提前 return | 每个早退点加 `print()` 定位 |
| `sum()` 异常 | JoinQuant 环境 shadow | 全部改用显式循环 |

## 8. 策略文件结构

```
/root/.openclaw/workspace/trading/
├── joinquant_heat_rotation.py    # 聚宽回测版策略 (可直接复制到JQ编辑器)
├── jq_automation_worker.mjs      # 自动化回测工具
├── test_minimal.py               # 极简测试策略 (验证流水线)
├── .joinquant_cookie             # 聚宽登录 Cookie (私密)
├── .jq_backtests/                # 回测结果存档
│   ├── {sid}.json                # 状态+指标+日志
│   ├── {sid}_after_trigger.png   # 触发后截图
│   ├── {sid}_poll_*.png          # 轮询截图
│   └── {sid}_failed.png          # 失败截图
└── docs/
    ├── heat_rotation_strategy.md # 热度轮动策略文档
    └── joinquant_backtest_guide.md # 本文档
```

## 9. 相关文档

- [热度轮动策略](./heat_rotation_strategy.md)
- [系统架构与数据流](./longterm_architecture_dataflow.md)
- [运维手册](./longterm_scheduler_runbook.md)

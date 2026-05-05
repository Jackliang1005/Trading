# 热度轮动策略 (Heat Rotation Strategy)

> 最后更新: 2026-05-05 | 当前模式: 创业板小市值 (GEM Small Cap Rotation)

## 1. 策略概述

当前运行版本为 **创业板小市值轮动策略**，核心理念是：

- **创业板 (30xxxx)** 股票池，聚焦小市值成长股
- **基本面质量过滤**：市值 5-50 亿 + 营收同比增长 ≥15%
- **按市值升序排列**，取最小市值的前 20 只 (跳过前 10 只微盘)
- **HRS 综合评分**选股，结合热度加速度、板块动量、价格趋势、流动性
- **5 条出场规则**控制下行风险
- 每日盘后自动扫描 → LLM 分析 → 飞书推送调仓建议

回测基线 (gem_small_cap_baseline.py): 创业板指基准, 止盈 120%, 止损 10%。

## 2. 股票池 (创业板小市值)

### 2.1 初筛

| 条件 | 值 |
|------|-----|
| 板块 | 创业板 (30xxxx) |
| 市值范围 | 5 亿 ~ 50 亿 |
| 营收同比增长 | ≥ 15% |
| 上市天数 | ≥ 375 天 |
| 排除 | ST、退市 |

### 2.2 排序与选取

1. 按市值**升序**排列 (小市值优先)
2. 跳过前 `gem_rank_start` (10) 只 (过滤微盘/问题股)
3. 取 `gem_final_pool_size` (20) 只纳入候选池

## 3. 策略架构

```
_sync_universe_core()
  ├── 从持仓构建候选
  ├── 板块轮动预判选股
  └── GEM小市值候选注入 (当 gem_universe=True)

run_post_market_scan()
  ├── 计算热度加速度 (heat_accel)
  ├── 计算板块动量 (sector_momentum, 基于概念DB)
  ├── 计算价格趋势 (price_trend)
  ├── 计算流动性质量 (liquidity_score)
  └── HRS = Σ(维度 × 权重)

build_rebalance_plan() [rotation_mode=True]
  ├── 市场趋势检测 (CSI500 MA60/MA20)
  ├── 主题分配 (_theme_allocation)
  ├── 选股 (_theme_based_weight_map)
  ├── 出场信号 (_generate_exit_signals, 5条规则)
  └── 生成调仓计划

LLM Advisor → 飞书推送
```

## 3. HRS 综合评分 (Heat Rotation Score)

### 3.1 四个评分维度

| 维度 | 权重 | 说明 |
|------|------|------|
| 热度加速度 (heat_accel) | 0.20 | 5日热度变化率, 缩放后映射到0-100 |
| 板块动量 (sector_mom) | 0.10 | 概念板块动量代理 (简化版用5日涨幅归一化) |
| 价格趋势 (price_trend) | 0.40 | MA排列(40) + RSI健康度(30) + MA5偏离度(30) |
| 流动性质量 (liquidity) | 0.30 | 成交额百分位(40) + 量比(30) + 换手适中度(30) |

### 3.2 热度代理 (Heat Proxy)

在无法获取同花顺热度数据时，使用成交量和价格动量计算代理热度分 (0-100)：

- **量能比 (0-30)**: 5日均量 / 20日均量
- **价格动量 (0-30)**: 5日涨跌幅
- **涨停接近度 (0-20)**: 近5日是否有接近涨停的涨幅
- **波动率 (0-20)**: 20日收益率标准差

### 3.3 入场过滤

| 过滤器 | 阈值 | 目的 |
|--------|------|------|
| 最小热度 | >= 15 | 排除冷门股 |
| 最大热度 | <= 75 | 排除过买股 (顶部接盘) |
| RSI | <= 75 | 排除超买 (>75) |
| MA20趋势 | close > MA20 | 只做上升趋势 |
| 科创板(688) | 排除 | 市价单需保护限价, 策略不支持 |

## 4. 出场规则 (5条)

### 规则1: 热度衰减
```
当前热度 < 入场热度 × heat_exit_decay (0.35)
```
热度显著下降时退出。

### 规则2: 趋势破位 (带缓冲区)
```
close < MA10 × (1 - trend_break_pct) AND MA5 < MA10
trend_break_pct = 0.03 (3%)
```
价格跌破MA10且MA5死叉MA10时退出。**3%缓冲区避免噪音触发**。

### 规则3: 超时持有
```
持有交易日 > max_hold_days (40)
```
避免长期套牢。

### 规则4: 追踪止损
```
当前价 < 入场后最高价 × (1 - trailing_stop_pct)
trailing_stop_pct = 0.18 (18%)
```
保护浮盈, 限制回撤。

### 规则5: 主题退场
```
持仓的主题已跌出 Top-5 热门主题
```
当股票所属的热门概念退潮时退出。

## 5. 市场趋势自适应

策略自动检测 CSI 500 的牛熊状态：

```python
is_bull = close > MA60 AND MA20 > MA60
```

| 市场状态 | 最大持仓 | 说明 |
|----------|---------|------|
| 牛市 | 5只 | 全仓轮动 |
| 熊市 | 3只 | 降低风险暴露 |

## 6. 策略参数

### 6.1 持仓与仓位

| 参数 | 值 | 说明 |
|------|-----|------|
| rotation_max_holdings | 5 | 牛市最大持仓数 |
| max_holdings_bear | 3 | 熊市最大持仓数 |
| single_name_cap | 0.25 | 单票上限 25% |
| max_theme_weight | 0.50 | 单主题上限 50% |
| max_per_theme | 2 | 每主题最多选股数 |
| cash_buffer | 0.05 | 现金缓冲 5% |

### 6.2 调仓参数

| 参数 | 值 | 说明 |
|------|-----|------|
| rebalance_freq | 10 | 调仓频率 (交易日) |
| lookback | 60 | 历史数据窗口 (交易日) |
| heat_accel_window | 5 | 热度加速度窗口 |

### 6.3 出场参数

| 参数 | 值 | 说明 |
|------|-----|------|
| max_hold_days | 40 | 最大持有交易日 |
| trailing_stop_pct | 0.18 | 追踪止损 18% |
| heat_exit_decay | 0.35 | 热度衰减退出阈值 |
| min_heat_entry | 15.0 | 最低入场热度 |
| max_heat_entry | 75.0 | 最高入场热度 |
| rsi_max_entry | 75.0 | RSI 过买阈值 |
| trend_break_pct | 0.03 | 趋势破位缓冲 3% |

### 6.4 市场趋势

| 参数 | 值 | 说明 |
|------|-----|------|
| market_trend_index | 000905.XSHG | 趋势检测标的 (中证500) |
| exclude_star_board | True | 排除科创板 (688) |

## 7. HRS 选股公式

```
HRS = heat_accel_scaled × 0.20
    + sector_momentum × 0.10
    + price_trend × 0.40
    + liquidity × 0.30

heat_accel_scaled = clamp(50 + heat_accel × 20, 0, 100)
```

### 主题分配算法

1. 按 `sector_momentum` 分值将股票分为4组
2. 每组等权分配 25% 资金
3. 组内按 HRS 排序, 每主题最多取 `max_per_theme` 只
4. 目标权重: 50% 等权 + 50% HRS 加权
5. 归一化 → 应用单票上限 → 再归一化

## 8. 调仓执行

```
1. 卖出不在目标池的持仓
2. 对目标池股票执行 order_target_value(portfolio_value × weight)
3. 记录入场信息 (日期、热度、价格)
```

调仓日同时处理卖出 (not_in_target) 和买入 (target_weight), 确保资金高效利用。

## 9. GEM 小市值配置参数

| 参数 | 值 | 说明 |
|------|-----|------|
| gem_universe | true | 启用 GEM 小市值候选池 |
| gem_board_prefix | "30" | 创业板代码前缀 |
| gem_market_cap_min | 5.0 | 最小市值 (亿) |
| gem_market_cap_max | 50.0 | 最大市值 (亿) |
| gem_revenue_yoy_min | 0.15 | 营收同比增长门槛 |
| gem_min_listing_days | 375 | 最少上市天数 |
| gem_universe_limit | 200 | 候选池上限 |
| gem_rank_start | 10 | 跳过前 N 只 (过滤微盘) |
| gem_final_pool_size | 20 | 最终候选池大小 |

## 10. 代码位置

| 组件 | 位置 |
|------|------|
| 策略模型 (字段定义) | `trading_core_new/longterm/models.py` → `LongTermSettings` |
| 策略引擎 (核心逻辑) | `trading_core_new/longterm/engine.py` |
| 盘后扫描 | `trading_core_new/longterm/post_market_scanner.py` |
| 数据源 (GEM候选获取) | `trading_core_new/longterm/data_source.py` → `fetch_gem_candidates()` |
| CLI 入口 (universe sync) | `trading_core_new/longterm/cli.py` → `_sync_universe_core()` |
| 策略配置 | `trading_data/longterm/settings.json` |
| 聚宽回测版 (baseline) | `joinquant_heat_rotation.py` (来自 qmttrader/strategies/jukuan/gem_small_cap_baseline.py) |
| 聚宽自动化 | `jq_automation_worker.mjs` |
| 定时调度 | `deploy/systemd/trading-evening.{service,timer}` → 每交易日 15:35 |

## 10. 相关文档

- [聚宽自动回测指南](./joinquant_backtest_guide.md)
- [系统架构与数据流](./longterm_architecture_dataflow.md)
- [运维手册](./longterm_scheduler_runbook.md)
- [平台设计](./longterm_sim_platform_design.md)

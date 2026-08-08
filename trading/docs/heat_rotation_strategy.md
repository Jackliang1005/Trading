# 热度轮动策略 (Heat Rotation Strategy)

> 最后更新: 2026-05-06 | 当前模式: 创业板主线科技成长轮动 (GEM Mainline Growth Rotation)

## 1. 策略概述

当前运行版本为 **创业板主线科技成长轮动策略**，核心理念是：

- **创业板 (30xxxx)** 股票池，但不再迷恋纯微盘
- **基本面质量过滤**：市值 15-120 亿 + 营收同比增长 ≥15% + 净利润同比 ≥0% + 毛利率 ≥18%
- **主线识别 + 综合评分**：优先 AI 算力、光通信、半导体、国产替代、先进制造等产业趋势方向
- **流动性与趋势过滤**：20 日日均成交额、MA 结构、短期动量共同确认，避免“小 + 冷门”
- **5 条出场规则**控制下行风险
- 每日盘后自动扫描 → LLM 分析 → 飞书推送调仓建议

回测基线 (gem_small_cap_baseline.py): 创业板指基准, 止盈 120%, 止损 10%。

## 2. 股票池 (创业板主线科技成长)

### 2.1 初筛

| 条件 | 值 |
|------|-----|
| 板块 | 创业板 (30xxxx) |
| 市值范围 | 15 亿 ~ 120 亿 |
| 营收同比增长 | ≥ 15% |
| 净利润同比增长 | ≥ 0% |
| 毛利率 | ≥ 18% |
| 上市天数 | ≥ 500 天 |
| 排除 | ST、退市 |

### 2.2 排序与选取

1. 先做 **流动性过滤**：20 日日均成交额必须达到最低阈值
2. 再做 **趋势过滤**：股价至少站上 MA20，且 MA 结构不能过弱
3. 再做 **主线过滤**：行业/概念需要命中科技主线关键词
4. 最后按综合评分排序，取 `gem_final_pool_size` (25) 只纳入候选池

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

### 3.1 五个评分维度

| 维度 | 权重 | 说明 |
|------|------|------|
| 主线强度 (mainline) | 0.35 | 是否命中 AI 算力/半导体/国产替代/先进制造等主线 |
| 业绩质量 (growth) | 0.25 | 营收同比 + 净利润同比 + 毛利率 |
| 流动性质量 (liquidity) | 0.20 | 20 日日均成交额，弱化结构性流动性陷阱 |
| 价格趋势 (trend) | 0.10 | MA5/MA10/MA20 结构 + 5 日动量 |
| 市值甜蜜区间 (size) | 0.10 | 偏好 20-80 亿，兼顾弹性与可交易性 |

### 3.2 主线优先原则

策略不再把“越小越好”当成核心，而是强调：

- **小 + 主线**
- **小 + 流动性**
- **小 + 业绩质量**

主线关键词优先覆盖：

- AI 算力 / 数据中心 / 服务器 / 交换机 / 光模块 / CPO / 液冷 / 铜缆
- 半导体 / 芯片 / 存储 / HBM / GPU / 封装 / 晶圆 / EDA
- 国产替代 / 自主可控 / 信创
- 机器人 / 自动化 / 智能制造

### 3.3 入场过滤

| 过滤器 | 阈值 | 目的 |
|--------|------|------|
| 日均成交额 | >= 1.2 亿 | 排除冷门和流动性陷阱 |
| 主线评分 | >= 55 | 只保留科技主线或强产业趋势方向 |
| 趋势评分 | >= 45 | 降低逆势抄底 |
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

策略自动检测 **创业板指 + 中证 2000** 的联动状态：

```python
strong = GEM > MA20 AND CSI2000 > MA20
neutral = GEM > MA20 OR CSI2000 > MA20
weak = GEM < MA20 AND CSI2000 < MA20
```

| 市场状态 | 最大持仓 | 说明 |
|----------|---------|------|
| 强势 | 4只 | 主线环境配合时扩大轮动 |
| 中性 | 3只 | 保持进攻但不过度外扩 |
| 弱势 | 2只 | 只保留最强主线，显著降风险 |

## 6. 策略参数

### 6.1 持仓与仓位

| 参数 | 值 | 说明 |
|------|-----|------|
| base_stock_sum | 4 | 强势环境最大持仓数 |
| neutral_stock_sum | 3 | 中性环境目标持仓数 |
| weak_stock_sum | 2 | 弱势环境目标持仓数 |
| single_name_cap | 0.25 | 单票上限 25% |
| max_per_stock | 250000 | 单票名义资金上限 |

### 6.2 调仓参数

| 参数 | 值 | 说明 |
|------|-----|------|
| rebalance_freq | 每周一 | 周频主调仓 |
| lookback | 20 | 趋势/流动性评分窗口 |
| concept_check | 调仓时 | 仅在候选池阶段读取概念/行业 |

### 6.3 出场参数

| 参数 | 值 | 说明 |
|------|-----|------|
| max_hold_days | 40 | 最大持有交易日 |
| trailing_stop_pct | 0.18 | 追踪止损 18% |
| heat_exit_decay | 0.35 | 热度衰减退出阈值 |
| min_avg_money_20d | 1.2e8 | 最低 20 日日均成交额 |
| target_avg_money_20d | 3.5e8 | 理想成交额中枢 |
| min_mainline_score | 55 | 主线方向最低分 |
| min_price_trend_score | 45 | 趋势最低分 |
| trend_break_pct | 0.03 | 趋势破位缓冲 3% |

### 6.4 市场趋势

| 参数 | 值 | 说明 |
|------|-----|------|
| market_trend_index | 399006.XSHE | 主趋势检测标的 (创业板指) |
| market_support_index | 000852.XSHG | 小票确认标的 (中证2000) |
| exclude_star_board | True | 排除科创板 (688) |

## 7. HRS 选股公式

```
Score = mainline × 0.35
      + growth × 0.25
      + liquidity × 0.20
      + trend × 0.10
      + size × 0.10
```

### 评分逻辑

- 主线越清晰，分数越高
- 业绩和盈利质量越强，分数越高
- 成交额越稳定，越能穿越 A 股结构性流动性波动
- 市值不再单纯越小越好，而是偏好 20-80 亿甜蜜区间
- 排序时若总分接近，优先主线强度、业绩质量和流动性更好的标的

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
| gem_market_cap_min | 15.0 | 最小市值 (亿) |
| gem_market_cap_max | 120.0 | 最大市值 (亿) |
| gem_revenue_yoy_min | 0.15 | 营收同比增长门槛 |
| gem_netprofit_yoy_min | 0.0 | 净利润同比门槛 |
| gem_gross_profit_margin_min | 18.0 | 毛利率门槛 |
| gem_min_listing_days | 500 | 最少上市天数 |
| gem_universe_limit | 200 | 候选池上限 |
| gem_rank_start | 0 | 不再依赖机械跳过最小市值 |
| gem_final_pool_size | 25 | 最终候选池大小 |

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
| 定时调度 | `deploy/systemd/trading-evening.{service,timer}` → 每交易日 20:30 |

## 10. 相关文档

- [聚宽自动回测指南](./joinquant_backtest_guide.md)
- [系统架构与数据流](./longterm_architecture_dataflow.md)
- [运维手册](./longterm_scheduler_runbook.md)
- [平台设计](./longterm_sim_platform_design.md)

# 做T策略方案总览

## 1. 文档目的

本文档用于给外部 agent / reviewer 做审计，目标是快速说明当前 Trading 系统的：

- 策略目标
- 核心算法
- 数据流与执行流
- 风控闭环
- 学习回路
- 当前已知边界与待审计点

本文档描述的是当前代码实现，而不是理想设计。

---

## 2. 系统目标

系统目标不是“选出明天涨停股”，也不是“全自动开新仓交易”，而是：

1. 以真实持仓为前提，对持仓池进行盘前筛选。
2. 在盘中识别适合做T的结构与价格位置。
3. 给出唯一动作：`buy / sell / wait`。
4. 对自动交易施加预算、轮次、禁买、可卖数量等约束。
5. 用真实成交结果反哺参数，而不是只做静态规则提醒。

一句话概括：

`持仓型做T Agent = 盘前选票 + 盘中结构决策 + 预算风控 + 执行闭环 + 复盘学习`

---

## 3. 当前架构

核心模块：

- [engine.py](/root/.openclaw/workspace/trading/trading_core/engine.py)
  主循环；负责盘前筛选、盘中上下文刷新、信号触发、自动交易调用。
- [analysis.py](/root/.openclaw/workspace/trading/trading_core/analysis.py)
  行情获取、分钟线分析、结构分类、做T参考位生成、机会评分。
- [selector_agent.py](/root/.openclaw/workspace/trading/trading_core/selector_agent.py)
  负责 `focus / watch / avoid`、`AM/PM mode`、预算和建议股数。
- [budget_agent.py](/root/.openclaw/workspace/trading/trading_core/budget_agent.py)
  负责现金预算与风险单位约束下的建议股数。
- [decision_engine.py](/root/.openclaw/workspace/trading/trading_core/decision_engine.py)
  负责将 playbook、结构、模式、预算与学习参数合成为唯一动作。
- [risk_engine.py](/root/.openclaw/workspace/trading/trading_core/risk_engine.py)
  负责自动交易边界：禁买、轮次、可卖数量、市场 regime。
- [news_agent.py](/root/.openclaw/workspace/trading/trading_core/news_agent.py)
  负责新闻、外盘同行、板块映射与禁买判断。
- [learning.py](/root/.openclaw/workspace/trading/trading_core/learning.py)
  负责从近端真实盈亏和决策日志回算 `risk_factor / entry_tolerance / preferred_structure`。
- [review_engine.py](/root/.openclaw/workspace/trading/trading_core/review_engine.py)
  负责决策日报、模式复盘、结构归因基础数据。
- [command_service.py](/root/.openclaw/workspace/trading/trading_core/command_service.py)
  负责飞书命令、查询、执行、回执、状态展示。
- [execution.py](/root/.openclaw/workspace/trading/trading_core/execution.py)
  负责仓位状态、指令簿、执行状态更新。

---

## 4. 当前策略骨架

### 4.1 盘前层

盘前层的任务是决定“今天哪些持仓值得做T，以及偏什么模式”。

输入：

- 基础持仓配置：[做T监控配置.json](/root/.openclaw/workspace/trading/做T监控配置.json)
- 初始持仓真源：[初始持仓数据库.json](/root/.openclaw/workspace/trading/初始持仓数据库.json)
- 行情/资金流
- 新闻/题材/外盘同行

输出：

- `focus / watch / avoid`
- `selection_am_mode / selection_pm_mode`
- `selection_buy_budget_amount / selection_sell_budget_amount`
- `selection_buy_shares / selection_sell_shares`
- `selection_buy_blocked / selection_buy_block_reason`

关键点：

- 外盘同行链路：`TickDB -> Finnhub -> AkShare -> 新闻 -> 人工禁买`
- 新闻多源增强：优先 `财联社 / 华尔街见闻 / 第一财经 / 东方财富 / 新浪 / 同花顺 / 富途`
- 学习参数已进入选票层：正向样本会提高分数，负向样本会压低分数

### 4.2 盘中层

盘中层的任务是回答两个问题：

1. 现在是什么结构？
2. 这个结构下，当前价位是否允许做T？

当前结构分类：

- `trend_up`
- `trend_down`
- `range`
- `panic_rebound`
- `neutral`

分析方法在 [analysis.py](/root/.openclaw/workspace/trading/trading_core/analysis.py)：

- 取分钟 bars
- 计算 `VWAP`
- 计算 `MA20 / MA60`
- 提取近期分时 pivot highs / lows
- 计算前日 `PP / S1 / R1`
- 估算分钟 ATR 近似，记作 `risk_unit`

然后先分类结构，再在结构内部生成：

- `t_buy_target`
- `t_sell_target`
- `t_spread_pct`
- `confidence`

这是当前系统和旧版最大的区别：

- 旧版：先算若干支撑阻力，再经验加权
- 新版：先识别结构，再根据结构选择参考位权重

### 4.3 决策层

当前 playbook：

- `trend_sell_rebuy`：顺T
- `panic_reverse_t / reverse_t`：逆T
- `box_range_t`：箱体
- `observe_only`：观察

决策原则：

- 顺T只在 `trend_up / range` 更积极
- 逆T只在 `panic_rebound / 超跌回转` 更积极
- 箱体T只在 `range` 内执行
- `trend_down` 会显著压制买入动作

决策输出：

- `action`
- `score`
- `level`
- `trigger_price`
- `execution_price`
- `target_price`
- `stop_price`
- `allow_auto_trade`
- `risk_flags`

### 4.4 资金管理层

当前已经不是简单固定股数。

建议股数由两层约束共同决定：

1. 预算约束
   来自账户机动资金、观察池分组、票的分数、当日剩余额度

2. 风险单位约束
   来自 `volatility_unit` 和学习回路算出的 `risk_factor`

最终逻辑：

`建议股数 = min(预算允许股数, 风险单位允许股数)`

因此：

- 高波动票会自动缩小建议仓位
- 正反馈票会提高风险系数
- 负反馈票会降低风险系数

---

## 5. 风控体系

### 5.1 盘前禁买

以下条件可压制主动买入：

- 外盘同行大跌
- 板块/新闻负面
- 人工 `T 外盘风险`
- `buy_blocked = true`

### 5.2 盘中模式限制

通过 `selection_am_mode / selection_pm_mode` 限制：

- `sell_only`
- `observe_only`
- `trend_t_only`
- `reverse_t_only`
- `box_t_only`

即使价格进入买区，`sell_only / observe_only` 下也不会放行买入。

### 5.3 自动交易限制

自动交易只在这些条件都满足时才允许：

- `focus` 票
- `decision.allow_auto_trade = true`
- `trade_connected = true`
- `可卖数量` 充足
- 未触发 `round_trip_limit`
- 未触发 `daily_auto_trade_limit`
- 未命中 `buy_blocked`

### 5.4 状态一致性

当前已修复的状态闭环：

- `submitted` 指令可在成交回执后转为 `executed`
- 自动计数只在成功提交后累计
- 手工 `T 已买 / T 已卖` 会更新轮次状态
- `monitor_state` 按交易日自动重置，避免隔日脏状态

---

## 6. 学习回路

### 6.1 当前不是强化学习

当前学习层不是 RL，而是轻量的近端参数自适应：

- 从近端 `trades_YYYY-MM-DD.json` 提取真实卖出盈亏
- 从 `decision_journal_YYYY-MM-DD.json` 匹配最近决策
- 统计：
  - `sample_count`
  - `win_rate`
  - `avg_profit`
  - `preferred_structure`

然后计算：

- `bias`
- `risk_factor`
- `entry_tolerance`
- `preferred_structure`

### 6.2 学习参数如何作用到系统

学习参数当前作用到三层：

1. 选票层
   正反馈票加分，负反馈票减分

2. 决策层
   偏好结构匹配时加分
   `entry_tolerance` 影响入场容忍带

3. 资金层
   `risk_factor` 影响建议股数

### 6.3 当前学习输出

学习快照文件：

- [learning_snapshot.json](/root/.openclaw/workspace/trading/trading_data/learning_snapshot.json)

字段：

- `sample_count`
- `win_rate`
- `avg_profit`
- `bias`
- `risk_factor`
- `entry_tolerance`
- `preferred_structure`

---

## 7. 关键数据文件

- [做T监控配置.json](/root/.openclaw/workspace/trading/做T监控配置.json)
  基础配置、股票池、区间、风控参数
- [初始持仓数据库.json](/root/.openclaw/workspace/trading/初始持仓数据库.json)
  真实底仓真源
- [今日交易计划.json](/root/.openclaw/workspace/trading/今日交易计划.json)
  当日 selector 回写后的计划
- [monitor_state.json](/root/.openclaw/workspace/trading/trading_data/monitor_state.json)
  盘中信号、冷却、轮次、摘要推送状态
- [portfolio_state.json](/root/.openclaw/workspace/trading/trading_data/portfolio_state.json)
  当日仓位、今买今卖、可卖、可回补
- [command_book.json](/root/.openclaw/workspace/trading/trading_data/command_book.json)
  指令簿
- [focus_list.json](/root/.openclaw/workspace/trading/trading_data/focus_list.json)
  观察池结果
- [universe_state.json](/root/.openclaw/workspace/trading/trading_data/universe_state.json)
  全量选票结果
- [selection_review_2026-03-31.json](/root/.openclaw/workspace/trading/trading_data/selection_review_2026-03-31.json)
  选股日报示例
- [learning_snapshot.json](/root/.openclaw/workspace/trading/trading_data/learning_snapshot.json)
  学习快照

---

## 8. 当前方案的优点

1. 已经从“单票提醒脚本”升级到“可执行做T系统”
2. 结构分类和波动单位已经进入主决策链路
3. 外盘同行风险已实装，不再无视美股/韩股存储链下跌
4. 预算已考虑：
   - 已成交净买入占用
   - 未成交买单占用
5. 自动/手工执行状态已经基本闭环
6. 学习参数已能反向影响仓位和入场容忍度

---

## 9. 当前已知边界

以下不是 bug，但仍是审计时应重点关注的边界：

1. 学习参数仍是“近端经验自适应”，不是严格统计学习模型
2. 结构归因目前还是“最近决策匹配最近卖出”，不是逐笔订单级映射
3. 新闻多源仍是“搜索增强”，不是 datasource 级强约束路由
4. `risk_unit`、结构阈值、风险倍率仍是工程经验参数，尚未离线回测
5. 自动交易当前是“成功提交即累计”，不是“券商最终成交反馈累计”

---

## 10. 建议审计重点

建议外部 agent 重点审计以下问题：

1. 结构分类逻辑是否稳定
   审计 `_classify_intraday_structure()`

2. `risk_unit` 与建议股数是否合理
   审计 `compute_intraday_analysis()` 与 `allocate_t_budget()`

3. 顺T/逆T/箱体的结构约束是否会冲突
   审计 `_derive_action()` 与 `adjust_for_strategy()`

4. 学习回路是否会放大噪声
   审计 `load_learning_profile()`

5. 选票层与决策层是否存在重复加权
   审计 `selector_agent.py` 与 `decision_engine.py` 的学习加分

6. 执行状态是否完全闭环
   审计 `execution.py`、`command_service.py`、`engine.py`

7. 运行态与配置态是否完全隔离
   审计 `做T监控配置.json`、`今日交易计划.json`、`monitor_state.json` 的职责边界

---

## 11. 当前不建议直接做的事

当前不建议直接上在线强化学习。

原因：

- 样本量不足
- 状态空间还没标准化
- 缺少安全可探索模拟环境
- 当前更适合做离线参数校准与弱监督优化

若未来考虑更高级训练，建议顺序：

1. 先标准化训练样本
2. 再做离线评分模型/参数搜索
3. 最后才考虑 contextual bandit / offline RL

---

## 12. 审计结论模板

给其他 agent 的建议审计口径：

1. 先确认当前实现是否与本文档一致
2. 再判断算法是否有逻辑漏洞
3. 再判断参数是否过于经验化
4. 最后判断是否适合继续自动交易扩容

不建议只做代码风格 review；优先做：

- 行为正确性
- 风控正确性
- 资金管理一致性
- 学习回路是否会误导系统

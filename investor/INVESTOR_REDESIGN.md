# Investor 模块重设计

## 1. 当前模块已经具备的功能

基于 `HANDOFF.md` 与当前代码实现核对，`investor` 目前不是空壳，已经具备一个可运行的闭环原型。

### 1.1 调度与入口

- `main.py` 提供 CLI 入口
- 支持命令：
  - `init`
  - `collect`
  - `predict`
  - `reflect`
  - `evolve`
  - `audit`
  - `dashboard`
  - `prompt`
  - `backtest`
  - `sector-scan`
  - `record`
- `investor_agent.py` 提供 agent 桥接能力：
  - 拉取 RAG 上下文
  - 记录预测
  - 记录反馈
  - 记录交互
  - 输出 dashboard / prompt

### 1.2 数据采集

`data_collector.py` 已具备多源采集能力：

- A 股指数实时行情
  - 优先 OpenClaw 插件
  - 回退到 QMT2HTTP
- QMT 双服务器交易数据采集
  - 账户资产
  - 持仓
  - 委托
  - 成交
  - 汇总交易摘要
- 外部市场信息采集
  - 东方财富相关数据
  - RSS 新闻
  - 全球指数
  - 大宗商品
  - 宏观新闻
- 市场快照写入 SQLite

### 1.3 QMT 统一接入

`qmt_client.py` 已实现两层封装：

- `QMTClient`
  - 单服务器 HTTP/RPC 调用
  - 账户、持仓、委托、成交、行情、板块查询、健康检查
- `QMTManager`
  - 双服务器聚合
  - 单服务器回退
  - 合并账户/持仓/委托/成交
  - 统一交易摘要

### 1.4 预测生成

`predictor.py` 已具备完整预测链路：

- 读取最新 `daily_close` 快照
- 注入多类上下文：
  - 行情
  - 板块热点
  - 新闻
  - 全球市场
  - 大宗商品
  - 宏观信息
  - QMT 持仓/委托/成交/账户状态
  - `sector_scan` 快照
  - RAG 检索结果
  - few-shot 示例
  - system prompt
- 调用 LLM
  - DeepSeek API
  - OpenRouter 回退
  - 规则预测再回退
- 解析 JSON 预测结果
- 记录到 `prediction_log`

当前预测范围：

- 上证指数
- 深证成指
- 创业板指

### 1.5 回测与反思

`reflection.py` 已具备：

- 未回测预测的自动回测
- 预测评分
  - 方向正确
  - near-miss
  - 置信度校准
  - 幅度接近度
- 周度归因分析
- 每日 / 周度 / 月度反思报告生成
- 交易摘要参与反思

### 1.6 策略进化

`evolution.py` 已具备：

- 策略权重配置读取与保存
- 基于近 14 天胜率调整权重
- 从失败案例提取规则
- 低置信规则自动禁用
- few-shot 案例维护
- system prompt 生成

### 1.7 知识库与 RAG

`knowledge_base.py` 已具备：

- 本地 SQLite 知识库存储
- 文档分类：
  - news
  - analysis
  - interaction
  - reflection
- 基于 LIKE/FTS 的检索
- 构建 RAG 上下文
- 自动记忆新闻、分析、交互

### 1.8 板块轮动与持仓诊断

`sector_scanner.py` 已具备：

- 同花顺热门板块抓取
- 美股板块 ETF 表现抓取
- 美股龙头个股表现抓取
- 美股到 A 股板块映射
- 次日轮动预判
- 当前持仓与热门板块交叉诊断
- 结果存入 `sector_scan` 快照

### 1.9 数据层

`db.py` 已具备结构化持久化：

- `prediction_log`
- `strategy`
- `feedback`
- `rules`
- `few_shot_examples`
- `reflection_reports`
- `market_snapshots`
- `interactions`


## 2. 当前模块的主要问题

当前模块的问题不是“没有功能”，而是“所有功能都堆在一起”，导致扩展性和可靠性都很差。

### 2.1 领域边界混乱

- `main.py` 同时承担 CLI、编排、业务逻辑、展示
- `predictor.py` 同时承担 prompt 拼装、数据选择、模型调用、结果解析、持久化
- `data_collector.py` 同时承担数据源适配、聚合、快照构造、异常处理
- `reflection.py` 同时承担回测、评分、报表、交易摘要解释

结果是任何一个需求变化都会牵扯多处文件。

### 2.2 “快照 JSON” 过重，结构不稳定

- `market_snapshots.data` 是大 JSON 包
- 不同快照类型缺少显式 schema
- 下游逻辑大量依赖 `dict.get(...)`
- 字段来源不统一，兼容代码越来越多

这会直接导致：

- prompt 拼接脆弱
- 调试困难
- 历史数据难以回放
- 迁移/审计成本高

### 2.3 采集、特征、结论没有分层

当前系统把三类东西混在一起：

- 原始数据
- 派生特征
- 最终判断/建议

例如：

- 持仓原始数据与“浮亏多少”“是不是热门板块核心股”混在一起
- 美股板块原始涨跌与“潜在轮动判断”混在一起
- 反思结果与规则提炼混在一起

这会让系统很难验证哪一层出了问题。

### 2.4 预测对象过窄，但提示词过重

- 现在只预测 3 个指数
- prompt 却注入了大量杂项数据
- 没有按任务类型裁剪上下文

这意味着：

- token 成本高
- 解释链不清晰
- 预测质量不一定随上下文长度提升

### 2.5 模型调用层不独立

- `predictor.py` 内直接处理 DeepSeek/OpenRouter
- 模型配置、超时、回退策略没有统一接口
- 输出解析和模型调用耦合

这会阻碍后续：

- 多模型投票
- 离线回放
- 模型评估
- A/B 测试

### 2.6 数据访问缺少 repository/service 分层

- 业务逻辑直接调用 `db.py`
- SQL 能力和业务语义混在一起
- 缺少实体模型、查询对象、服务边界

### 2.7 交易与研究耦合过深

系统把两类完全不同的事情耦在一起：

- 研究与预测
- 交易账户状态读取

账户上下文很重要，但不应让研究模块直接依赖交易网关结构。


## 3. 重设计目标

新的 `investor` 模块不应继续做“脚本集合”，而应升级为一个清晰分层的投资研究与决策支持系统。

### 3.1 核心目标

- 把“研究”与“交易接入”解耦
- 把“原始采集”与“派生分析”解耦
- 把“模型调用”与“业务流程”解耦
- 把“快照存档”升级为“可验证的数据资产”
- 让每个步骤都可单独运行、单独测试、单独回放

### 3.2 新的模块定位

Investor 应重新定义为 5 层：

1. `ingestion`
   负责原始数据接入
2. `research`
   负责特征提取、专题分析、板块分析、账户画像
3. `decision`
   负责预测、打分、规则、建议
4. `learning`
   负责反思、评估、进化
5. `interfaces`
   负责 CLI、agent、消息推送、定时任务


## 4. 新架构设计

建议重构为如下目录：

```text
investor/
├── app/
│   ├── cli.py
│   ├── scheduler.py
│   └── agent_bridge.py
├── domain/
│   ├── entities/
│   │   ├── market.py
│   │   ├── portfolio.py
│   │   ├── prediction.py
│   │   ├── reflection.py
│   │   └── strategy.py
│   ├── services/
│   │   ├── market_research_service.py
│   │   ├── prediction_service.py
│   │   ├── reflection_service.py
│   │   ├── evolution_service.py
│   │   └── portfolio_diagnosis_service.py
│   └── policies/
│       ├── scoring_policy.py
│       ├── confidence_policy.py
│       └── rotation_policy.py
├── infrastructure/
│   ├── db/
│   │   ├── sqlite.py
│   │   ├── repositories/
│   │   └── migrations/
│   ├── qmt/
│   │   ├── client.py
│   │   └── portfolio_gateway.py
│   ├── llm/
│   │   ├── client.py
│   │   ├── deepseek.py
│   │   ├── openrouter.py
│   │   └── parser.py
│   ├── feeds/
│   │   ├── akshare_feed.py
│   │   ├── eastmoney_feed.py
│   │   ├── ths_feed.py
│   │   ├── rss_feed.py
│   │   └── openclaw_feed.py
│   └── kb/
│       ├── repository.py
│       └── retriever.py
├── workflows/
│   ├── collect_daily.py
│   ├── build_research_context.py
│   ├── generate_market_prediction.py
│   ├── run_backtest.py
│   ├── run_reflection.py
│   ├── run_evolution.py
│   └── run_sector_scan.py
├── schemas/
│   ├── snapshot_schema.py
│   ├── prediction_schema.py
│   └── report_schema.py
├── prompts/
│   ├── market_prediction.md
│   ├── portfolio_diagnosis.md
│   ├── daily_reflection.md
│   └── weekly_evolution.md
├── legacy/
│   └── compat/
└── tests/
```


## 5. 关键设计原则

### 5.1 原始层、特征层、结论层三分

每个分析流程都拆成三层数据：

- `raw_*`
  原始抓取结果，不改语义
- `feature_*`
  派生特征与归一化指标
- `insight_*`
  可供模型和人消费的结论

例如每日研究包应拆成：

- `raw_market`
- `raw_news`
- `raw_portfolio`
- `feature_market_regime`
- `feature_sector_heat`
- `feature_portfolio_exposure`
- `insight_daily_research`

### 5.2 研究包替代大快照

不要继续依赖一个巨大 `daily_close` JSON 包。

建议引入显式的 Research Packet：

- `market_daily_packet`
- `sector_rotation_packet`
- `portfolio_state_packet`
- `macro_context_packet`
- `prediction_context_packet`

每个 packet：

- 有固定 schema
- 有版本号
- 可独立重建
- 可单独入库

### 5.3 模型接口统一

新增统一 `LLMClient`：

- 输入：
  - prompt template
  - structured context
  - model config
- 输出：
  - raw response
  - parsed object
  - token/meta
  - error state

这样后续才能做：

- 多模型 ensemble
- 输出对比
- 回放评估
- 成本统计

### 5.4 交易上下文降级为“画像输入”

QMT 相关功能要保留，但角色要调整：

- 不再让研究流程直接绑定 QMT 返回字段
- 统一转换为 `PortfolioState`
- 研究层只消费：
  - 仓位
  - 行业暴露
  - 盈亏分布
  - 待成交风险
  - 现金占比

### 5.5 Prompt 模板化

当前 prompt 在 Python 中硬编码太重。

应拆为：

- 模板文件
- 上下文构造器
- 输出 schema

Python 只负责：

- 选择模板
- 注入结构化上下文
- 调用模型
- 校验输出


## 6. 重构后的功能版图

### 6.1 Market Research

替代今天的 `collect + 部分 predictor 拼接`。

功能：

- 市场收盘研究包生成
- 全球市场联动摘要
- 宏观/大宗商品影响摘要
- 新闻主题提取
- 市场状态识别
- 次日需关注事件清单

输出：

- `DailyResearchReport`

### 6.2 Portfolio Intelligence

替代今天的 `QMT 摘要 + sector diagnosis` 的拼接逻辑。

功能：

- 仓位快照
- 持仓盈亏分布
- 行业/主题暴露
- 热门板块重叠度
- 挂单与成交摘要
- 风险暴露摘要

输出：

- `PortfolioState`
- `PortfolioDiagnosisReport`

### 6.3 Prediction Engine

替代当前 `predictor.py` 的一体化实现。

功能：

- 指数预测
- 板块预测
- 个股观察池预测
- 可选的持仓标的预测
- 单模型 / 多模型预测
- 输出结构校验

输出：

- `PredictionBatch`

### 6.4 Evaluation Engine

替代当前 `reflection.py` 中混合逻辑。

功能：

- 预测对账
- 方向得分
- 幅度得分
- 置信度校准
- 误差归因
- 分层统计：
  - 按标的
  - 按策略
  - 按行情状态
  - 按数据来源

输出：

- `PredictionEvaluation`
- `DailyEvaluationReport`

### 6.5 Learning Engine

替代当前 `evolution.py` 的半自动规则更新。

功能：

- 失败模式聚类
- 规则提炼
- few-shot 样本晋升/淘汰
- system prompt 更新
- 策略权重再平衡

输出：

- `LearningReport`
- `StrategyAdjustment`
- `RuleChangeSet`

### 6.6 Live Monitor

新增一条独立于预测闭环的”实盘监控”能力，用于持续观察策略运行状态，而不是只看预测结果。

功能：

- 监控 qmt2http 通道健康度
- 监控双交易账户
  - 国金全功能账户：`http://39.105.48.176:8085`
  - 东莞交易专用账户：`http://150.158.31.115:8085`
- 监控策略进程心跳与 phase
- 监控 observability 指标与执行质量
- 监控日志中的异常、重试、超时、堆栈
- 监控当日候选、最终选股、买入提交、买入成交、买入跳过原因
- 对比本地策略日志与 qmt2http 账户/成交读口，做交易对账
- **交易风险监控（2026-04-25 新增）**：
  - 仓位集中度（单票 >20% 总资产告警，>30% 紧急告警）
  - 行业集中度（单板块 >40% 持仓市值告警）
  - 日内回撤（>3% 告警，>5% 紧急告警）
  - 持仓异动（单票 ±5% 以上日内波动告警）
  - 执行质量（成交率 <50% 告警，过滤原因单类占比 >50% 告警）
- 在识别到代码级问题时，生成 Codex 自动修复任务

输出：

- `LiveMonitorSnapshot`
- `LiveIncident`（含风险类 incident：`position_concentration` / `sector_concentration` / `intraday_drawdown` / `stock_adverse_move` / `low_fill_rate` / `dominant_skip_reason`）
- `CodexFixTask`


## 7. 数据模型重设计

建议保留 SQLite，但重构表结构。

### 7.1 保留并重构的表

- `predictions`
  替代 `prediction_log`
- `prediction_evaluations`
  从预测表拆出评估结果
- `research_packets`
  替代松散的 `market_snapshots`
- `portfolio_snapshots`
  专门存账户画像
- `strategy_configs`
  保留历史版本
- `rules`
  保留，但增强版本字段和来源字段
- `few_shot_examples`
  保留，但增加 lifecycle 状态
- `reports`
  统一 daily/weekly/monthly/sector/portfolio

当前第一版已在代码中落地，采用“双写兼容”策略：

- 旧表仍保留：
  - `market_snapshots(daily_close/intraday)`
- 新表开始写入：
  - `research_packets`
    - `market`
    - `macro`
    - `sector_rotation`
    - `prediction_context`
  - `portfolio_snapshots`
    - `combined`
- 当前先改写入路径，不强制一次性切换旧读路径

### 7.2 应新增的表

- `data_sources`
  记录数据源状态、成功率、延迟
- `workflow_runs`
  每次 collect/predict/reflect/evolve 的运行记录
- `llm_runs`
  模型调用记录
- `prediction_targets`
  预测标的清单与类型
- `feature_store`
  可选，用于缓存关键派生特征
- `live_monitor_snapshots`
  记录每次实盘监控采样结果
- `live_incidents`
  记录检测出的运行故障、严重度、归因、处理状态
- `codex_fix_runs`
  记录每次自动修复任务、涉及文件、验证结果、回滚状态
- `intraday_risk_snapshots`（2026-04-25 新增）
  记录日内风险快照：总资产、持仓市值、现金率、最大单票集中度、行业集中度、日内最大回撤
- `strategy_performance_metrics`（2026-04-25 新增）
  记录滚动策略绩效：Sharpe比率、最大回撤、胜率、盈亏比、交易次数、平均收益、波动率


## 8. 实盘监控设计

### 8.1 监控范围

实盘监控覆盖三层：

1. 通道层
   - qmt2http `/health`
   - 双账户 `asset / positions / orders / trades / trade/records`
   - 账户、持仓、委托、成交接口是否可用
   - 响应延迟、超时、401/500、空响应
2. 引擎层
   - `/root/qmttrader/logs/runtime/*.json`
   - `logs/runtime/<mode>/*_observability_YYYYMMDD.json`
   - 进程状态、心跳更新时间、当前 phase、status、detail
3. 策略层
   - `/root/qmttrader/logs/`
   - `system.log` / `trade.log` / 各策略日志
   - 异常栈、重复报错、持续重连、订单失败、数据不足、策略代码报错
   - 候选池、最终选股、买入提交、成交确认、买入过滤原因

### 8.2 qmt2http 与日志的实际边界

最新 `/root/qmt2http` 代码已经明确提供日志接口：

- `GET /api/trade/log`

该接口当前能力包括：

- 未传 `path` 时，默认优先按 `日志根目录/YYYYMMDD/system.log` 定位
- 支持 `date`
  - `YYYY-MM-DD`
  - `YYYYMMDD`
- 支持 `lines`
- 支持 `encoding`
- 支持 `include_content`
- `path` 指向目录时，返回按日期匹配到的日志文件列表
- `path` 指向文件时，直接返回文件 tail 内容

当前日志路径解析规则：

- 默认优先使用：
  - `QMT_TRADE_LOG_PATH`
  - `QMT_LOG_PATH`
  - `QMT_LOG_ROOT`
  - `QMT_USERDATA_PATH/log`
- 默认允许读取：
  - `QMT_TRADE_LOG_PATH`
  - `QMT_LOG_PATH`
  - `QMT_LOG_ROOT`
  - `QMT_LOG_ROOTS`
  - `QMT_USERDATA_PATH`
  - `qmt2http` 项目目录
- 如需放开限制，可设置 `QMT_ALLOW_ANY_LOG_PATH=1`

因此监控设计从“两段式降级方案”调整为“三路信号并行”：

- 第一优先级：通过 qmt2http 获取远端运行面信号
- 第二优先级：通过 qmt2http 的 `/api/trade/log` 获取远端日志 tail
- 第三优先级：直接读取 `/root/qmttrader` 本地日志和 runtime status

这意味着 `investor` 的 Live Monitor 可以优先走 HTTP 方式完成远程日志观测，仅在本机部署场景或需要补充上下文时才直接读取本地日志文件。

### 8.3 新增监控子系统

建议新增目录：

```text
investor/
├── live_monitor/
│   ├── collectors/
│   │   ├── qmt_health_collector.py
│   │   ├── qmt_trading_state_collector.py
│   │   ├── trade_decision_collector.py
│   │   ├── runtime_status_collector.py
│   │   ├── observability_collector.py
│   │   └── strategy_log_collector.py
│   ├── analyzers/
│   │   ├── heartbeat_analyzer.py
│   │   ├── log_error_analyzer.py
│   │   ├── qmt_trade_state_analyzer.py
│   │   ├── trading_state_analyzer.py
│   │   ├── trade_decision_analyzer.py
│   │   ├── runtime_phase_analyzer.py
│   │   └── root_cause_router.py
│   ├── remediation/
│   │   ├── codex_fix_runner.py
│   │   ├── patch_validator.py
│   │   └── escalation_policy.py
│   └── workflow.py
```

### 8.4 采集信号

`qmt2http` 采集：

- `/health`
- 账户资产
- 持仓
- 今日委托
- 今日成交
- `/api/trade/records?record_type=trades`
- `/api/trade/log`
- 必要时补充：
  - `is_trading_time`
  - `get_realtime_data`

日志接口推荐参数：

- 常规轮询：
  - `/api/trade/log?lines=200`
- 指定交易日：
  - `/api/trade/log?date=2026-04-24&lines=200`
- 仅获取文件列表不取内容：
  - `/api/trade/log?date=2026-04-24&include_content=false`

本地文件采集：

- 统一本地日志根目录：`/root/qmttrader/logs`
- `/root/qmttrader/logs/runtime/*_status.json`
- `/root/qmttrader/logs/runtime/**/*_observability_*.json`
- `/root/qmttrader/logs/**/*.log`
- `/root/qmttrader/watchlists/*.json`

推荐把策略、runtime status、observability、error/system/trade 日志统一收敛到 `qmttrader/logs/` 下，避免监控侧维护多套日志发现规则。

Codex 修复作用域：

- `/root/qmttrader/strategies/**`
- `/root/qmttrader/core/**`
- `/root/qmttrader/adapters/**`
- `/root/qmttrader/config/**`

### 8.5 监控判定规则

建议至少实现以下判定：

#### A. 心跳异常

- runtime `updated_at` 超过阈值未更新
- `status = error`
- phase 长时间停留在 `loading_strategy` / `starting_engine`

#### B. 通道异常

- `/health` 失败
- qmt2http 接口连续超时
- 交易接口返回认证错误
- 账户/持仓/委托连续空返回且不符合交易时段特征

#### C. 策略异常

- 日志出现 `Traceback`
- 日志出现 `ERROR` / `CRITICAL` / `系统运行错误`
- 同一错误短时间重复出现
- 下单失败率异常高
- 反复重连但无法恢复

#### D. 业务异常

- 长时间无心跳但 qmt2http 健康，说明更可能是策略进程挂了
- qmt2http `/api/trade/log` 能读到新日志，但 runtime status 不更新，说明主流程可能卡死在未写心跳的路径
- qmt2http `/health` 正常但 `/api/trade/log` 持续无新内容，说明策略可能未真正启动或 supervisor 未拉起
- 有信号计数但无订单尝试，说明可能信号到执行链断裂
- 订单尝试高但成功率低，说明执行通道或价格逻辑有问题
- 持仓/委托/成交与 observability 指标严重不一致
- 最新交易决策日志日期明显陈旧，说明当日策略决策链可能未运行
- 本地日志出现买入成交，但可达账户的持仓/成交读口中查不到，说明需要对账

### 8.6 故障分级

- `P0`
  - 实盘策略进程退出
  - qmt2http 完全不可用
  - 连续订单执行失败且影响交易
- `P1`
  - 策略持续报错但进程未退出
  - 心跳停滞
  - 订单/成交异常偏离
- `P2`
  - 单次异常、偶发超时、非关键数据缺失
- `P3`
  - 低优先级提示，如 token 成本高、信号利用率低

### 8.7 Codex 自动修复闭环

当故障被判定为“代码/配置问题”时，触发 Codex 修复流程：

1. 收集证据
   - 最近 N 行错误日志
   - runtime status JSON
   - observability JSON
   - 相关配置片段
   - 涉及代码路径
2. 生成 `CodexFixTask`
   - 问题摘要
   - 严重度
   - 可疑模块
   - 最小修复范围
3. 调用 Codex
   - 只允许修改 `/root/qmttrader` 内相关文件
   - 默认限制在策略、adapter、core、config
4. 自动验证
   - 语法检查
   - 目标测试
   - 最小 smoke test
5. 记录结果
   - 修复 diff
   - 验证结果
   - 是否需要人工确认

### 8.8 Codex 触发边界

以下情况可以自动调用 Codex：

- import 错误
- 配置键缺失
- 明确的 Python 异常
- 可重复的 qmt2http 适配错误
- runtime status/日志指向明确代码路径

以下情况不应自动直接修代码，而应升级人工处理：

- 实盘成交异常但没有明确代码错误
- 风险控制逻辑与策略意图冲突
- 下单价格/仓位参数需人工判断
- qmt2http 服务端自身故障
- Windows 系统级故障、网络断连、QMT 客户端崩溃

### 8.9 与 qmttrader 的集成方式

建议把监控设计成“旁路 watcher”，不要直接嵌进策略主线程。

集成方式：

- `investor` 侧定时任务每 1-5 分钟跑一次 monitor workflow
- 仅读取：
  - qmt2http HTTP 接口
  - `/root/qmttrader/logs/runtime`
  - `/root/qmttrader/logs`
- 不改动交易进程主循环

必要时对 `qmttrader` 做最小补充：

- 在关键错误日志中输出结构化 tag
- 扩展 runtime status 的 `phase/detail/extra`
- 补充 observability 指标

### 8.10 推荐的最小可落地版本

第一版只做：

1. qmt2http health + 账户/持仓/委托/成交采样
2. qmt2http `/api/trade/log` 日志 tail 采样
3. runtime status 文件采样
4. 本地 `watchlists`、`system.log` 中的候选/最终选股/买入提交/成交提取
5. 本地策略日志与 qmt2http 读口的交易对账
6. 最近日志尾部错误提取
7. 规则化故障判定
8. 生成 incident 报告
9. 对明确代码错误生成 Codex 修复任务

不要第一版就做：

- 自动重启交易程序
- 自动重新下单
- 自动改风控参数
- 自动回滚持仓相关决策


## 9. 飞书交互策略（2026-04-25 新增）

飞书是 Investor 与用户之间的核心交互通道。它不仅是"查询接口"，更应成为**全天候交易助手的对话界面**。

### 9.1 三层交互模型

```
用户 → 飞书Bot → feishu-bridge → 意图分类
                                      ├── investor (实盘数据查询)
                                      ├── llm (分析/建议/预测讨论)
                                      └── hybrid (数据 + 分析)
                                              ↓
                                    飞书消息回复
```

### 9.2 当前已具备的能力

| 能力 | 实现 | 触发方式 |
|------|------|---------|
| 实盘数据查询 | `feishu_query_service.py` | 自然语言："国金今天持仓" |
| 双账户支持 | 自动识别国金/东莞/双账户 | "东莞成交" "双账户持仓" |
| 意图分类 | `feishu_bridge_service.py` | investor / llm / hybrid 三路路由 |
| 定时简报 | `scheduled_briefings.py` | 09:45 东莞策略 / 13:20 国金ETF / 14:20 国金ETF |
| Hybrid 模式 | investor 数据 + LLM 分析 | "帮我分析一下今天持仓" |

### 9.3 关键增强方向

#### 9.3.1 被动查询 → 主动推送

当前 Feishu 以**被动响应查询**为主。交易助手需要在关键时刻**主动推送**：

```
推送触发条件：
├── P0 告警（立即推送）
│   ├── 单票仓位集中度 >30%
│   ├── 日内回撤 >5%
│   ├── 策略进程退出
│   └── qmt2http 完全不可用
├── P1 告警（5分钟内推送）
│   ├── 持仓异动 ±5%
│   ├── 对账 mismatch
│   └── 心跳停滞
├── 定时简报（按时间表推送）
│   ├── 09:45 开盘简报
│   ├── 11:30 午盘简报
│   ├── 14:20 尾盘简报
│   └── 15:30 收盘简报（含当日反思）
└── 交易执行通知
    ├── 买入成交通知
    ├── 卖出成交通知
    └── 撤单/废单通知
```

**实现策略**：在 `live_monitor/workflow.py` 的每次轮询后增加 `push_alerts_to_feishu()` 钩子，按告警级别和订阅策略推送。

#### 9.3.2 固定查询 → 智能问答

当前 `feishu-query` 只能回答"持仓/委托/成交/日志"四类结构化查询。交易助手应能回答：

```
新增问答场景：
├── 预测相关
│   ├── "今天预测结果如何？" → 读取最新 prediction_log
│   ├── "最近一周胜率？" → 读取 checked predictions
│   └── "哪些预测最不准？" → 失败模式分析
├── 反思相关
│   ├── "今日交易摘要" → today-summary --text
│   ├── "持仓盈亏排名" → 持仓P&L表
│   └── "帮我复盘今日交易" → daily_reflection
├── 风险相关
│   ├── "当前风险敞口？" → 仓位/行业集中度
│   ├── "有没有异常持仓？" → risk incidents
│   └── "日内最大回撤多少？" → intraday_risk
├── 策略相关
│   ├── "策略表现如何？" → strategy_performance_metrics
│   ├── "当前权重分布？" → strategy_config
│   └── "最近规则变化？" → rules 变更记录
└── 操作指令
    ├── "跑一次实盘监控" → trigger monitor
    ├── "生成今日反思" → trigger daily_reflection
    └── "查看最新候选" → trigger today-candidates
```

**实现策略**：扩展 `_normalize_intent()` 的分词表，增加 `feishu_query_service.py` 的分析类查询路径，复用 `analysis_context_summary` 统一上下文。

#### 9.3.3 Hybrid 模式增强

当前 hybrid 模式返回 `investor_reply` + `llm_task`，但飞书 bot 端通常只消费第一个 `reply` 字段。建议：

```
Option A（推荐）：investor 侧完成分析再回复
  feishu-bridge 先调用 feishu-query 获取数据
  → 调用 LLM 基于数据做分析
  → 返回完整分析结果作为 reply
  → llm_task 作为备用（供支持多阶段的客户端使用）

Option B：分步回复
  第一条：实盘数据摘要（即刻返回）
  第二条：LLM 分析结果（异步推送）
```

#### 9.3.4 飞书卡片消息

当前所有回复都是纯文本（`\n`.join(lines)）。飞书支持富交互卡片：

```json
{
  "msg_type": "interactive",
  "card": {
    "header": {"title": {"content": "🦞 交易简报 2026-04-25"}},
    "elements": [
      {"tag": "div", "text": {"content": "账户状态与持仓盈亏"}},
      {"tag": "hr"},
      {"tag": "div", "text": {"content": "持仓6只 | 浮盈+2,345 | 成交4笔"}},
      {"tag": "action": {"actions": [
        {"tag": "button", "text": {"content": "查看持仓"}, "value": "cmd:positions"},
        {"tag": "button", "text": {"content": "查看成交"}, "value": "cmd:trades"},
        {"tag": "button", "text": {"content": "运行监控"}, "value": "cmd:monitor"}
      ]}}
    ]
  }
}
```

**实现策略**：新增 `feishu_card_builder.py`，为简报和告警构建卡片模板。卡片按钮用于触发后续操作（轮询/查询/执行命令）。

#### 9.3.5 订阅与免打扰

告警推送需要有节制，避免消息轰炸：

```
订阅策略：
├── P0 告警：始终推送（不可关闭）
├── P1 告警：默认推送，可配置静默时段
├── P2 告警：每日汇总推送一次
├── 定时简报：按配置时段推送（09:30-15:00）
└── 交易执行通知：每笔成交即时推送
```

铁粉/高级用户可配置：`/feishu-config alerts P0,P1` 或 `/feishu-config mute 12:00-13:00`。

### 9.4 推荐的 Feishu 命令体系

```
# 查询类
/持仓 [国金|东莞]            — 查看持仓
/成交 [国金|东莞]            — 查看今日成交
/账户                        — 双账户概览
/摘要 [日期]                 — 今日交易简报
/候选 [日期]                 — 今日候选与选股
/预测                        — 最新预测结果
/胜率 [7|30]                 — 近N天预测胜率
/风险                        — 当前风险敞口

# 操作类
/监控                        — 执行一次实盘监控
/反思                        — 生成今日反思报告
/简报 [0945|1320|1420]       — 指定时段简报
/进化                        — 执行一次策略进化

# 分析类
/分析 <标的>                 — 对标做全面分析
/复盘 [日期]                 — 复盘当日交易
/建议                        — 基于当前状态给出交易建议

# 配置类
/告警设置 [P0|P0,P1|全部]   — 设置告警推送级别
/静默 [HH:MM-HH:MM]         — 设置免打扰时段
/帮助                        — 显示所有命令
```

### 9.5 技术架构

```
investor/
├── app/
│   └── feishu_card_builder.py          # 🆕 飞书卡片消息构建
├── domain/
│   └── services/
│       ├── feishu_query_service.py      # 扩展：增加分析/建议类查询
│       ├── feishu_bridge_service.py     # 扩展：hybrid 模式增强
│       ├── feishu_alert_service.py      # 🆕 告警推送编排
│       └── feishu_subscription_service.py # 🆕 用户订阅与免打扰
└── workflows/
    └── push_alerts_to_feishu.py         # 🆕 告警推送到飞书 workflow
```

### 9.6 分阶段落地

| 阶段 | 内容 | 优先级 |
|------|------|--------|
| Phase 1 | 扩展 feishu-query 支持预测/反思/风险查询 | P0 |
| Phase 1 | feishu-bridge hybrid 模式增强（investor 侧完成分析） | P0 |
| Phase 2 | 告警推送到飞书（P0/P1 实时推送） | P1 |
| Phase 2 | 飞书卡片消息模板 | P1 |
| Phase 3 | 订阅管理 + 免打扰 | P2 |
| Phase 3 | 交互式按钮命令执行 | P2 |

---

## 10. 新的运行流程

### 10.1 每日流程

1. `collect_daily`
   拉原始数据并持久化
2. `build_research_context`
   生成研究包和特征
3. `build_portfolio_state`
   生成账户画像
4. `generate_predictions`
   基于研究包与账户画像产出预测
5. `evaluate_predictions`
   次日回测与评分
6. `run_reflection`
   生成归因与改进建议

### 10.2 盘中流程

1. 刷新轻量市场数据
2. 刷新持仓状态
3. 运行板块异动检测
4. 输出盘中提醒

### 10.3 周期流程

1. 汇总一周/一月评估结果
2. 重新评估规则有效性
3. 调整策略权重
4. 更新 prompt / few-shot


## 11. 建议删除或弱化的旧设计

以下设计不建议继续强化：

- 继续向 `daily_close` 塞更多字段
- 在 Python 代码里继续拼超长 prompt
- 在 `main.py` 继续加命令分支
- 直接让业务逻辑读 QMT 原始字段
- 把研究、交易、反思共用一份大上下文字符串


## 12. 分阶段迁移方案

### Phase 1: 稳定边界

- 新建 `app / domain / infrastructure / workflows` 目录
- 保留旧文件，但只做兼容层
- 把 CLI 从 `main.py` 拆出去
- 把 LLM 调用从 `predictor.py` 拆出去

### Phase 2: 拆数据模型

- 引入 `research_packets`
- 引入 `portfolio_snapshots`
- 把评估结果从 `prediction_log` 拆分
- 给 snapshot 增加 schema version

### Phase 3: 拆业务服务

- 新建：
  - `PredictionService`
  - `ReflectionService`
  - `EvolutionService`
  - `PortfolioDiagnosisService`
- 旧模块调用新服务

### Phase 4: 升级预测引擎

- 支持板块/观察池/持仓标的预测
- 支持多模型投票
- 支持统一输出校验

### Phase 5: 做研究平台化

- 把 investor 从“自动脚本”升级成“研究中台”
- 对外提供：
  - CLI
  - Agent bridge
  - 定时任务
  - Feishu 报告

### Phase 6: 加入实盘监控与自动修复

- 新增 `live_monitor` 子系统
- 打通 qmt2http 健康检查与本地日志采样
- 建立 incident 表和修复任务表
- 对 `/root/qmttrader` 开启受控的 Codex 自动修复
- 先从只读分析 + 人工确认补丁开始，再逐步放宽


## 13. 当前实现状态

截至当前版本，以下能力已经在代码中落地：

- `monitor`
  - 运行侧监控：qmt2http `/health`、`/api/trade/log`、runtime status、observability、策略日志
  - 交易侧监控：双账户 `asset / positions / orders / trades / trade/records`
  - 交易决策提取：候选池、最终选股、买入提交、买入成交、买入跳过原因
  - 交易对账：本地日志 vs qmt2http 双账户读口
- `fix-task` 工作流
  - `fix-tasks`
  - `fix-task-show`
  - `fix-task-context`
  - `fix-task-pack`
  - `fix-task-bundle`
  - `fix-task-run-validation`
  - `fix-task-validation-groups`
  - `fix-task-promote`
- 交易专用命令
  - `monitor-trading`
  - `today-candidates`
  - `today-buys`
  - `today-account`
  - `today-summary`
  - `today-summary <date> --text`
  - `fix-task-export <id> <path>`

当前监控默认按双账户执行：

- `guojin` -> `http://39.105.48.176:8085`
- `dongguan` -> `http://150.158.31.115:8085`

当前已实现的交易类 incident 包括：

- `trade_log_unavailable`
- `qmt_trade_state_unavailable`
- `trade_decision_stale`
- `trade_reconciliation_mismatch`

其中 `trade_reconciliation_mismatch` 只会在“至少一个账户可达且本地成交在 qmt 侧查不到”时触发；如果两个账户都不可达，则仅返回 `qmt_unreachable` 状态，不误报 mismatch。

当前交易监控还新增了两类实用输出：

- 日期参数
  - `today-*` 和 `monitor-trading` 已支持 `YYYY-MM-DD` / `YYYYMMDD`
- 账户归属判断
  - `trade_reconciliation.server_matches`
  - `trade_reconciliation.filled_account_candidates`
  - 用于判断本地成交更可能归属于 `guojin`、`dongguan`、`ambiguous`、`missing` 或 `unreachable`

### 2026-04-25 增强（已并入本设计文档）

本轮增强聚焦于三个方面：数据质量修复、交易风险监控、持仓级预测扩展。

**数据质量修复：**
- `adjust_strategy_weights()` 增加权重无变化时跳过写入的守卫，消灭 weight_history 空转重复
- `update_rules_from_failures()` 引入规则签名去重机制（`_normalize_rule_signature`），同策略/同标的规则只更新不新建
- `build_trading_summary_report()` 增加多 key 兼容解析（`_resolve_positions/_resolve_trades/_resolve_accounts`），确保从不同来源读取持仓数据

**交易风险监控（新增）：**
- `live_monitor/analyzers/risk_analyzer.py` — 五类风险检测：
  - `position_concentration` — 单票集中度（>20% P1，>30% P0）
  - `sector_concentration` — 行业集中度（>40% P1）
  - `intraday_drawdown` — 日内回撤（>3% P1，>5% P0）
  - `stock_adverse_move` — 持仓异动（±5% P1）
  - `low_fill_rate` / `dominant_skip_reason` — 执行质量
- 已集成到 `run_live_monitor()`，输出增加 `risk_incident_count` / `risk_incidents`

**持仓级预测扩展：**
- `prediction_service.py` 新增 `get_position_prediction_targets()` — 从 portfolio_snapshot 动态加载持仓标的
- `_get_strategy_distribution()` — 按当前权重分配策略标签，避免全部标为 technical
- `prediction_orchestrator.py` — `generate_predictions()` 默认包含持仓标的预测

**反思报告增强：**
- `daily_reflection()` 增加 `_build_prediction_breakdown_table()` — 按标的/策略分组统计预测偏差
- 持仓盈亏表增加盈利/亏损分布统计

**数据库新增：**
- `intraday_risk_snapshots` — 日内风险快照
- `strategy_performance_metrics` — 策略绩效指标（Sharpe/回撤/盈亏比）


## 14. 这一轮重构我建议保留的资产

- `qmt_client.py` 的双服务器思路
- `sector_scanner.py` 的美股到 A 股映射知识
- `reflection.py` 的评分逻辑
- `evolution.py` 的策略权重调整思想
- `knowledge_base.py` 的本地知识库方案
- 现有 SQLite 数据库中的历史预测与反思数据
- `/root/qmttrader/utils/runtime_status.py` 的 JSON 心跳机制
- `/root/qmttrader/logs/runtime/**/*_observability_*.json` 这类可观测性快照


## 15. 结论

当前 `investor` 模块已经具备：

- 多源采集
- 双 QMT 聚合
- 指数预测
- 自动回测
- 规则进化
- 板块轮动分析
- 本地知识库
- 可扩展到实盘运行监控与代码级自动修复

但它的结构还是“可运行原型”，不是“可持续演进的系统”。

下一步不应该继续在现有文件上堆逻辑，而应该按“数据接入 / 研究分析 / 决策预测 / 学习进化 / 接口编排”五层拆开，先重建边界，再迁移功能。

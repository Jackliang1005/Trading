# Investor 改造计划

## 目标

基于 [INVESTOR_REDESIGN.md](/root/.openclaw/workspace/investor/INVESTOR_REDESIGN.md:1)，将当前 `investor` 从脚本堆叠式原型，逐步改造成可持续演进的研究、监控与自动修复系统。

本计划强调：

- 先稳边界，再迁移逻辑
- 先加兼容层，再替换旧入口
- 先做只读监控，再做自动修复
- 每一步都能回退


## 总体策略

改造分 4 条并行主线推进：

1. 应用骨架重建
   - 建立新目录结构和新入口
2. 业务能力迁移
   - 将 collect / predict / reflect / evolve 从旧文件迁出
3. 数据与模型层治理
   - 引入 packet、repository、统一 LLM 接口
4. 实盘监控与 Codex 修复
   - 接 qmt2http 日志接口与 qmttrader runtime status

其中优先级顺序为：

1. 应用骨架
2. Live Monitor 最小版本
3. Prediction 拆分
4. 数据层重构
5. Reflection / Evolution 迁移


## 当前进度

截至当前版本，Phase 1、Phase 2、Phase 3 已经不是“计划中”，而是“已落地基础版本”。

已完成：

- 新骨架与兼容层
  - `app/`
  - `domain/`
  - `infrastructure/`
  - `workflows/`
  - `legacy/compat/`
- Live Monitor 基础能力
  - qmt2http `/health`
  - qmt2http `/api/trade/log`
  - qmt2http 双账户 `asset / positions / orders / trades / trade/records`
  - runtime status / observability / 本地日志采集
  - incident 入库
- 交易监控能力
  - 候选池
  - 最终选股
  - 买入提交
  - 买入成交
  - 买入过滤原因
  - watchlists（从 `system.log` 解析保存/加载/缺失状态）
  - 本地日志与 qmt2http 双账户对账
  - 双账户归属增强：
    - `final_candidates / submitted_buys / filled_buys` 统一做账户候选归属
    - `account_trade_matrix` 输出每账户候选/提交/成交覆盖
    - `monitor-trading / today-account / today-summary --text` 已接入聚合汇总
- Agent 分析入口
  - `main.analyze` 输出统一 `analysis_context_summary`
  - `investor_agent context` 复用 `app/agent_bridge.py` 统一上下文构建
  - `main.analyze / investor_agent context` 已增加交易决策摘要（log_date/strategy/候选/提交/成交/watchlists）
- Codex 修复任务流
  - task 生成
  - task 状态流转
  - validation 记录
  - pack / bundle 输出
  - `patched / closed` 状态推进门槛（`closed` 需 `patched` 且最近两组 validation 全通过）

当前已可用命令：

- `monitor`
- `monitor-trading`
- `today-candidates`
- `today-buys`
- `today-account`
- `today-summary`
- `fix-tasks`
- `fix-task-show`
- `fix-task-context`
- `fix-task-pack`
- `fix-task-bundle`
- `fix-task-run-validation`
- `fix-task-validation-groups`
- `fix-task-promote`

近期主线修复补充（2026-04-25）：

- qmt2http `trade_only` 已切换为“仅屏蔽行情/数据接口”，交易读口（asset/positions/orders/trades/records）可用
- `live_monitor/collectors/qmt_trade_state_collector.py` 已对齐新策略，不再误跳过交易读口
- `workflows/run_smoke_checks.py` 已补 `__main__` 入口与失败退出码，避免“空跑成功”
- `qmt_client.QMTManager` 默认双账户（MAIN+TRADE），支持 `QMT2HTTP_DISABLE_TRADE=1` 强制单账户
- 已新增 `domain/services/assistant_service.py`，承接 `analyze / dashboard / record_prediction / record_feedback`，`main.py`、`investor_agent.py`、`app/agent_bridge.py` 改为 facade 调用

新增主线收敛：

- `live_monitor/workflow.py` 的存储侧已迁移到 `domain/repository.py::LiveMonitorRepository`
  - `ensure_tables`
  - `save_snapshot`
  - `save_incidents`
  - `save_codex_fix_tasks`
  - 目标是减少 workflow 对 `db.get_conn` 的直接耦合
- `domain/services/live_monitor_service.py` 已落地
  - collector + analyzer + repository 的核心编排从 `workflow.py` 下沉
  - `workflow.py::run_live_monitor` 变为薄代理，保留原命令输出结构
- `domain/services/live_monitor_view_service.py` 已落地
  - `run_trading_monitor / today-* / today-summary --text` 视图编排下沉
  - `app/cli.py` 已改为直接依赖 service 层
  - `live_monitor/workflow.py` 目前仅保留兼容转发函数

近期增强（2026-04-25 第二轮）：

- **策略进化修复**：
  - `adjust_strategy_weights()` 增加权重无变化守卫，消灭 weight_history 空转
  - `update_rules_from_failures()` 引入规则签名去重（`_normalize_rule_signature`），同策略规则只更新不新建
  - 数据库已清理：weight_history 20→7条，rules 去重5条，system_prompt rules 16+→11条
- **交易风险监控**：
  - 新增 `live_monitor/analyzers/risk_analyzer.py`，覆盖仓位集中度/行业集中度/日内回撤/个股异动/执行质量五类风险
  - 已集成到 `run_live_monitor()`，输出增加 `risk_incident_count` / `risk_incidents`
- **持仓级预测**：
  - `prediction_service.py` 新增 `get_position_prediction_targets()` 和 `_get_strategy_distribution()`
  - `prediction_orchestrator.py` 默认对持仓标的生成预测，策略标签按权重分配
- **反思报告增强**：
  - `daily_reflection()` 增加按标的/策略分组的预测偏差分析表和盈亏分布统计
- **数据库**：新增 `intraday_risk_snapshots` 和 `strategy_performance_metrics` 两张表
- **设计文档**：已将关键评审结论内联到 `INVESTOR_REDESIGN.md`，并持续按运行数据迭代

近期增强（2026-04-25 第三轮 — 飞书 Webhook 接入）：

- **统一 Feishu Webhook 服务**：
  - `feishu_webhook_server.py` 重写为统一 webhook，监听端口 8788 `/feishu/trading`
  - 替换原 trading 系统的 `feishu_trading_webhook.py`（已停用）
  - 同时处理 investor 投资查询 和 trading 交易指令（向后兼容）
  - 通过 `openclaw message send` 回复飞书，与 trading 通知走同一通道
- **关键决策**：不走 openclaw AI agent，webhook 直连处理。原因：agent 在飞书上下文中只做 `[[reply_to_current]]` 文本回复，不执行 bash 命令
- **部署**：systemd 服务 `feishu-webhook`，开机自启
- **已验证命令**：`/持仓` `/预测` `/风险` `/策略` 全部返回正确结果
- **文档**：`HANDOFF.md`、`docs/feishu_plugin_interface.md` 已更新


## Phase 0: 基线冻结

### 目标

在改造前固定当前可运行基线，防止重构中失去参照。

### 任务

- 记录当前 CLI 命令及输出行为
- 记录当前数据库表结构
- 记录当前关键文件职责
- 记录当前依赖：
  - `investor`
  - `/root/qmttrader`
  - `/root/qmt2http`
- 补一个最小 smoke 清单

### 产物

- `docs/current_baseline.md`
- `tests/smoke/` 目录

### 验收标准

- 能列出当前所有命令及其输入输出
- 能跑最小 smoke：
  - import 主要模块
  - 读取最新 snapshot
  - 生成 dashboard


## Phase 1: 应用骨架与兼容层

### 目标

建立新的工程骨架，但不改变旧功能行为。

### 任务

- 新建目录：
  - `app/`
  - `domain/`
  - `infrastructure/`
  - `workflows/`
  - `live_monitor/`
  - `legacy/compat/`
- 新建新入口：
  - `app/cli.py`
  - `app/agent_bridge.py`
- 旧 `main.py` 先不删除，只改成转发层
- 将旧函数通过兼容层映射到新 workflow

### 产物

- 新目录结构
- `app/cli.py`
- `legacy/compat/main_compat.py`

### 验收标准

- `python3 main.py dashboard` 仍可运行
- 新 CLI 可以调用旧逻辑
- 没有破坏现有数据库和日志路径


## Phase 2: Live Monitor 最小可用版

### 目标

先把实盘监控做成可用产品，再碰复杂业务迁移。

### 范围

只做只读监控和 incident 生成，不做自动改单、自动重启。

### 任务

- 实现 collectors：
  - `qmt_health_collector.py`
  - `qmt_trade_log_collector.py`
  - `runtime_status_collector.py`
  - `observability_collector.py`
- 实现 analyzers：
  - `heartbeat_analyzer.py`
  - `log_error_analyzer.py`
  - `runtime_phase_analyzer.py`
  - `root_cause_router.py`
- 实现 workflow：
  - `live_monitor/workflow.py`
- 新增表：
  - `live_monitor_snapshots`
  - `live_incidents`
- 接入 `/root/qmt2http`：
  - `/health`
  - `/api/trade/log`
- 接入 `/root/qmttrader`：
  - 统一日志根目录 `/root/qmttrader/logs`
  - `logs/runtime/*.json`
  - `logs/runtime/**/*_observability_*.json`

### 产物

- `live_monitor/` 最小实现
- `main.py` 或新 CLI 中的 `monitor` 命令

### 验收标准

- 能输出一份完整监控快照
- 能识别至少 4 类故障：
  - 心跳停滞
  - qmt2http 不健康
  - 日志 Traceback
  - runtime status = error
- 能把 incident 写入库

### 当前状态

该阶段已完成基础实现，并已扩展到交易监控：

- 默认双账户：
  - `guojin`
  - `dongguan`
- 已实现的交易类规则：
  - `trade_log_unavailable`
  - `qmt_trade_state_unavailable`
  - `trade_decision_stale`
  - `trade_reconciliation_mismatch`
- 已实现的交易专用视图：
  - `monitor-trading`
  - `today-candidates`
  - `today-buys`
  - `today-account`
  - `today-summary`
  - `today-summary <date> --text`
- 已实现的修复任务导出：
  - `fix-task-export <id> <path>`
- 已实现的交易补充能力：
  - `today-*` 日期参数
  - 双账户 `filled_account_candidates`
  - 双账户 `server_matches`


## Phase 3: Codex 修复任务编排

### 目标

把“监控发现问题”升级成“生成可执行修复任务”。

### 任务

- 新增：
  - `live_monitor/remediation/codex_fix_runner.py`
  - `live_monitor/remediation/patch_validator.py`
  - `live_monitor/remediation/escalation_policy.py`
- 新增表：
  - `codex_fix_runs`
- 定义 `CodexFixTask` 结构
- 限定修复作用域：
  - `/root/qmttrader/strategies/**`
  - `/root/qmttrader/core/**`
  - `/root/qmttrader/adapters/**`
  - `/root/qmttrader/config/**`
- 默认策略：
  - 只生成修复任务和补丁建议
  - 默认人工确认后再应用

### 产物

- 可落库的修复任务
- 对应验证报告

### 验收标准

- 遇到明确 Python 异常时，能生成修复任务
- 任务中包含：
  - 错误摘要
  - 证据
  - 可疑文件
  - 验证命令
- 不会对交易行为直接做自动动作

### 当前状态

该阶段已完成第一版落地：

- `codex_fix_runs`
- `codex_fix_validation_runs`
- 状态：
  - `open`
  - `acknowledged`
  - `patched`
  - `closed`
- 已实现命令：
  - `fix-tasks`
  - `fix-task-show`
  - `fix-task-context`
  - `fix-task-pack`
  - `fix-task-bundle`
  - `fix-task-run-validation`
  - `fix-task-validation-groups`
  - `fix-task-promote`

已完成的原则：

- 只对代码级错误生成 Codex 修复任务
- 交易通道不可达、账户读口失败、交易对账失败默认只告警，不自动改交易逻辑


## Phase 4: Prediction 模块拆分

### 目标

把 `predictor.py` 的超大职责拆开。

### 任务

- 抽离：
  - prompt builder
  - model client
  - output parser
  - prediction repository
- 新建：
  - `domain/services/prediction_service.py`
  - `infrastructure/llm/client.py`
  - `infrastructure/llm/deepseek.py`
  - `infrastructure/llm/openrouter.py`
  - `infrastructure/llm/parser.py`
  - `prompts/market_prediction.md`
- 旧 `predictor.py` 只保留兼容入口

### 产物

- 新的 prediction service
- 模板化 prompt
- 统一模型调用接口

### 验收标准

- 预测结果字段与旧系统兼容
- 支持 DeepSeek / OpenRouter / rule-based fallback
- 新旧入口都能完成预测

### 当前进度

第一批拆分已落地（兼容旧入口）：

- `domain/services/prediction_service.py`
  - `load_prediction_snapshot_data()` 从 `predictor.py` 下沉
  - packet 优先 + `daily_close` fallback 行为保持不变
  - `build_prediction_runtime_context()` 下沉了 snapshot/rag/few-shot/system-prompt 组装
  - `build_rule_based_predictions()` 下沉了无 LLM 回退策略
  - `save_predictions()` 下沉了预测落库逻辑
- `domain/services/prediction_prompt_service.py`
  - `build_prediction_context / build_market_context_text / build_prediction_prompt` 从 `predictor.py` 下沉
  - `predictor.py` 现仅保留兼容导出与主流程编排
- `domain/services/prediction_orchestrator.py`
  - `generate_predictions / call_llm_for_prediction / parse_predictions` 主流程下沉
  - `predictor.py` 调整为 compatibility facade（保留旧导入路径）
  - `main.py` 已改为直接依赖 prediction service/orchestrator，而非 `predictor.py` facade
  - `app/cli.py::record` 与 `record_prediction.py` 已切到 orchestrator+service（解析/落库不再重复实现）
- `infrastructure/llm/`
  - `client.py` 统一 provider 选择与 key 发现
  - `deepseek.py` / `openrouter.py` HTTP 适配
  - `parser.py` 统一预测 JSON 解析
- `predictor.py`
  - 保留原函数名与主流程
  - 内部改为调用新 service / llm infrastructure
  - 网络失败时仍自动回退 rule-based 预测


## Phase 5: 数据层与 packet 化

### 目标

把“大 JSON snapshot”改造成显式 packet 体系。

### 任务

- 引入：
  - `research_packets`
  - `portfolio_snapshots`
  - `prediction_evaluations`
- 定义 schema/version

### 当前建议

由于交易监控、任务流和基础文本输出已经成形，下一阶段应优先做：

1. qmt2http 可达时的双账户归属精化验证
2. `today-summary --text` 的更细粒度分时/盘后模板
3. `research_packets` 与 `portfolio_snapshots` 数据层重构
- 将 `daily_close` 拆成：
  - market packet
  - macro packet
  - portfolio packet
  - sector rotation packet
  - prediction context packet
- 增加 repository 层

### 当前进度

第一版数据层已落地：

- 新表：
  - `research_packets`
  - `portfolio_snapshots`
- 新接口：
  - `save_research_packet`
  - `get_latest_research_packet`
  - `save_portfolio_snapshot`
  - `get_latest_portfolio_snapshot`
  - `save_daily_close_packets`
- 当前 `collect_daily_data()` 已在保留旧 `market_snapshots(daily_close)` 的同时，同步写入：
  - `market`
  - `macro`
  - `sector_rotation`
  - `prediction_context`
  - `portfolio`
- `collect_intraday_data()` 已同步写入 `market` packet

### 产物

- 新 schema
- 新 repository
- snapshot 迁移脚本

### 验收标准

- 新 packet 可独立读取
- 新 packet 能拼出预测上下文
- 旧 snapshot 仍可回读


## Phase 6: Reflection / Evolution 迁移

### 目标

将评估与学习逻辑从脚本拆成服务。

### 任务

- 新建：
  - `domain/services/reflection_service.py`
  - `domain/services/evolution_service.py`
  - `domain/policies/scoring_policy.py`
  - `domain/policies/confidence_policy.py`
- 将评分逻辑、归因逻辑、规则演化逻辑下沉
- 拆出 report formatter

### 产物

- 反思服务
- 进化服务
- 独立 scoring policy

### 验收标准

- 评分结果与旧逻辑一致或差异可解释
- weekly / monthly report 仍可生成

### 当前进度

第一批入口迁移已落地（行为不变）：

- `domain/services/reflection_service.py`
  - `daily_reflection / weekly_attribution / monthly_audit / backtest_predictions` façade
- `domain/services/evolution_service.py`
  - 初版已接入，后续升级为主编排 service
- `main.py` 与 `investor_agent.py` 已改为优先依赖上述 service，而非直接 import 大脚本模块
- policy 下沉已推进：
  - `domain/policies/scoring_policy.py` 已接入 `reflection.calculate_prediction_score`（保留兼容函数名）
  - `domain/policies/confidence_policy.py` 已落地，并接入 `evolution.update_rules_from_failures` 的规则置信度更新与自动禁用判断
- `domain/services/reflection_analysis_service.py` 已落地：
  - `analyze_failure_patterns / format_weekly_report` 从 `reflection.py` 下沉
  - `reflection.py` 保留兼容函数并转发到 service
- `domain/services/reflection_service.py` 不再是纯 façade：
  - `weekly_attribution / monthly_audit` 主编排已迁入 service
  - `main.py` 与 `reflection.py` 的周/月触发路径已收敛到 service 层
- `domain/services/evolution_service.py` 不再是纯 façade：
  - `evolve` 主编排已迁入 service（权重/规则/few-shot/prompt 串联）
  - `load_strategy_config / adjust_strategy_weights / update_rules_from_failures / update_few_shot_examples / generate_system_prompt` 核心实现已迁入 service
- `evolution.py` 已瘦身为兼容转发层（CLI 行为保持）
- `analysis_context_summary` 已在 `monitor-trading / today-summary / today-account` 统一做字段归一化（固定默认键 + 空值策略）
- `analysis_context_summary` 统一契约已扩展到 `main.analyze / app.agent_bridge / investor_agent context`
- `reflection.py` 周/月主逻辑已移除重复实现，`weekly_attribution / monthly_audit` 现为兼容转发到 `domain/services/reflection_service.py`
- `reflection.py` 的 `backtest_predictions` 已迁到 `domain/services/reflection_runtime_service.py`，旧文件保留兼容入口
- `domain/services/reflection_service.py::backtest_predictions` 已直接走 runtime service，不再反向依赖旧脚本
- `reflection.py` 的 `load_reflection_context / get_reflection_context_summary / build_trading_summary_report / daily_reflection` 已迁到 `domain/services/reflection_runtime_service.py`，旧文件保留兼容入口
- `domain/services/reflection_service.py::daily_reflection` 已直接走 runtime service
- packet 迁移脚本已落地：
  - [workflows/backfill_packets.py](/root/.openclaw/workspace/investor/workflows/backfill_packets.py:1)
  - 支持 `--dry-run(default) / --apply / --type / --limit / --force`
  - 新 CLI 命令：`python3 main.py backfill-packets ...`
- Phase 5 运行文档已补齐：
  - [docs/packet_backfill_runbook.md](/root/.openclaw/workspace/investor/docs/packet_backfill_runbook.md:1)
  - [docs/current_baseline.md](/root/.openclaw/workspace/investor/docs/current_baseline.md:1)


## 主线未完成清单（2026-04-25）

开发主线已完成到“可运行收敛”状态，当前剩余以运行维护为主：

1. 运行侧增量维护
   - 按日执行 packet 增量回填并更新 `HANDOFF.md` 快照（已提供 `packet-maintain` 命令与自动快照文件）
2. Monitor 细化（可选）
   - 分账户过滤原因归档与盘中时序统计（已完成首版，后续可继续丰富事件类型）
3. Feishu 通道治理
   - 已移除旧 OpenAPI/Webhook 直连接口，统一 `feishu-query` plugin 入口
   - 后续仅维护 plugin 调用协议，不再新增直连飞书代码


## Phase 7: 清理旧代码

### 目标

在新入口稳定后，压缩旧代码为兼容层。

### 任务

- 清理旧 `main.py` 逻辑
- 清理旧 `predictor.py`、`reflection.py`、`evolution.py` 中已迁移部分
- 将旧模块改为 facade
- 补文档与迁移说明

### 产物

- 精简后的 legacy compat 层
- 更新后的开发文档

### 验收标准

- 新入口成为默认入口
- 旧入口仍能调用核心能力
- 没有重复实现的主逻辑


## 第一阶段立即执行项

建议现在就开始以下 6 个动作：

1. 新建目录骨架与兼容入口
2. 新增 `monitor` 命令
3. 实现 qmt2http `/health` collector
4. 实现 qmt2http `/api/trade/log` collector
5. 实现 runtime status collector
6. 实现最小 incident 生成

这 6 步完成后，就已经从“设计文档”进入“可运行的新架构”阶段。


## 风险控制

### 不做的事

当前阶段不做：

- 自动下单
- 自动撤单
- 自动重启交易主程序
- 自动调整风控参数
- 自动修改持仓相关逻辑

### 高风险变更要求

以下改动必须带验证：

- `/root/qmttrader/core/execution/**`
- `/root/qmttrader/core/risk/**`
- `/root/qmttrader/strategies/**`
- `/root/qmttrader/adapters/qmt2http.py`


## 成功标准

改造成功的最低标准不是“代码更漂亮”，而是：

- `investor` 有新骨架，旧功能不崩
- 能持续监控 qmt2http + qmttrader
- 能识别并记录真实 incident
- 能对明确代码错误生成 Codex 修复任务
- collect / predict / reflect / evolve 逐步迁出旧脚本

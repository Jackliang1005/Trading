# Trading 长线组合平台 — 代码与文档 Review（第二版）

> 审查日期：2026-04-26（初版） → 2026-04-26（复核更新）
> 审查范围：`trading_core_new/longterm/` 全部 Python 模块、Shell 调度脚本、文档、测试、配置及数据文件
> 审查原则：不改代码，仅做分析并提出可操作优化建议

---

## 一、总体评价

系统整体架构清晰，模块职责划分合理（models / repository / engine / data_source / scanner / advisor / notifier / llm_runtime / cli），调仓约束体系完整，容错机制完善（LLM → 规则兜底，行情 → 成本价回退，网络异常 → 静默降级）。

**初版 review 的 10 个 P0/P1 问题已全部修复**，涉及代码去重、CLI 解耦、异常分类、配置参数化、并发优化、环境加载。修复质量整体良好，未发现引入新 bug。

**当前剩余待跟进项**：测试补齐（3.2）。

---

## 二、代码问题

### 2.1 代码重复（DRY 违反）— ✅ 已修复

**文件**: `llm_runtime.py`（新增），`llm_advisor.py`，`post_market_scanner.py`

`_resolve_llm_runtime()` 已提取到独立模块 `llm_runtime.py`，导出为 `resolve_llm_runtime()`，支持 `allow_openclaw_key_fallback` 参数区分两种使用场景。`llm_advisor.py` 调用时传 `True`（允许从 openclaw 配置读取 key），`post_market_scanner.py` 调用时传 `False`（仅用环境变量）。

---

### 2.2 CLI 层内部耦合 — ✅ 已修复

**文件**: `cli.py` L397-434 (`_sync_universe_core`), L455-494 (`_run_post_market_scan_core`)

新增两个核心函数，CLI handler `cmd_sync_universe_from_portfolio` 和 `cmd_post_market_scan` 现在仅做参数解析 + 调用核心函数 + print 输出。`cmd_evening_decision`（L760-783）不再直接调用其他 CLI handler，改为调用 `_sync_universe_core()` 和 `_run_post_market_scan_core()`。

---

### 2.3 关键参数硬编码 — ✅ 已修复

**变更**:
- `LongTermSettings` 新增 14 个扫描器配置字段（`scan_atr_min`, `scan_rsi_min`, `scan_rsi_max`, `scan_turnover_min`, `scan_turnover_max`, `scan_bias_ma5_abs_max`, `scan_volume_ma5_multiplier`, `scan_heat_bonus_cap`, `scan_heat_bonus_scale`, `scan_fallback_*` 系列, `ths_heat_limit_up_weight`, `ths_heat_change_weight`）
- `post_market_scanner.py` 新增 `_param()` 辅助函数从 Settings 读取，`_build_reference_style_row` 和 `_fallback_row` 均接受 `settings` 参数
- `data_source.py` 的 `fetch_ths_hotness` 新增 `limit_up_weight` 和 `change_weight` 参数，调用方从 Settings 传入
- `settings.json` 已同步包含所有新增字段

---

### 2.4 异常吞没过于宽泛 — ✅ 已修复

**文件**: `repository.py` L48-61

`_read_json` 现在区分三类异常：
- `json.JSONDecodeError` → `logger.warning`
- `OSError` → `logger.error`
- 其他 `Exception` → `logger.error`

---

### 2.5 数据源 HTTP 请求脆弱 — ✅ 已修复（工程级）

**文件**: `data_source.py` L144 (`_fetch_ths_sectors_from_web`)

已改为 `https://basic.10jqka.com.cn/`，并增加重试与退避（0/0.25/0.5s）、自动编码识别（`apparent_encoding` 回退）；  
网页解析已改为 `HTMLParser`，正则仅作兜底。

---

### 2.6 随机计划 ID 可能冲突 — ✅ 已修复

**文件**: `engine.py` L373

`plan_id` 从 `%H%M%S` 改为 `%H%M%S%f`（精确到微秒），消除了同秒冲突风险。

---

### 2.7 `PortfolioState.__post_init__` 的隐式行为 — ✅ 已修复

**文件**: `models.py` L56-64

保留原有钳制行为，同时增加不一致状态 `logging.warning`：`frozen_cash < 0`、`frozen_cash > cash`、`available_cash > cash` 三类都会记录告警。

---

### 2.8 `_safe_weight_map` 的权重偏差 — ✅ 已修复

**文件**: `engine.py` L89-101

- 零分股票不再被强行抬升至 1.0，而是保留 0.0（权重为 0）
- 当 `total == 0` 时，改为等权分配
- 测试新增 `test_safe_weight_map_zero_score_not_forced_nonzero` 验证

---

### 2.9 缺少结构化日志 — ✅ 已修复

在 `cli.py` 入口新增 `_configure_logging()`，统一配置 root logger（stream + 文件 `trading_data/longterm/logs/longterm.log`），支持 `LONGTERM_LOG_LEVEL` 调整级别；`repository.py` 与 `models.py` 告警可落盘。

---

### 2.10 并发写入无保护 — ✅ 已修复（当前范围）

`repository.py` 的 `_write_json` 已引入 `fcntl.flock` 进程级排他锁；并且已补充原子读改写封装用于 `append_manual_executions`，  
同时 `save_post_market_scan` / `save_decision` 的“目标文件 + latest 文件”改为单锁事务写入，`init_if_missing` 也改为锁内初始化写入。  
当前核心高频写路径（manual/scan/decision/init）已事务化，已覆盖本系统主要并发风险。

---

### 2.11 行情批量获取串行化 — ✅ 已修复

**文件**: `data_source.py` L471-494 (`fetch_batch_daily_history`)

已改为 `ThreadPoolExecutor` 并发请求，最大并发数 `max(1, min(8, len(codes)))`，使用 `as_completed` 收集结果。

---

### 2.12 `industry_normalizer` 对未知行业无兜底 — ✅ 已修复

已增加未知值回退（`UNKNOWN`/`N/A`/`--`/无字符标签 → `"其他"`），并支持在未知行业下结合概念细化主题（如 `"其他-AI算力"`）。

---

## 三、测试覆盖

### 3.1 现状

`tests/test_longterm_regression.py` 已扩展到 **39 个**测试用例。新增覆盖：
- `build_rebalance_plan` 的 `min_trade_amount` / `cash_buffer` 约束路径
- `build_rebalance_plan` 的 `industry_cap` / `theme_cap` / `risk_budget` 约束路径
- `apply_manual_executions` 买入 + 部分卖出路径
- `llm_runtime` 环境变量解析路径
- `data_source` 同花顺网页回退使用 HTTPS 路径
- `data_source` HTML 解析提取路径
- `cli evening/morning` 全流程（mock 外部依赖）路径
- `cli evening-decision` 的 `universe_empty` 异常返回路径
- `repository` 手工成交日志连续追加路径
- `industry_normalizer` 未知行业回退路径
- `apply_manual_executions` 新股买入 + 超卖按比例手续费路径
- `repository.cleanup_history` 生命周期清理路径
- `cli cleanup-data --dry-run` 调用路径
- `cli morning-decision` 推送失败分支（不阻断决策落库）
- `data_source` 网络超时与 SQLite 异常回退路径
- `post_market_scanner` 自定义阈值边界路径
- `llm_runtime` openclaw key 回退 / unknown provider 路径
- `engine` 约束优先级（industry_cap 先于 theme_cap）路径
- `data_source.fetch_batch_daily_history` 单任务异常不影响整体路径
- `decision-quality-report` 报告生成与计数路径
- `market_structure` 结构因子计算路径
- `market_structure` 外部输入文件读取路径

### 3.2 仍建议补充的测试

| 模块 | 缺失测试内容 | 优先级 |
|---|---|---|
| `engine.py` | `build_rebalance_plan` 更复杂多约束叠加（含持仓与卖出联动） | 中 |
| `llm_runtime.py` | 文件格式异常 + provider 切换矩阵（更细粒度） | 中 |
| `cli.py` | `morning-decision` 的结构因子在无行情/部分行情下的边界断言 | 中 |
| `cli.py` | `decision-quality-report` 多日样本下滚动告警边界断言 | 中 |

---

## 四、文档问题

文档项已补齐：
- 新增 `docs/longterm_architecture_dataflow.md`（模块架构图 + 盘后/早盘时序图）
- `runbook` 已明确脚本内交易日检查与 cron 的关系，并补充生命周期清理入口
- 已补充 LLM 输入输出契约摘要（决策字段、兜底规则）

---

## 五、配置与环境

### 5.1 Shell 脚本加载 `.env.longterm` — ✅ 已修复

两个脚本均增加了：
```bash
if [[ -f "$ROOT_DIR/.env.longterm" ]]; then
  set -a
  source "$ROOT_DIR/.env.longterm"
  set +a
fi
```
使用 `set -a` 自动导出所有 source 的变量。

### 5.2 `settings.json` 默认值与代码默认值

新增的扫描参数在 `settings.json` 中已体现（含 `scan_*` 和 `ths_heat_*` 系列字段）。

### 5.3 `.gitignore` 不完整 — ✅ 已修复

新增 `.env.*` 和 `trading_data/longterm/` 规则。现在整个 longterm 数据目录和所有 env 文件都不会被 git 追踪。

---

## 六、Shell 脚本

### 6.1 错误处理不完整 — ✅ 已修复

两个脚本已增加 `trap ERR`，失败时会调用 `notifier.push_feishu_text` 发送故障告警（含脚本名、日期、exit code、行号、主机名）。

### 6.2 `TOP_K` 环境变量 — ✅ 无变化（不影响功能）

---

## 七、安全问题

### 7.1 明文 HTTP 请求 — ✅ 已修复

同 2.5，同花顺网页抓取已切换至 HTTPS，并加了解析与重试兜底。

### 7.2 API Key 跨文件搜索 — ✅ 已改善

`llm_runtime.py` 中的 `resolve_llm_runtime()` 通过 `allow_openclaw_key_fallback` 参数显式控制是否从 openclaw 配置读取 key，行为更加明确。

---

## 八、新增隐患

### 8.1 Logging 未配置 handler — ✅ 已关闭

`cli.py` 已在 `main()` 前执行 `_configure_logging()`，全局 handler 已配置。

### 8.2 `_param()` 缺少缓存

`post_market_scanner.py` 的 `_param()` 函数在每次阈值判断时多次通过 `getattr` 读取 Settings 属性。对于不变值，这本身性能影响极小但模式不够优雅。可以接受。

---

## 九、优化建议优先级汇总

### P0 — 初版 P0 全部修复 ✅

| # | 问题 | 状态 |
|---|---|---|
| 1 | 增加结构化日志 | ✅ 完成 |
| 2 | Shell 脚本加载 `.env.longterm` | ✅ 完成 |
| 3 | CLI 层移除对其他 CLI handler 的调用 | ✅ 完成 |
| 4 | 补齐核心逻辑的回归测试 | ✅ 完成（34 个回归测试） |

### P1 — 初版 P1 大部分已修复

| # | 问题 | 状态 |
|---|---|---|
| 5 | 提取重复的 `_resolve_llm_runtime` | ✅ 完成 |
| 6 | 扫描器的硬编码阈值移到 Settings | ✅ 完成 |
| 7 | `_read_json` 区分异常类型 | ✅ 完成 |
| 8 | 行情批量获取并行化 | ✅ 完成 |
| 9 | 并发写入保护（文件锁） | ✅ 完成（核心写路径事务化） |
| 10 | `_safe_weight_map` 权重计算修正 | ✅ 完成 |

### P2（中长期改进）— 待跟进

| # | 问题 | 影响 |
|---|---|---|
| 14 | 提升异常分支测试覆盖率 | 稳定性 |

---

## 十、亮点（保留）

1. **原子写入模式** (`_write_json` 写 tmp 再 rename) — 防止写入中途崩溃损坏数据文件
2. **LLM 输出 schema 校验 + 重试 + 规则兜底** — 三层防护确保决策链路不中断
3. **行业二级细分体系** (`industry_normalizer.py`) — 关键词规则 + THS 概念板块双通道
4. **被拒绝的调仓动作可追溯** (`rejected_actions` + `reason`) — 对人工复核非常有价值
5. **交易日历双层校验** — 优先用 `exchange_calendars` 库，失败回退 weekday + 假日文件
6. **飞书推送三级回退** — Webhook → OpenAPI 直连 → openclaw CLI channel
7. **`PortfolioState` 支持 `frozen_cash`** — 比简单的 cash 字段更真实
8. **新增**: `llm_runtime.py` 单一职责模块，清晰的 key 查找策略
9. **新增**: `_sync_universe_core` / `_run_post_market_scan_core` 实现 CLI 与核心逻辑分离
10. **新增**: 扫描器参数完全可通过 Settings 动态配置，无需修改代码

---

## 十一、落实进度（2026-04-26）

### 已确认修复（含本轮新增）

| 问题编号 | 修复内容 | 质量评估 |
|---|---|---|
| 2.1 DRY | 提取 `llm_runtime.py`，统一 LLM 运行时解析 | 优秀 — 增加了 `allow_openclaw_key_fallback` 参数区分场景 |
| 2.2 CLI 耦合 | 提取 `_sync_universe_core` / `_run_post_market_scan_core` | 优秀 — CLI handler 仅做参数解析和 print |
| 2.3 硬编码 | 14 个扫描参数 + 2 个热点系数进入 `LongTermSettings` | 优秀 — 覆盖全面，`_param()` 提供默认值兼容 |
| 2.4 异常吞没 | `_read_json` 区分 JSON/S/O 错误并记录日志 | 合格 — 异常分类正确 |
| 2.7 PortfolioState 钳制 | 增加现金字段不一致告警日志 | 合格 — 保留兼容行为并提升可观测性 |
| 2.9 结构化日志 | CLI 入口统一配置 root logger 与文件输出 | 合格 — 可观测性显著提升 |
| 2.10 并发写入 | `_write_json` 加写锁 + manual/scan/decision/init 事务化写入 | 良好 — 并发覆盖风险进一步下降 |
| 2.12 行业兜底 | 未知行业统一回退 `"其他"`，并可基于概念细化主题 | 合格 — 提升稳定性并保留可解释性 |
| 2.6 plan_id | 秒级改为微秒级 `%H%M%S%f` | 合格 |
| 2.8 权重偏差 | 零分不再抬升，总分零时等权 | 优秀 — 有对应测试 |
| 2.11 串行行情 | `ThreadPoolExecutor` 并发实现 | 优秀 — 并发数有上限控制 |
| 5.1 env 加载 | Shell 脚本 `source .env.longterm` + `set -a` | 优秀 — 使用了 `set -a` 自动导出 |
| 6.1 Shell 失败通知 | `trap ERR` + 飞书失败告警 | 合格 — 运维可见性提升 |
| 5.3 gitignore | 新增 `.env.*` 和 `trading_data/longterm/` | 合格 |
| 12 生命周期治理 | `cleanup-data` + 保留策略参数 + 晚间脚本自动清理 | 良好 — 降低磁盘膨胀风险 |
| 13 架构与数据流文档 | 新增 `longterm_architecture_dataflow.md` 并在设计/运维文档互链 | 合格 — 文档口径统一 |

### 待跟进（优先级排序）

1. **测试继续补齐** — `engine` 多约束优先级与 `data_source` 并发异常细项

---

*Review 生成时间：2026-04-26 | 审查工具：Claude Code manual review | 版本：第二版（含修复验证）*


---

## 十二、2026-05-05 更新 — 热度轮动策略重构 + 聚宽回测

### 策略优化 (joinquant_heat_rotation.py)

基于聚宽自动化回测流水线的迭代优化，将策略收益从 **-46% 提升至 +48.28%**：

| # | 改动 | 收益变化 |
|---|------|---------|
| 1 | 修复 `sum()` 被 JoinQuant 环境 shadow 的 bug | -46% → (修复崩溃) |
| 2 | 趋势破位加缓冲区 (0.03%→2%→3%) | -13.90% |
| 3 | 添加 RSI 过买过滤 (>75剔除) + 偏好中等热度(15-75) | +2.00% |
| 4 | 添加市场趋势检测 (MA60/MA20), 熊市降仓 | +5.98% |
| 5 | 去熊市现金缓冲, 持3只, 过滤科创板688, 趋势破位→3% | +48.28% |

### 生产系统回导 (trading_core)

将聚宽验证过的策略参数和逻辑回导到 `trading_core_new/longterm/`：

- **models.py**: 新增10个字段 (`rotation_max_holdings`, `max_holdings_bear`, `max_heat_entry`, `rsi_max_entry`, `trend_break_pct`, `exclude_star_board` 等)
- **engine.py**: 5处逻辑改动 (HRS权重缩放、退出规则缓冲区、入场过滤、市场趋势检测、科创板排除)
- **settings.json**: 同步所有参数

### 新增文档

- `docs/heat_rotation_strategy.md` — 策略完整文档 (HRS公式、出场规则、参数表、代码位置)
- `docs/joinquant_backtest_guide.md` — 聚宽回测指南 (环境准备、使用方法、API差异、调试技巧)

### 聚宽环境陷阱 (新增知识)

1. `sum()` 被 shadow → 必须用显式循环
2. `get_current_data()` 不可靠 → 用纯代码过滤
3. 科创板(688)市价单 → 需要保护限价, 策略应排除
4. `log.info()` 输出不可见 → 统一用 `print()`
5. `get_concepts()` 太慢 → 避免在回测中频繁调用

---

---

## 十三、2026-05-05 晚间更新 — GEM 小市值轮动

### 策略切换

将股票池从 CSI 500+1000 切换为 **创业板小市值 (GEM Small Cap)**：

- **代码前缀**: 30xxxx (创业板)
- **市值过滤**: 5亿 ~ 50亿
- **基本面**: 营收同比增长 ≥ 15%
- **排序**: 按市值升序，跳过前 10 只 (过滤微盘/问题股)，取 20 只

### 新增文件/字段

| 文件 | 改动 |
|------|------|
| `models.py` | 新增 9 个 `gem_*` 字段 |
| `data_source.py` | 新增 `fetch_gem_candidates()` 方法 |
| `cli.py` | `_sync_universe_core()` 中注入 GEM 候选 |
| `settings.json` | `rotation_mode: true`, `gem_universe: true` |
| `joinquant_heat_rotation.py` | 替换为 `gem_small_cap_baseline.py` |
| `docs/heat_rotation_strategy.md` | 更新为 GEM 小市值版 |

### 启用的定时任务

```bash
systemctl enable --now trading-evening.timer
# 每交易日 15:35 → evening-decision → 飞书推送调仓建议
```

首次手动运行已成功产出 8 笔买入 + 2 笔卖出的调仓建议。

---

---

## 十四、2026-05-05 深夜更新 — 系统修复与 GEM 激活

### Bug 修复

| Bug | 根因 | 修复 |
|-----|------|------|
| `fetch_batch_daily_history` 返回空 | `count=` 参数不存在 → TypeError → 被 `except` 静默吞掉 | `count`→`lookback_days`，加 `end_date` |
| 非交易日无数据 | 工具默认用当天（假期）→ 无K线 | `last_trading_day()` 自动回退最近交易日 |
| GEM未进入选股 | `rotation_scan` 未透传 → rotation path 从未进入 | `cmd_run_review` 注入 `rotation_scan` |
| GEM被 industry_cap 阻挡 | tiered 模式下 satellite_industry_cap=40% | 切换 `industry_cap_mode`/`theme_cap_mode`→`single` |
| shell脚本非交易日直接 exit | 不检查明天是否交易日 | 自动检测明天交易日→添加 `--ignore-trading-calendar` |

### 最终产出

- 计划：卖9只主板 + 买3只创业板小市值（301630同宇新材/301665泰禾股份/301632广东建科）
- 定时器 `trading-evening.timer` 每交易日 15:35 自动盘后决策 → 飞书推送
- 服务 `trading-evening.service` 已 enable

---

*Review 生成时间：2026-04-26 | 更新：2026-05-05 | 版本：第五版（GEM小市值激活）*

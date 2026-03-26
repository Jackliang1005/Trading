# 🦞 OpenClaw Investor — Handoff 手册

> 下次启动优化前，先读此文件，快速恢复上下文。

## 系统定位

自学习 A股投资分析系统，核心循环：**Collect → Predict → Reflect → Evolve**。
自动采集多源数据、LLM 生成预测、回测打分、策略权重/规则/Few-shot 自动进化。

---

## 模块地图

```
main.py              CLI 入口，所有命令调度
data_collector.py    数据采集（QMT/AKShare/东财/RSS/全球指数/大宗商品/宏观新闻）
predictor.py         LLM 预测生成（DeepSeek/OpenRouter，含规则回退）
reflection.py        回测打分 + 每日/周/月反思报告
evolution.py         策略权重调整 + 规则库更新 + Few-shot 管理 + system prompt 生成
knowledge_base.py    FTS5 全文检索 RAG + Few-shot 构建
sector_scanner.py    板块轮动扫描 + 美股→A股映射 + 持仓诊断
db.py                SQLite 数据层（prediction_log/strategy/rules/few_shot/snapshots/kb）
investor_agent.py    OpenClaw Agent 桥接接口
cron_setup.py        定时任务配置生成
```

## CLI 命令

```
python3 main.py <command>

init          初始化 DB + 知识库
collect       采集每日数据（07:30）
predict       生成预测（09:30）
reflect       回测反思（20:30）
evolve        周进化（周日 21:00）
audit         月度审计（每月1日 22:00）
dashboard     状态看板
prompt        查看当前 system prompt
backtest      手动回测
sector-scan   板块扫描 + 美股映射 + 持仓诊断
record        手动录入预测
```

---

## 数据流

```
07:30  collect ─→ market_snapshots(daily_close) ─→ auto_memorize → kb
09:30  predict ─→ 读 snapshot + RAG + few-shot + system_prompt → LLM → prediction_log
随时    sector-scan ─→ 同花顺热门板块 + 美股板块ETF + 龙头个股 → 轮动预判 → market_snapshots(sector_scan)
20:30  reflect ─→ 回测 prediction_log → 打分 → reflection_reports
周日    evolve  ─→ 调权重 + 提规则 + 管 few-shot → strategy_config.json + system_prompt.md
```

## 数据源清单

| 来源 | 用途 | 接口 |
|------|------|------|
| QMT2HTTP | 实时行情/账户/持仓 | HTTP RPC |
| AKShare | A股日线/全球指数/大宗商品/宏观新闻/美股日线 | Python |
| 东方财富 | 行情/资金流向/板块资金/新闻 | skills 复用 |
| 同花顺 | 涨停板块 + 龙头股 | HTTP API |
| RSS | 财联社/新浪/东财新闻 | feedparser |
| DeepSeek / OpenRouter | LLM 预测 | HTTP API |

## 关键环境变量

```
QMT2HTTP_BASE_URL     QMT 服务地址（默认 http://150.158.31.115）
QMT2HTTP_API_TOKEN    QMT 认证 token
DEEPSEEK_API_KEY      DeepSeek API key
OPENROUTER_API_KEY    OpenRouter API key（回退）
```

---

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

QMT 持仓 × 热门板块交叉 → 🟢核心股 / 🟡关联股 / 🔴非热门

### predictor.py 集成

`build_prediction_prompt()` 从 sector_scan snapshot 提取 `us_sectors`、`us_stocks`、`rotation_prediction`，
注入 LLM prompt 的"隔夜美股板块表现"+"美股龙头个股"+"美股→A股板块轮动预判"三个段落。

---

## 已知限制 & 优化方向

### 当前限制
- 预测仅覆盖 3 大指数（上证/深证/创业板），不含个股
- 美股数据来自新浪（`ak.stock_us_daily`），偶尔延迟或缺失
- 同花顺涨停板块 API 非交易时段可能返回空
- QMT 连接依赖外部服务器在线
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

# 验证模块加载
python3 -c "import sector_scanner; print('OK')"
python3 -c "import predictor; print('OK')"

# 验证美股数据
python3 -c "from sector_scanner import fetch_us_sector_performance; print(fetch_us_sector_performance())"

# 完整板块扫描
python3 main.py sector-scan

# 状态看板
python3 main.py dashboard

# 查看当前 system prompt
python3 main.py prompt
```

---

*最后更新: 2026-03-19 — 新增隔夜美股板块+A股轮动预判功能*

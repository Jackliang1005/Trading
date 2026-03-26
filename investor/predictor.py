#!/usr/bin/env python3
"""
Loop 1.5: 预测生成 — 基于采集数据自动生成市场预测
在 collect 之后、reflect 之前运行
"""

import json
import os
import sys
from datetime import datetime
from typing import Dict, List

sys.path.insert(0, os.path.dirname(__file__))

import db
from data_collector import fetch_market_quotes
from knowledge_base import build_rag_context, build_few_shot_prompt
from evolution import generate_system_prompt

# 预测目标：主要指数
PREDICTION_TARGETS = [
    {"code": "sh000001", "name": "上证指数"},
    {"code": "sz399001", "name": "深证成指"},
    {"code": "sz399006", "name": "创业板指"},
]


def build_prediction_prompt(snapshot_data: Dict, rag_context: str,
                            few_shot: str, system_prompt: str) -> str:
    """构建让 LLM 生成预测的 prompt"""
    # 提取行情摘要
    quotes_str = ""
    for q in snapshot_data.get("quotes", []):
        if not q.get("error"):
            quotes_str += f"- {q.get('name', q.get('code', '?'))}: {q.get('price', '?')} (涨跌幅: {q.get('change_percent', '?')}%)\n"

    # 提取资金流向
    flow = snapshot_data.get("flow", {})
    flow_str = json.dumps(flow, ensure_ascii=False)[:500] if flow else "无数据"

    # 提取板块
    sectors = snapshot_data.get("sectors", [])
    sectors_str = ""
    for s in sectors[:10]:
        if isinstance(s, dict):
            sectors_str += f"- {s.get('name', '?')}: 主力净流入 {s.get('main_net', '?')}万\n"

    # 提取新闻
    news_str = ""
    for n in (snapshot_data.get("news_eastmoney", []) + snapshot_data.get("news_rss", []))[:8]:
        if isinstance(n, dict):
            news_str += f"- {n.get('title', '')}\n"
        elif isinstance(n, str):
            news_str += f"- {n}\n"

    # 全球市场行情
    global_str = ""
    for g in snapshot_data.get("global_indices", []):
        if isinstance(g, dict):
            global_str += f"- {g.get('name', '?')}: {g.get('price', '?')} (涨跌幅: {g.get('change_percent', '?')}%)\n"

    # 大宗商品价格
    commodity_str = ""
    for c in snapshot_data.get("commodities", []):
        if isinstance(c, dict):
            commodity_str += f"- {c.get('name', '?')}: {c.get('price', '?')} (涨跌幅: {c.get('change_percent', '?')}%)\n"

    # 宏观/地缘政治新闻
    macro_str = ""
    for m in snapshot_data.get("macro_news", []):
        if isinstance(m, dict):
            macro_str += f"- [{m.get('source', '')}] {m.get('title', '')}\n"

    # 市场状态检测
    regime = snapshot_data.get("market_regime", {})
    regime_str = ""
    if regime:
        regime_str = f"当前市场状态: **{regime.get('regime', 'unknown')}**\n"
        regime_str += f"- {regime.get('details', '')}\n"
        regime_str += f"- 近10日波动率: {regime.get('volatility', 0)}%\n"
        regime_str += f"- 近10日累计涨跌: {regime.get('total_change_10d', 0)}%\n"
        regime_str += f"- 上涨天数/下跌天数: {regime.get('up_days', 0)}/{regime.get('down_days', 0)}\n"

    # QMT 实时行情（更精确的数据源）
    qmt_str = ""
    for q in snapshot_data.get("qmt_quotes", []):
        if isinstance(q, dict) and q.get("price"):
            qmt_str += f"- {q.get('name', q.get('code', '?'))}: {q.get('price', '?')} (涨跌幅: {q.get('change_percent', '?')}%, 成交额: {q.get('amount', '?')})\n"

    # QMT 账户资产
    account_str = ""
    acct = snapshot_data.get("qmt_account", {})
    if acct:
        account_str += f"- 总资产: {acct.get('total_asset', acct.get('m_dBalance', '?'))}\n"
        account_str += f"- 可用资金: {acct.get('cash', acct.get('m_dAvailable', '?'))}\n"
        account_str += f"- 持仓市值: {acct.get('market_value', acct.get('m_dStockValue', '?'))}\n"

    # QMT 持仓
    positions_str = ""
    for p in snapshot_data.get("qmt_positions", []):
        if isinstance(p, dict):
            code = p.get("stock_code", p.get("m_strInstrumentID", "?"))
            name = p.get("stock_name", code)
            vol = p.get("volume", p.get("m_nVolume", 0))
            cost = p.get("open_price", p.get("m_dOpenPrice", 0))
            profit_pct = p.get("profit_rate", 0)
            if isinstance(profit_pct, (int, float)):
                profit_pct = f"{profit_pct:.2f}%"
            positions_str += f"- {name}({code}): 持仓{vol}股, 成本{cost}, 盈亏{profit_pct}\n"

    # 今日热门板块（来自 sector_scan 快照）
    hot_sectors_str = ""
    us_sectors_str = ""
    us_stocks_str = ""
    rotation_str = ""
    try:
        sector_snap = db.get_latest_snapshot("sector_scan")
        if sector_snap:
            snap_data = sector_snap.get("data", {})
            for i, s in enumerate(snap_data.get("hot_sectors", [])[:10], 1):
                leaders = ", ".join(st["name"] for st in s.get("stocks", [])[:3])
                hot_sectors_str += (f"- {i}. {s.get('name', '?')}: "
                                    f"涨停{s.get('limit_up_num', 0)}只, "
                                    f"涨幅{s.get('change', 0):+.2f}%, "
                                    f"龙头: {leaders}\n")
            # 隔夜美股板块
            for s in snap_data.get("us_sectors", []):
                us_sectors_str += (f"- {s.get('name', '?')}({s.get('symbol', '?')}): "
                                   f"涨跌 {s.get('change_pct', 0):+.2f}%\n")
            # 美股龙头个股
            for st in snap_data.get("us_stocks", []):
                a_secs = ", ".join(st.get("a_sectors", [])[:3])
                us_stocks_str += (f"- {st.get('name', '?')}({st.get('symbol', '?')}): "
                                  f"涨跌 {st.get('change_pct', 0):+.2f}%, "
                                  f"A股关联: {a_secs}\n")
            # 轮动预判
            rot = snap_data.get("rotation_prediction", {})
            if rot.get("continuing"):
                rotation_str += "持续强势板块:\n"
                for item in rot["continuing"]:
                    rotation_str += f"  - {item['a_sector']}: {item['reason']}\n"
            if rot.get("bullish"):
                rotation_str += "潜在轮动（看多）:\n"
                for item in rot["bullish"]:
                    rotation_str += f"  - {item['a_sector']}: {item['reason']}\n"
            if rot.get("bearish"):
                rotation_str += "可能承压（看空）:\n"
                for item in rot["bearish"]:
                    rotation_str += f"  - {item['a_sector']}: {item['reason']}\n"
    except Exception:
        pass

    prompt = f"""你是A股投资分析助手。请基于以下最新市场数据，对明日A股主要指数走势做出预测。

## 今日A股市场数据

### 指数行情
{quotes_str or '无数据'}

### 资金流向
{flow_str}

### 板块资金流向（前10）
{sectors_str or '无数据'}

### 今日热门板块（涨停概念，板块轮动参考）
说明：以下为同花顺涨停板块数据，反映当日市场资金主攻方向，对判断板块轮动和短期热点有重要参考意义。
{hot_sectors_str or '无数据'}

### 隔夜美股板块表现（S&P 500 十一大板块 ETF）
说明：美股板块涨跌对次日A股对应板块有直接映射关系，如美股科技板块涨→A股芯片/AI受益。
{us_sectors_str or '无数据'}

### 美股龙头个股表现
说明：美股龙头个股大幅波动会直接影响A股对应产业链板块，如英伟达涨→A股算力/芯片受益。
{us_stocks_str or '无数据'}

### 美股→A股板块轮动预判
说明：基于隔夜美股板块和龙头表现，结合A股当前热门板块，预判次日板块轮动方向。
{rotation_str or '无明显信号'}

### 今日重要新闻
{news_str or '无数据'}

## 全球市场行情
说明：隔夜美股走势对A股次日开盘有重要参考意义，港股与A股联动性强。
{global_str or '无数据'}

## 大宗商品价格
说明：原油价格波动直接影响化工、航空板块及整体市场情绪；黄金走强通常反映避险情绪升温。
{commodity_str or '无数据'}

## 宏观/地缘政治新闻
说明：重点关注地缘冲突（影响原油供应和市场情绪）、央行政策（影响流动性）、贸易摩擦等。
{macro_str or '无数据'}

## 市场状态检测
{regime_str or '无数据'}

## QMT 实时行情（高精度数据源）
{qmt_str or '无数据'}

## 实盘账户概况
说明：以下为本人实盘账户数据，预测时应考虑当前仓位情况，避免在满仓时仍建议加仓。
{account_str or '无数据'}

## 当前持仓
{positions_str or '无持仓'}

{rag_context}

{few_shot}

## 预测要求（严格遵守）

**关键规则：**
1. 如果市场状态为"sideways"（横盘），应优先预测"neutral"，除非有明确的方向性催化剂
2. 如果你预期的涨跌幅在±0.3%以内，必须预测"neutral"，不要强行给出方向
3. 只有在出现明确方向性信号（重大政策变化、突发地缘事件、技术面明确突破/跌破等）时才预测"up"或"down"
4. reasoning中必须提及全球市场和宏观因素的影响
5. 微幅波动（±0.1%以内）不构成方向性判断依据
6. 隔夜美股板块表现对A股对应板块有映射参考意义，reasoning中应结合美股板块轮动预判分析

请对以下每个指数给出明日预测，严格按 JSON 格式输出，不要输出其他内容：

```json
[
  {{
    "code": "sh000001",
    "name": "上证指数",
    "direction": "up/down/neutral",
    "confidence": 0.0-1.0,
    "predicted_change": 0.0,
    "strategy_used": "technical/fundamental/sentiment/geopolitical",
    "reasoning": "简要分析理由，必须包含全球市场和宏观因素（80字内）"
  }},
  {{
    "code": "sz399001",
    "name": "深证成指",
    "direction": "...",
    "confidence": ...,
    "predicted_change": ...,
    "strategy_used": "...",
    "reasoning": "..."
  }},
  {{
    "code": "sz399006",
    "name": "创业板指",
    "direction": "...",
    "confidence": ...,
    "predicted_change": ...,
    "strategy_used": "...",
    "reasoning": "..."
  }}
]
```

direction 只能是 up/down/neutral 之一。confidence 范围 0-1。predicted_change 为预测涨跌幅百分比。strategy_used 可选 technical/fundamental/sentiment/geopolitical。
"""
    return prompt


def call_llm_for_prediction(prompt: str, model: str = "deepseek/deepseek-chat") -> str:
    """调用 LLM 生成预测（通过 openclaw agent 的方式）"""
    # 写入临时文件，让外部调用
    import subprocess
    import tempfile

    # 尝试直接用 deepseek API
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        # 尝试从配置文件读取
        config_paths = [
            os.path.expanduser("~/.openclaw/config.json"),
            os.path.expanduser("~/.openclaw/workspace/config/llm_config.json"),
            os.path.expanduser("~/.openclaw/openclaw.json"),
            os.path.expanduser("~/.openclaw/agents/main/agent/models.json"),
        ]
        for p in config_paths:
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        cfg = json.load(f)
                    # 直接查找
                    api_key = cfg.get("deepseek_api_key", cfg.get("api_key", ""))
                    # 嵌套在 models.providers.deepseek.apiKey
                    if not api_key:
                        providers = cfg.get("models", cfg).get("providers", {})
                        ds = providers.get("deepseek", {})
                        api_key = ds.get("apiKey", ds.get("api_key", ""))
                    if api_key:
                        break
                except Exception:
                    pass

    if api_key:
        return _call_deepseek_api(prompt, api_key, model)

    # 回退：用 openrouter
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if openrouter_key:
        return _call_openrouter_api(prompt, openrouter_key, model)

    # 最后回退：基于规则的简单预测
    print("  ⚠️ 无可用 LLM API，使用规则预测")
    return _rule_based_prediction()


def _call_deepseek_api(prompt: str, api_key: str, model: str) -> str:
    """调用 DeepSeek API"""
    import urllib.request
    import urllib.error

    url = "https://api.deepseek.com/chat/completions"
    body = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是专业的A股投资分析师，擅长结合全球市场、地缘政治、大宗商品等宏观因素进行综合分析。横盘市场优先预测neutral。请严格按要求的 JSON 格式输出预测。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 1500,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    return result["choices"][0]["message"]["content"]


def _call_openrouter_api(prompt: str, api_key: str, model: str) -> str:
    """调用 OpenRouter API"""
    import urllib.request

    url = "https://openrouter.ai/api/v1/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是专业的A股投资分析师，擅长结合全球市场、地缘政治、大宗商品等宏观因素进行综合分析。横盘市场优先预测neutral。请严格按要求的 JSON 格式输出预测。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 1500,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    return result["choices"][0]["message"]["content"]


def _rule_based_prediction() -> str:
    """无 LLM 时的规则预测回退"""
    latest = db.get_latest_snapshot("daily_close")
    predictions = []
    if latest:
        data = latest.get("data", {})
        for q in data.get("quotes", []):
            if q.get("error"):
                continue
            code = q.get("code", "")
            name = q.get("name", "")
            change = q.get("change_percent", 0)
            # 简单均值回归：涨多了预测跌，跌多了预测涨
            if change > 1.5:
                direction, pred_change = "down", -change * 0.3
            elif change < -1.5:
                direction, pred_change = "up", abs(change) * 0.3
            else:
                direction, pred_change = "neutral", 0.0
            if code in ["sh000001", "sz399001", "sz399006"]:
                predictions.append({
                    "code": code, "name": name,
                    "direction": direction, "confidence": 0.3,
                    "predicted_change": round(pred_change, 2),
                    "strategy_used": "technical",
                    "reasoning": f"基于均值回归，今日涨跌{change}%",
                })
    if not predictions:
        predictions = [
            {"code": t["code"], "name": t["name"], "direction": "neutral",
             "confidence": 0.2, "predicted_change": 0.0,
             "strategy_used": "technical", "reasoning": "数据不足，默认中性"}
            for t in PREDICTION_TARGETS
        ]
    return json.dumps(predictions, ensure_ascii=False)


def parse_predictions(llm_output: str) -> List[Dict]:
    """从 LLM 输出中解析预测 JSON"""
    # 尝试提取 JSON 块
    text = llm_output.strip()

    # 去掉 markdown 代码块
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    # 尝试找到 JSON 数组
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        text = text[start:end + 1]

    predictions = json.loads(text)
    if not isinstance(predictions, list):
        predictions = [predictions]

    # 验证字段
    valid = []
    for p in predictions:
        if not p.get("code") or not p.get("direction"):
            continue
        if p["direction"] not in ("up", "down", "neutral"):
            continue
        p["confidence"] = max(0.0, min(1.0, float(p.get("confidence", 0.5))))
        p["predicted_change"] = float(p.get("predicted_change", 0.0))
        valid.append(p)

    return valid


def generate_predictions(model: str = "deepseek/deepseek-chat") -> List[int]:
    """主函数：生成预测并写入数据库"""
    print(f"🔮 开始生成市场预测 [{datetime.now().strftime('%Y-%m-%d %H:%M')}]")

    # 1. 获取最新市场数据
    latest = db.get_latest_snapshot("daily_close")
    if not latest:
        print("  ❌ 无市场数据快照，请先运行 collect")
        return []

    snapshot_data = latest.get("data", {})
    print(f"  📊 使用数据快照: {latest.get('captured_at', '?')}")

    # 2. 构建上下文
    rag_context = build_rag_context("A股明日走势预测 指数 资金流向")
    few_shot = build_few_shot_prompt()
    system_prompt = generate_system_prompt()

    # 3. 构建 prompt
    prompt = build_prediction_prompt(snapshot_data, rag_context, few_shot, system_prompt)

    # 4. 调用 LLM
    print(f"  🤖 调用 LLM 生成预测...")
    try:
        llm_output = call_llm_for_prediction(prompt, model)
    except Exception as e:
        print(f"  ❌ LLM 调用失败: {e}")
        print(f"  ⚠️ 回退到规则预测")
        llm_output = _rule_based_prediction()

    # 5. 解析预测
    try:
        predictions = parse_predictions(llm_output)
    except Exception as e:
        print(f"  ❌ 解析预测失败: {e}")
        print(f"  LLM 原始输出: {llm_output[:500]}")
        predictions = parse_predictions(_rule_based_prediction())

    if not predictions:
        print("  ❌ 未生成有效预测")
        return []

    # 6. 获取当前价格并写入数据库
    pred_ids = []
    for p in predictions:
        current_price = None
        quotes = fetch_market_quotes(p["code"])
        if quotes and not quotes[0].get("error"):
            current_price = quotes[0].get("price")

        pid = db.add_prediction(
            target=p["code"],
            target_name=p.get("name", ""),
            direction=p["direction"],
            confidence=p["confidence"],
            reasoning=p.get("reasoning", ""),
            strategy_used=p.get("strategy_used", "technical"),
            model_used=model,
            predicted_change=p.get("predicted_change"),
            actual_price=current_price,
        )
        pred_ids.append(pid)
        print(f"  📝 [{p.get('name', p['code'])}] {p['direction']} "
              f"(置信度:{p['confidence']:.0%}, 预测涨跌:{p.get('predicted_change', 0):+.2f}%) → ID:{pid}")

    print(f"✅ 预测生成完成，共 {len(pred_ids)} 条")
    return pred_ids

#!/usr/bin/env python3
"""Prediction prompt/context builder service."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Dict, List

import db


TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "prompts" / "market_prediction.md"


@lru_cache(maxsize=1)
def _load_market_prediction_template() -> str:
    try:
        text = TEMPLATE_PATH.read_text(encoding="utf-8")
        if text.strip():
            return text
    except Exception:
        pass
    # fallback for compatibility when template file is absent
    return (
        "你是A股投资分析助手。请基于以下最新市场数据，对明日A股主要指数走势做出预测。\n\n"
        "## 数据来源\n${source_summary}\n\n"
        "## 今日A股市场数据\n\n"
        "### 指数行情\n${quotes_str}\n\n"
        "### 资金流向\n${flow_str}\n\n"
        "### 板块资金流向（前10）\n${sectors_str}\n\n"
        "### 今日热门板块（涨停概念，板块轮动参考）\n"
        "说明：以下为同花顺涨停板块数据，反映当日市场资金主攻方向，对判断板块轮动和短期热点有重要参考意义。\n"
        "${hot_sectors_str}\n\n"
        "### 隔夜美股板块表现（S&P 500 十一大板块 ETF）\n"
        "说明：美股板块涨跌对次日A股对应板块有直接映射关系，如美股科技板块涨→A股芯片/AI受益。\n"
        "${us_sectors_str}\n\n"
        "### 美股龙头个股表现\n"
        "说明：美股龙头个股大幅波动会直接影响A股对应产业链板块，如英伟达涨→A股算力/芯片受益。\n"
        "${us_stocks_str}\n\n"
        "### 美股→A股板块轮动预判\n"
        "说明：基于隔夜美股板块和龙头表现，结合A股当前热门板块，预判次日板块轮动方向。\n"
        "${rotation_str}\n\n"
        "### 今日重要新闻\n${news_str}\n\n"
        "## 全球市场行情\n"
        "说明：隔夜美股走势对A股次日开盘有重要参考意义，港股与A股联动性强。\n"
        "${global_str}\n\n"
        "## 大宗商品价格\n"
        "说明：原油价格波动直接影响化工、航空板块及整体市场情绪；黄金走强通常反映避险情绪升温。\n"
        "${commodity_str}\n\n"
        "## 宏观/地缘政治新闻\n"
        "说明：重点关注地缘冲突（影响原油供应和市场情绪）、央行政策（影响流动性）、贸易摩擦等。\n"
        "${macro_str}\n\n"
        "## 市场状态检测\n${regime_str}\n\n"
        "## OpenClaw 实时行情\n${openclaw_str}\n\n"
        "## 实盘账户概况\n"
        "说明：以下为本人实盘账户数据（双服务器汇总），预测时应考虑当前仓位情况，避免在满仓时仍建议加仓。\n"
        "${account_str}\n\n"
        "## 当前持仓（含浮动盈亏）\n${positions_str}\n\n"
        "## 今日成交明细\n${trades_str}\n\n"
        "## 待成交委托\n${pending_str}\n\n"
        "${rag_context}\n\n${few_shot}\n\n"
        "## 预测要求（严格遵守）\n\n"
        "**关键规则：**\n"
        "1. 如果市场状态为\"sideways\"（横盘），应优先预测\"neutral\"，除非有明确的方向性催化剂\n"
        "2. 如果你预期的涨跌幅在±0.3%以内，必须预测\"neutral\"，不要强行给出方向\n"
        "3. 只有在出现明确方向性信号（重大政策变化、突发地缘事件、技术面明确突破/跌破等）时才预测\"up\"或\"down\"\n"
        "4. reasoning中必须提及全球市场和宏观因素的影响\n"
        "5. 微幅波动（±0.1%以内）不构成方向性判断依据\n"
        "6. 隔夜美股板块表现对A股对应板块有映射参考意义，reasoning中应结合美股板块轮动预判分析\n\n"
        "请对以下每个指数给出明日预测，严格按 JSON 格式输出，不要输出其他内容。\n"
    )


def _join_lines(lines: List[str]) -> str:
    return "".join(line for line in lines if line)


def _format_quotes(snapshot_data: Dict) -> str:
    lines = []
    for q in snapshot_data.get("quotes", []):
        if not isinstance(q, dict) or q.get("error"):
            continue
        lines.append(
            f"- {q.get('name', q.get('code', '?'))}: {q.get('price', '?')} "
            f"(涨跌幅: {q.get('change_percent', '?')}%)\n"
        )
    return _join_lines(lines)


def _format_sectors(snapshot_data: Dict) -> str:
    lines = []
    for s in snapshot_data.get("sectors", [])[:10]:
        if not isinstance(s, dict):
            continue
        lines.append(f"- {s.get('name', '?')}: 主力净流入 {s.get('main_net', '?')}万\n")
    return _join_lines(lines)


def _format_news(snapshot_data: Dict) -> str:
    lines = []
    for n in (snapshot_data.get("news_eastmoney", []) + snapshot_data.get("news_rss", []))[:8]:
        if isinstance(n, dict):
            lines.append(f"- {n.get('title', '')}\n")
        elif isinstance(n, str):
            lines.append(f"- {n}\n")
    return _join_lines(lines)


def _format_global(snapshot_data: Dict) -> str:
    lines = []
    for g in snapshot_data.get("global_indices", []):
        if not isinstance(g, dict):
            continue
        lines.append(
            f"- {g.get('name', '?')}: {g.get('price', '?')} "
            f"(涨跌幅: {g.get('change_percent', '?')}%)\n"
        )
    return _join_lines(lines)


def _format_commodities(snapshot_data: Dict) -> str:
    lines = []
    for c in snapshot_data.get("commodities", []):
        if not isinstance(c, dict):
            continue
        lines.append(
            f"- {c.get('name', '?')}: {c.get('price', '?')} "
            f"(涨跌幅: {c.get('change_percent', '?')}%)\n"
        )
    return _join_lines(lines)


def _format_macro_news(snapshot_data: Dict) -> str:
    lines = []
    for m in snapshot_data.get("macro_news", []):
        if not isinstance(m, dict):
            continue
        lines.append(f"- [{m.get('source', '')}] {m.get('title', '')}\n")
    return _join_lines(lines)


def _format_market_regime(snapshot_data: Dict) -> str:
    regime = snapshot_data.get("market_regime", {})
    if not regime:
        return ""
    return (
        f"当前市场状态: **{regime.get('regime', 'unknown')}**\n"
        f"- {regime.get('details', '')}\n"
        f"- 近10日波动率: {regime.get('volatility', 0)}%\n"
        f"- 近10日累计涨跌: {regime.get('total_change_10d', 0)}%\n"
        f"- 上涨天数/下跌天数: {regime.get('up_days', 0)}/{regime.get('down_days', 0)}\n"
    )


def _format_realtime_quotes(snapshot_data: Dict) -> str:
    lines = []
    for q in snapshot_data.get("openclaw_quotes", snapshot_data.get("qmt_quotes", [])):
        if not isinstance(q, dict) or not q.get("price"):
            continue
        lines.append(
            f"- {q.get('name', q.get('code', '?'))}: {q.get('price', '?')} "
            f"(涨跌幅: {q.get('change_percent', '?')}%, 成交额: {q.get('amount', '?')})\n"
        )
    return _join_lines(lines)


def _format_account_context(snapshot_data: Dict) -> str:
    account_str = ""
    acct = snapshot_data.get("qmt_account", {})
    if acct:
        account_str += f"- 总资产: {acct.get('total_asset', acct.get('m_dBalance', '?'))}\n"
        account_str += f"- 可用资金: {acct.get('cash', acct.get('m_dAvailable', '?'))}\n"
        account_str += f"- 持仓市值: {acct.get('market_value', acct.get('m_dStockValue', '?'))}\n"

    ts = snapshot_data.get("qmt_trading_summary", {})
    if ts:
        account_str = ""
        accounts = ts.get("accounts", {})
        for src in ("main", "trade"):
            src_acct = accounts.get(src, {})
            if src_acct:
                total = src_acct.get("total_asset", "?")
                avail = src_acct.get("cash", src_acct.get("available", "?"))
                account_str += f"- [{src}] 总资产: {total}, 可用: {avail}\n"
        account_str += f"- 持仓市值: {ts.get('total_market_value', '?')}\n"
        account_str += f"- 未实现盈亏: {ts.get('total_unrealized_pnl', '?')}\n"
        account_str += f"- 今日成交: {ts.get('today_trade_count', 0)}笔, 委托: {ts.get('today_order_count', 0)}笔\n"
        account_str += (
            f"- 买入 {ts.get('buy_count', 0)}笔/{ts.get('buy_amount', 0)}元, "
            f"卖出 {ts.get('sell_count', 0)}笔/{ts.get('sell_amount', 0)}元\n"
        )
    return account_str


def _format_positions(snapshot_data: Dict) -> str:
    lines = []
    for p in snapshot_data.get("qmt_positions", []):
        if not isinstance(p, dict):
            continue
        code = p.get("stock_code", p.get("m_strInstrumentID", "?"))
        name = p.get("stock_name", code)
        vol = p.get("volume", p.get("m_nVolume", 0))
        cost = p.get("open_price", p.get("m_dOpenPrice", 0))
        price = p.get("current_price", p.get("last_price", 0))
        mv = p.get("market_value", 0)
        profit_pct = p.get("profit_rate", 0)
        if isinstance(profit_pct, (int, float)):
            profit_pct = f"{profit_pct:.2f}%"
        try:
            cv = float(cost or 0)
            v = float(vol or 0)
            mv_f = float(mv or 0)
            pnl = mv_f - (cv * v) if cv and v else 0
            pnl_str = f"{pnl:+.2f}" if pnl else "?"
        except Exception:
            pnl_str = "?"
        lines.append(
            f"- {name}({code}): 持仓{vol}股, 成本{cost}, 现价{price}, "
            f"市值{mv}, 浮动盈亏{pnl_str}, 盈亏率{profit_pct}\n"
        )
    return _join_lines(lines)


def _format_trades(snapshot_data: Dict) -> str:
    lines = []
    for t in snapshot_data.get("qmt_trades", []):
        if not isinstance(t, dict):
            continue
        code = t.get("stock_code", "?")
        otype = t.get("order_type", 0)
        direction = "买" if otype in (23, "buy", "BUY") else "卖"
        tvol = t.get("trade_volume", t.get("deal_volume", 0))
        tprice = t.get("trade_price", t.get("deal_price", 0))
        tsrc = t.get("_source", "")
        lines.append(f"- [{direction}] {code} {tvol}股 @{tprice} ({tsrc})\n")
    return _join_lines(lines)


def _format_pending_orders(snapshot_data: Dict) -> str:
    lines = []
    for o in snapshot_data.get("qmt_orders", []):
        if not isinstance(o, dict) or o.get("order_status") in (3, 6, 7):
            continue
        code = o.get("stock_code", "?")
        otype = o.get("order_type", 0)
        direction = "买" if otype in (23, "buy", "BUY") else "卖"
        ovol = o.get("order_volume", 0)
        oprice = o.get("order_price", 0)
        lines.append(f"- [{direction}] {code} {ovol}股 @{oprice}\n")
    return _join_lines(lines)


def _load_sector_rotation_context() -> Dict:
    context = {
        "hot_sectors_str": "",
        "us_sectors_str": "",
        "us_stocks_str": "",
        "rotation_str": "",
    }
    try:
        sector_snap = db.get_latest_snapshot("sector_scan")
        if not sector_snap:
            return context
        snap_data = sector_snap.get("data", {}) or {}
        hot_lines = []
        for i, s in enumerate(snap_data.get("hot_sectors", [])[:10], 1):
            if not isinstance(s, dict):
                continue
            leaders = ", ".join(
                st["name"]
                for st in s.get("stocks", [])[:3]
                if isinstance(st, dict) and st.get("name")
            )
            hot_lines.append(
                f"- {i}. {s.get('name', '?')}: 涨停{s.get('limit_up_num', 0)}只, "
                f"涨幅{s.get('change', 0):+.2f}%, 龙头: {leaders}\n"
            )
        us_sector_lines = []
        for s in snap_data.get("us_sectors", []):
            if not isinstance(s, dict):
                continue
            us_sector_lines.append(
                f"- {s.get('name', '?')}({s.get('symbol', '?')}): 涨跌 {s.get('change_pct', 0):+.2f}%\n"
            )
        us_stock_lines = []
        for st in snap_data.get("us_stocks", []):
            if not isinstance(st, dict):
                continue
            a_secs = ", ".join(st.get("a_sectors", [])[:3])
            us_stock_lines.append(
                f"- {st.get('name', '?')}({st.get('symbol', '?')}): "
                f"涨跌 {st.get('change_pct', 0):+.2f}%, A股关联: {a_secs}\n"
            )
        rotation_lines = []
        rot = snap_data.get("rotation_prediction", {}) or {}
        if rot.get("continuing"):
            rotation_lines.append("持续强势板块:\n")
            for item in rot["continuing"]:
                rotation_lines.append(f"  - {item['a_sector']}: {item['reason']}\n")
        if rot.get("bullish"):
            rotation_lines.append("潜在轮动（看多）:\n")
            for item in rot["bullish"]:
                rotation_lines.append(f"  - {item['a_sector']}: {item['reason']}\n")
        if rot.get("bearish"):
            rotation_lines.append("可能承压（看空）:\n")
            for item in rot["bearish"]:
                rotation_lines.append(f"  - {item['a_sector']}: {item['reason']}\n")
        context["hot_sectors_str"] = _join_lines(hot_lines)
        context["us_sectors_str"] = _join_lines(us_sector_lines)
        context["us_stocks_str"] = _join_lines(us_stock_lines)
        context["rotation_str"] = _join_lines(rotation_lines)
    except Exception:
        pass
    return context


def build_prediction_context(snapshot_data: Dict) -> Dict:
    sector_context = _load_sector_rotation_context()
    return {
        "source_summary": (
            f"source={snapshot_data.get('_source', 'unknown')}, "
            f"packet_hits={snapshot_data.get('_packet_hits', 0)}"
            if snapshot_data.get("_source") == "research_packets"
            else f"source={snapshot_data.get('_source', 'unknown')}, "
            f"captured_at={snapshot_data.get('_captured_at', '')}"
        ),
        "quotes_str": _format_quotes(snapshot_data),
        "flow_str": json.dumps(snapshot_data.get("flow", {}), ensure_ascii=False)[:500]
        if snapshot_data.get("flow")
        else "无数据",
        "sectors_str": _format_sectors(snapshot_data),
        "news_str": _format_news(snapshot_data),
        "global_str": _format_global(snapshot_data),
        "commodity_str": _format_commodities(snapshot_data),
        "macro_str": _format_macro_news(snapshot_data),
        "regime_str": _format_market_regime(snapshot_data),
        "openclaw_str": _format_realtime_quotes(snapshot_data),
        "account_str": _format_account_context(snapshot_data),
        "positions_str": _format_positions(snapshot_data),
        "trades_str": _format_trades(snapshot_data),
        "pending_str": _format_pending_orders(snapshot_data),
        **sector_context,
    }


def build_market_context_text(snapshot_data: Dict) -> str:
    context = build_prediction_context(snapshot_data)
    sections = [
        "## 📊 最新市场数据",
        context["source_summary"],
        "",
        "### 指数行情",
        context["quotes_str"] or "无数据",
        "### 资金流向",
        context["flow_str"] or "无数据",
        "### 市场状态",
        context["regime_str"] or "无数据",
        "### 实盘账户概况",
        context["account_str"] or "无数据",
        "### 当前持仓",
        context["positions_str"] or "无持仓",
        "### 今日成交",
        context["trades_str"] or "今日无成交",
        "### 待成交委托",
        context["pending_str"] or "无待成交委托",
    ]
    return "\n".join(sections).strip()


def build_prediction_prompt(snapshot_data: Dict, rag_context: str, few_shot: str, system_prompt: str) -> str:
    """Construct LLM prediction prompt."""
    context = build_prediction_context(snapshot_data)
    quotes_str = context["quotes_str"]
    flow_str = context["flow_str"]
    sectors_str = context["sectors_str"]
    news_str = context["news_str"]
    global_str = context["global_str"]
    commodity_str = context["commodity_str"]
    macro_str = context["macro_str"]
    regime_str = context["regime_str"]
    openclaw_str = context["openclaw_str"]
    account_str = context["account_str"]
    positions_str = context["positions_str"]
    trades_str = context["trades_str"]
    pending_str = context["pending_str"]
    hot_sectors_str = context["hot_sectors_str"]
    us_sectors_str = context["us_sectors_str"]
    us_stocks_str = context["us_stocks_str"]
    rotation_str = context["rotation_str"]
    source_summary = context["source_summary"]

    template = Template(_load_market_prediction_template())
    return template.safe_substitute(
        source_summary=source_summary,
        quotes_str=quotes_str or "无数据",
        flow_str=flow_str or "无数据",
        sectors_str=sectors_str or "无数据",
        hot_sectors_str=hot_sectors_str or "无数据",
        us_sectors_str=us_sectors_str or "无数据",
        us_stocks_str=us_stocks_str or "无数据",
        rotation_str=rotation_str or "无明显信号",
        news_str=news_str or "无数据",
        global_str=global_str or "无数据",
        commodity_str=commodity_str or "无数据",
        macro_str=macro_str or "无数据",
        regime_str=regime_str or "无数据",
        openclaw_str=openclaw_str or "无数据",
        account_str=account_str or "无数据",
        positions_str=positions_str or "无持仓",
        trades_str=trades_str or "今日无成交",
        pending_str=pending_str or "无待成交委托",
        rag_context=rag_context or "",
        few_shot=few_shot or "",
        system_prompt=system_prompt or "",
    )

#!/usr/bin/env python3
"""
Loop 1: 感知 — 自动数据采集
定时抓取市场数据，调用 LLM 提取结构化摘要存入知识库
"""

import json
import os
import sys
import subprocess
from importlib import import_module
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# 添加项目路径
sys.path.insert(0, os.path.dirname(__file__))
import db

OPENCLAW_PLUGIN_ROOT = Path.home() / ".openclaw" / "extensions" / "openclaw-data-china-stock"
OPENCLAW_PLUGIN_READY = False
if OPENCLAW_PLUGIN_ROOT.exists():
    sys.path.insert(0, str(OPENCLAW_PLUGIN_ROOT))
    sys.path.insert(0, str(OPENCLAW_PLUGIN_ROOT / "plugins"))
    OPENCLAW_PLUGIN_READY = True

# ──────────────── QMT2HTTP 配置（使用统一客户端）────────────────

from qmt_client import QMTManager, get_qmt_manager

# 保留旧接口兼容
QMT_BASE_URL = os.environ.get("QMT2HTTP_BASE_URL", "http://39.105.48.176:8085").rstrip("/")
QMT_TIMEOUT = float(os.environ.get("QMT2HTTP_TIMEOUT", "20"))
QMT_API_TOKEN = os.environ.get("QMT2HTTP_API_TOKEN", "998811").strip()


def _get_qmt() -> QMTManager:
    """获取 QMT 管理器实例"""
    return get_qmt_manager()


def _qmt_headers() -> dict:
    headers = {"Accept": "application/json"}
    token = QMT_API_TOKEN
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-API-Token"] = token
    return headers


def _qmt_rpc(method: str, params: dict = None) -> Optional[Dict]:
    """调用 qmt2http 单个 RPC 方法（使用 QMTManager）"""
    try:
        qm = _get_qmt()
        result = qm.main.rpc(method, params)
        return {"success": True, "data": result}
    except Exception as e:
        print(f"  ⚠️ QMT RPC {method} 失败: {e}")
        return None


def _qmt_get(endpoint: str, query: dict = None) -> Optional[Dict]:
    """调用 qmt2http GET 端点（使用 QMTManager）"""
    try:
        qm = _get_qmt()
        result = qm.main.get(endpoint, query)
        return {"success": True, "data": result}
    except Exception as e:
        print(f"  ⚠️ QMT GET {endpoint} 失败: {e}")
        return None


# ──────────────── QMT 实时行情 ────────────────

# QMT 格式的指数代码映射
QMT_INDEX_CODES = {
    "sh000001": "000001.SH",
    "sz399001": "399001.SZ",
    "sz399006": "399006.SZ",
    "sh000688": "000688.SH",
}

OPENCLAW_INDEX_CODES = {
    "sh000001": "000001",
    "sz399001": "399001",
    "sz399006": "399006",
    "sh000688": "000688",
}


def _normalize_openclaw_quote(item: Dict, code_hint: str = "") -> Dict:
    code = str(
        item.get("stock_code")
        or item.get("code")
        or code_hint
        or ""
    ).strip()
    price = float(item.get("current_price", item.get("price", 0)) or 0)
    pre_close = float(item.get("prev_close", item.get("pre_close", 0)) or 0)
    change_pct = item.get("change_percent")
    if change_pct in (None, "") and pre_close and price:
        change_pct = round((price - pre_close) / pre_close * 100, 3)
    return {
        "code": code,
        "name": item.get("name", code),
        "price": price,
        "open": float(item.get("open", 0) or 0),
        "high": float(item.get("high", 0) or 0),
        "low": float(item.get("low", 0) or 0),
        "pre_close": pre_close,
        "volume": float(item.get("volume", 0) or 0),
        "amount": float(item.get("amount", 0) or 0),
        "change_percent": float(change_pct or 0),
        "source": "openclaw-data-china-stock",
    }


def _load_openclaw_tool(module_path: str, function_name: str):
    if not OPENCLAW_PLUGIN_READY:
        return None
    try:
        module = import_module(module_path)
        return getattr(module, function_name, None)
    except Exception as e:
        print(f"  ⚠️ OpenClaw 插件导入失败 {module_path}.{function_name}: {e}")
        return None


def fetch_openclaw_realtime(codes: List[str] = None) -> List[Dict]:
    """通过 openclaw-data-china-stock 获取指数实时行情。"""
    if codes is None:
        codes = list(OPENCLAW_INDEX_CODES.keys())
    code_list = [str(code).strip() for code in codes if str(code).strip()]
    if not code_list:
        return []

    tool = _load_openclaw_tool(
        "plugins.data_collection.index.fetch_realtime",
        "tool_fetch_index_realtime",
    )
    if not tool:
        return []

    plugin_codes = []
    alias_map = {}
    for code in code_list:
        plugin_code = OPENCLAW_INDEX_CODES.get(code, code.replace("sh", "").replace("sz", ""))
        plugin_codes.append(plugin_code)
        alias_map[plugin_code] = code

    try:
        result = tool(index_code=",".join(plugin_codes), mode="test")
    except Exception as e:
        print(f"  ⚠️ OpenClaw 指数行情失败: {e}")
        return []

    if not result or not result.get("success"):
        return []

    raw_data = result.get("data")
    raw_list = raw_data if isinstance(raw_data, list) else ([raw_data] if isinstance(raw_data, dict) else [])
    quotes = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        code_hint = alias_map.get(str(item.get("code", "")), str(item.get("code", "")))
        quotes.append(_normalize_openclaw_quote(item, code_hint=code_hint))
    return quotes


def fetch_qmt_realtime(codes: List[str] = None) -> List[Dict]:
    """通过 QMT2HTTP 获取实时行情快照（使用统一客户端）"""
    if codes is None:
        codes = list(QMT_INDEX_CODES.values())
    try:
        qm = _get_qmt()
        data = qm.get_market_data(codes)
    except Exception as e:
        print(f"  ⚠️ QMT 实时行情失败: {e}")
        return []
    quotes = []
    for code, info in data.items():
        if isinstance(info, dict):
            quotes.append({
                "code": code,
                "name": info.get("name", code),
                "price": info.get("lastPrice", info.get("last_price", 0)),
                "open": info.get("open", 0),
                "high": info.get("high", 0),
                "low": info.get("low", 0),
                "pre_close": info.get("lastClose", info.get("pre_close", 0)),
                "volume": info.get("volume", 0),
                "amount": info.get("amount", 0),
                "change_percent": 0,
                "source": "qmt",
            })
            q = quotes[-1]
            if q["pre_close"] and q["pre_close"] > 0 and q["price"]:
                q["change_percent"] = round((q["price"] - q["pre_close"]) / q["pre_close"] * 100, 3)
    return quotes


def fetch_qmt_account() -> Dict:
    """获取 QMT 实盘账户资产（从两个服务器获取）"""
    try:
        qm = _get_qmt()
        accounts = qm.get_all_accounts()
        return accounts.get("main", {})
    except Exception as e:
        print(f"  ⚠️ QMT 账户资产获取失败: {e}")
        return {}


def fetch_qmt_positions() -> List[Dict]:
    """获取 QMT 实盘持仓（合并两个服务器）"""
    try:
        qm = _get_qmt()
        return qm.get_all_positions()
    except Exception as e:
        print(f"  ⚠️ QMT 持仓获取失败: {e}")
        return []


def fetch_qmt_orders() -> List[Dict]:
    """获取今日委托（从两个服务器）"""
    try:
        qm = _get_qmt()
        return qm.get_all_orders()
    except Exception as e:
        print(f"  ⚠️ QMT 委托获取失败: {e}")
        return []


def fetch_qmt_trades() -> List[Dict]:
    """获取今日成交（从两个服务器）"""
    try:
        qm = _get_qmt()
        return qm.get_all_trades()
    except Exception as e:
        print(f"  ⚠️ QMT 成交获取失败: {e}")
        return []


def fetch_qmt_trading_summary() -> Dict:
    """获取综合交易摘要（账户+持仓+委托+成交+P&L）"""
    try:
        qm = _get_qmt()
        return qm.get_trading_summary()
    except Exception as e:
        print(f"  ⚠️ QMT 交易摘要获取失败: {e}")
        return {}


# ──────────────── 东方财富数据采集（复用已有 skills）────────────────

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "..", "skills")


def fetch_market_quotes(codes: str = "sh000001,sz399001,sz399006,sh000688") -> List[Dict]:
    """获取主要指数行情，优先 openclaw-data-china-stock，回退 eastmoney-quotes。"""
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    plugin_quotes = fetch_openclaw_realtime(code_list)
    if plugin_quotes:
        return plugin_quotes

    script = os.path.join(SKILLS_DIR, "eastmoney-quotes", "eastmoney_quotes.py")
    if not os.path.exists(script):
        print(f"⚠️ eastmoney-quotes skill 未找到: {script}")
        return []
    try:
        sys.path.insert(0, os.path.join(SKILLS_DIR, "eastmoney-quotes"))
        from eastmoney_quotes import get_quotes
        return get_quotes(code_list)
    except Exception as e:
        print(f"❌ 获取行情失败: {e}")
        return []


def fetch_market_flow(code: str = "sh000001") -> Dict:
    """获取资金流向 — 调用已有的 eastmoney-flow skill"""
    try:
        sys.path.insert(0, os.path.join(SKILLS_DIR, "eastmoney-flow"))
        from eastmoney_flow import get_stock_flow
        real_code = code.replace("sh", "").replace("sz", "")
        return get_stock_flow(real_code)
    except Exception as e:
        print(f"❌ 获取资金流向失败: {e}")
        return {"error": str(e)}


def fetch_sector_flow() -> List[Dict]:
    """获取板块资金流向"""
    try:
        sys.path.insert(0, os.path.join(SKILLS_DIR, "eastmoney-flow"))
        from eastmoney_flow import get_sector_flow
        return get_sector_flow()
    except Exception as e:
        print(f"❌ 获取板块资金流向失败: {e}")
        return []


def fetch_market_news() -> List[Dict]:
    """获取市场新闻 — 调用已有的 eastmoney-news skill"""
    try:
        sys.path.insert(0, os.path.join(SKILLS_DIR, "eastmoney-news"))
        from eastmoney_news import get_market_news
        return get_market_news()
    except Exception as e:
        print(f"❌ 获取新闻失败: {e}")
        return []


# ──────────────── AKShare 数据采集 ────────────────

def fetch_akshare_daily(symbol: str = "sh000001") -> Dict:
    """通过 AKShare 获取日线数据"""
    try:
        import akshare as ak
        # 上证指数
        if symbol.startswith("sh"):
            idx_code = symbol[2:]
            df = ak.stock_zh_index_daily(symbol=f"sh{idx_code}")
        elif symbol.startswith("sz"):
            idx_code = symbol[2:]
            df = ak.stock_zh_index_daily(symbol=f"sz{idx_code}")
        else:
            df = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                     start_date=(datetime.now() - timedelta(days=30)).strftime("%Y%m%d"),
                                     end_date=datetime.now().strftime("%Y%m%d"),
                                     adjust="qfq")

        if df is not None and not df.empty:
            latest = df.iloc[-1]
            cols = df.columns.tolist()
            return {
                "symbol": symbol,
                "date": str(latest.get("date", latest.get("日期", ""))),
                "open": float(latest.get("open", latest.get("开盘", 0))),
                "high": float(latest.get("high", latest.get("最高", 0))),
                "low": float(latest.get("low", latest.get("最低", 0))),
                "close": float(latest.get("close", latest.get("收盘", 0))),
                "volume": float(latest.get("volume", latest.get("成交量", 0))),
            }
    except Exception as e:
        print(f"⚠️ AKShare 获取 {symbol} 失败: {e}")
    return {}


def fetch_akshare_stock_history(symbol: str, days: int = 5) -> List[Dict]:
    """获取个股最近N天历史数据"""
    try:
        import akshare as ak
        start = (datetime.now() - timedelta(days=days + 10)).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                 start_date=start, end_date=end, adjust="qfq")
        if df is not None and not df.empty:
            records = []
            for _, row in df.tail(days).iterrows():
                records.append({
                    "date": str(row.get("日期", "")),
                    "open": float(row.get("开盘", 0)),
                    "high": float(row.get("最高", 0)),
                    "low": float(row.get("最低", 0)),
                    "close": float(row.get("收盘", 0)),
                    "volume": float(row.get("成交量", 0)),
                    "change_pct": float(row.get("涨跌幅", 0)),
                })
            return records
    except Exception as e:
        print(f"⚠️ AKShare 历史数据获取失败 {symbol}: {e}")
    return []


# ──────────────── 全球市场数据采集 ────────────────

def fetch_global_indices() -> List[Dict]:
    """通过 AKShare 获取全球主要指数（道琼斯、标普500、纳斯达克、日经225、恒生指数、美元指数等）"""
    try:
        import akshare as ak
        df = ak.index_global_spot_em()
        if df is None or df.empty:
            print("  ⚠️ 全球指数数据为空")
            return []

        # 关注的主要指数关键词
        target_names = [
            "道琼斯", "标普500", "纳斯达克", "日经225", "恒生指数",
            "美元指数", "英国富时", "德国DAX", "法国CAC", "韩国KOSPI",
            "上证指数", "深证成指", "创业板指",
        ]
        results = []
        for _, row in df.iterrows():
            name = str(row.get("名称", ""))
            if any(t in name for t in target_names):
                results.append({
                    "name": name,
                    "price": float(row.get("最新价", 0) or 0),
                    "change_percent": float(row.get("涨跌幅", 0) or 0),
                    "change_amount": float(row.get("涨跌额", 0) or 0),
                })
        return results
    except Exception as e:
        print(f"  ⚠️ 获取全球指数失败: {e}")
        return []


def fetch_commodity_prices() -> List[Dict]:
    """通过 AKShare 获取原油、黄金等大宗商品价格"""
    try:
        import akshare as ak
        # 尝试获取大宗商品现货数据
        results = []

        # 能源和贵金属期货
        commodity_map = {
            "原油": ["WTI原油", "布伦特原油", "原油"],
            "黄金": ["黄金", "COMEX黄金"],
            "白银": ["白银", "COMEX白银"],
            "铜": ["铜", "沪铜"],
        }

        try:
            df = ak.futures_foreign_commodity_realtime(symbol="全部")
            if df is not None and not df.empty:
                target_keywords = ["原油", "黄金", "白银", "天然气", "铜"]
                for _, row in df.iterrows():
                    name = str(row.get("名称", row.get("symbol", "")))
                    if any(k in name for k in target_keywords):
                        results.append({
                            "name": name,
                            "price": float(row.get("最新价", row.get("current_price", 0)) or 0),
                            "change_percent": float(row.get("涨跌幅", row.get("change_percent", 0)) or 0),
                        })
        except Exception:
            pass

        # 备用：尝试获取国内期货主力合约
        if not results:
            try:
                for symbol_name in ["原油", "黄金", "白银"]:
                    try:
                        df = ak.futures_main_sina(symbol=symbol_name)
                        if df is not None and not df.empty:
                            latest = df.iloc[-1]
                            results.append({
                                "name": symbol_name,
                                "price": float(latest.get("收盘价", latest.get("close", 0)) or 0),
                                "change_percent": 0,  # 需要计算
                            })
                    except Exception:
                        continue
            except Exception:
                pass

        return results
    except Exception as e:
        print(f"  ⚠️ 获取大宗商品价格失败: {e}")
        return []


def fetch_macro_news() -> List[Dict]:
    """通过 AKShare news_cctv() + 关键词过滤获取宏观/地缘政治新闻"""
    try:
        import akshare as ak
        from datetime import date as date_cls
        today_str = date_cls.today().strftime("%Y%m%d")
        df = ak.news_cctv(date=today_str)
        if df is None or df.empty:
            return []

        # 地缘政治/宏观经济关键词
        keywords = [
            "战争", "冲突", "制裁", "军事", "导弹", "袭击", "紧张",
            "原油", "石油", "能源", "OPEC",
            "央行", "利率", "降息", "加息", "货币政策", "美联储", "欧央行",
            "关税", "贸易战", "贸易摩擦",
            "GDP", "CPI", "PMI", "就业", "通胀", "衰退",
            "人民币", "汇率", "美元",
            "股市", "暴跌", "熔断", "危机",
        ]

        results = []
        for _, row in df.iterrows():
            title = str(row.get("title", ""))
            content = str(row.get("content", ""))
            text = title + content
            if any(kw in text for kw in keywords):
                results.append({
                    "title": title,
                    "content": content[:300],
                    "date": today_str,
                    "source": "CCTV新闻联播",
                })
        return results[:10]  # 最多10条
    except Exception as e:
        print(f"  ⚠️ 获取宏观新闻失败: {e}")
        return []


def detect_market_regime(snapshot_data: Dict = None) -> Dict:
    """
    基于近10天价格波动率和方向一致性，判断市场状态
    返回: {"regime": "uptrend/downtrend/sideways", "volatility": float, "details": str}
    """
    try:
        import akshare as ak
        # 获取上证指数近15天数据（多取几天以防非交易日）
        df = ak.stock_zh_index_daily(symbol="sh000001")
        if df is None or df.empty:
            return {"regime": "unknown", "volatility": 0, "details": "无法获取历史数据"}

        # 取最近10个交易日
        recent = df.tail(10)
        if len(recent) < 5:
            return {"regime": "unknown", "volatility": 0, "details": "历史数据不足"}

        closes = recent["close"].astype(float).tolist()

        # 计算每日涨跌幅
        daily_changes = []
        for i in range(1, len(closes)):
            pct = (closes[i] - closes[i - 1]) / closes[i - 1] * 100
            daily_changes.append(pct)

        if not daily_changes:
            return {"regime": "unknown", "volatility": 0, "details": "无法计算涨跌幅"}

        # 波动率（标准差）
        avg_change = sum(daily_changes) / len(daily_changes)
        variance = sum((x - avg_change) ** 2 for x in daily_changes) / len(daily_changes)
        volatility = variance ** 0.5

        # 累计涨跌幅
        total_change = (closes[-1] - closes[0]) / closes[0] * 100

        # 方向一致性：上涨天数占比
        up_days = sum(1 for c in daily_changes if c > 0)
        down_days = sum(1 for c in daily_changes if c < 0)
        up_ratio = up_days / len(daily_changes)

        # 判断市场状态
        if abs(total_change) < 1.0 and volatility < 0.8:
            regime = "sideways"
            details = f"横盘整理：近10日累计涨跌{total_change:+.2f}%，日均波动{volatility:.2f}%"
        elif total_change > 1.0 and up_ratio > 0.55:
            regime = "uptrend"
            details = f"上升趋势：近10日累计涨{total_change:+.2f}%，{up_days}天上涨"
        elif total_change < -1.0 and up_ratio < 0.45:
            regime = "downtrend"
            details = f"下降趋势：近10日累计跌{total_change:+.2f}%，{down_days}天下跌"
        elif volatility > 1.5:
            regime = "sideways"
            details = f"高波动横盘：近10日累计{total_change:+.2f}%，波动率{volatility:.2f}%"
        else:
            regime = "sideways"
            details = f"方向不明确：近10日累计{total_change:+.2f}%，波动率{volatility:.2f}%"

        return {
            "regime": regime,
            "volatility": round(volatility, 3),
            "total_change_10d": round(total_change, 2),
            "up_days": up_days,
            "down_days": down_days,
            "avg_daily_change": round(avg_change, 3),
            "details": details,
        }
    except Exception as e:
        print(f"  ⚠️ 市场状态检测失败: {e}")
        return {"regime": "unknown", "volatility": 0, "details": f"检测失败: {e}"}


# ──────────────── RSS 新闻采集 ────────────────

RSS_FEEDS = [
    ("财联社", "https://rsshub.app/cls/telegraph"),
    ("新浪财经", "https://rsshub.app/sina/finance"),
    ("东方财富", "https://rsshub.app/eastmoney/report"),
]


def fetch_rss_news(max_per_feed: int = 5) -> List[Dict]:
    """从 RSS 源获取新闻"""
    all_news = []
    try:
        import feedparser
    except ImportError:
        print("⚠️ feedparser 未安装，跳过 RSS")
        return []

    for name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]:
                all_news.append({
                    "source": name,
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", "")[:500],
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                })
        except Exception as e:
            print(f"⚠️ RSS {name} 获取失败: {e}")
    return all_news


# ──────────────── 数据采集主流程 ────────────────

def collect_daily_data() -> Dict:
    """每日数据采集主函数，返回采集结果摘要"""
    print(f"📡 开始每日数据采集 [{datetime.now().strftime('%Y-%m-%d %H:%M')}]")

    result = {
        "timestamp": datetime.now().isoformat(),
        "quotes": [],
        "flow": {},
        "sectors": [],
        "news_eastmoney": [],
        "news_rss": [],
        "akshare": {},
    }

    # 1. 指数行情
    print("  📊 获取指数行情...")
    quotes = fetch_market_quotes()
    result["quotes"] = quotes
    print(f"    ✅ 获取 {len(quotes)} 条行情")

    # 2. 资金流向
    print("  💰 获取资金流向...")
    flow = fetch_market_flow()
    result["flow"] = flow

    # 3. 板块流向
    print("  📈 获取板块流向...")
    sectors = fetch_sector_flow()
    result["sectors"] = sectors
    print(f"    ✅ 获取 {len(sectors)} 个板块")

    # 4. 东方财富新闻
    print("  📰 获取东方财富新闻...")
    em_news = fetch_market_news()
    result["news_eastmoney"] = em_news
    print(f"    ✅ 获取 {len(em_news)} 条新闻")

    # 5. RSS 新闻
    print("  📡 获取 RSS 新闻...")
    rss_news = fetch_rss_news()
    result["news_rss"] = rss_news
    print(f"    ✅ 获取 {len(rss_news)} 条 RSS 新闻")

    # 6. AKShare 补充数据
    print("  📈 获取 AKShare 数据...")
    for idx in ["sh000001", "sz399001", "sz399006"]:
        ak_data = fetch_akshare_daily(idx)
        if ak_data:
            result["akshare"][idx] = ak_data

    # 7. 全球指数
    print("  🌍 获取全球指数...")
    global_indices = fetch_global_indices()
    result["global_indices"] = global_indices
    print(f"    ✅ 获取 {len(global_indices)} 条全球指数")

    # 8. 大宗商品价格
    print("  🛢️ 获取大宗商品价格...")
    commodities = fetch_commodity_prices()
    result["commodities"] = commodities
    print(f"    ✅ 获取 {len(commodities)} 条商品价格")

    # 9. 宏观/地缘政治新闻
    print("  🌐 获取宏观新闻...")
    macro_news = fetch_macro_news()
    result["macro_news"] = macro_news
    print(f"    ✅ 获取 {len(macro_news)} 条宏观新闻")

    # 10. 市场状态检测
    print("  🔍 检测市场状态...")
    market_regime = detect_market_regime(result)
    result["market_regime"] = market_regime
    print(f"    ✅ 市场状态: {market_regime.get('regime', 'unknown')} - {market_regime.get('details', '')}")

    # 11. OpenClaw 实时行情
    print("  📡 获取 OpenClaw 实时行情...")
    openclaw_quotes = fetch_openclaw_realtime()
    result["openclaw_quotes"] = openclaw_quotes
    print(f"    ✅ 获取 {len(openclaw_quotes)} 条 OpenClaw 行情")

    # 12. QMT 实盘账户资产（双服务器）
    print("  💼 获取 QMT 账户资产...")
    qmt_account = fetch_qmt_account()
    result["qmt_account"] = qmt_account
    if qmt_account:
        print(f"    ✅ 账户资产已获取")

    # 13. QMT 实盘持仓（双服务器合并）
    print("  📋 获取 QMT 持仓...")
    qmt_positions = fetch_qmt_positions()
    result["qmt_positions"] = qmt_positions
    print(f"    ✅ 获取 {len(qmt_positions)} 条持仓")

    # 14. QMT 今日委托（双服务器合并）
    print("  📝 获取 QMT 今日委托...")
    qmt_orders = fetch_qmt_orders()
    result["qmt_orders"] = qmt_orders
    print(f"    ✅ 获取 {len(qmt_orders)} 条委托")

    # 15. QMT 今日成交（双服务器合并）
    print("  💹 获取 QMT 今日成交...")
    qmt_trades = fetch_qmt_trades()
    result["qmt_trades"] = qmt_trades
    print(f"    ✅ 获取 {len(qmt_trades)} 条成交")

    # 16. QMT 综合交易摘要
    print("  📊 生成交易摘要...")
    qmt_trading_summary = fetch_qmt_trading_summary()
    result["qmt_trading_summary"] = qmt_trading_summary
    if qmt_trading_summary:
        pnl = qmt_trading_summary.get("total_unrealized_pnl", 0)
        print(f"    ✅ 交易摘要: 持仓{qmt_trading_summary.get('positions_count', 0)}只, 总未实现盈亏{pnl}")

    # 17. 存入数据库
    db.save_market_snapshot("daily_close", result)
    packet_ids = db.save_daily_close_packets(result, snapshot_type="daily_close")
    print(
        "    ✅ research_packets/portfolio_snapshots 已写入: "
        f"{', '.join(f'{name}={packet_id}' for name, packet_id in packet_ids.items())}"
    )
    print(f"✅ 每日数据采集完成，已存入数据库")

    return result


def collect_intraday_data() -> Dict:
    """盘中数据采集（简化版）"""
    result = {
        "timestamp": datetime.now().isoformat(),
        "quotes": fetch_market_quotes(),
        "flow": fetch_market_flow(),
    }
    db.save_market_snapshot("intraday", result)
    db.save_research_packet(
        "market",
        {
            "quotes": result.get("quotes", []),
            "flow": result.get("flow", {}),
        },
        as_of_date=result.get("timestamp"),
        source_snapshot_type="intraday",
        metadata={"fields": ["quotes", "flow"]},
    )
    return result


if __name__ == "__main__":
    db.init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "intraday":
        data = collect_intraday_data()
    else:
        data = collect_daily_data()
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str)[:3000])

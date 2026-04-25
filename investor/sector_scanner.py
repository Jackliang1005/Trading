#!/usr/bin/env python3
"""
板块轮动扫描 + 持仓诊断模块
自上而下分析链条：热门板块 → 核心个股 → 持仓诊断
"""

import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(__file__))

import db
from qmt_client import get_qmt_manager

# ──────────────── 常量 ────────────────

CONCEPTS_DB_PATH = "/root/qmttrader/concept_db/concepts.db"

# S&P 500 板块 ETF → A股板块映射
US_SECTOR_ETFS = {
    "XLK": {"name": "科技", "a_sectors": ["芯片", "半导体", "人工智能", "软件", "云计算", "消费电子"]},
    "XLF": {"name": "金融", "a_sectors": ["银行", "保险", "券商", "证券"]},
    "XLE": {"name": "能源", "a_sectors": ["石油", "煤炭", "天然气", "油气"]},
    "XLV": {"name": "医疗保健", "a_sectors": ["医药", "生物医药", "医疗器械", "创新药"]},
    "XLY": {"name": "可选消费", "a_sectors": ["汽车", "家电", "白酒", "旅游", "零售"]},
    "XLP": {"name": "必需消费", "a_sectors": ["食品饮料", "农业", "乳业"]},
    "XLI": {"name": "工业", "a_sectors": ["机械", "军工", "航空", "基建", "工程机械"]},
    "XLB": {"name": "材料", "a_sectors": ["有色金属", "钢铁", "化工", "锂电"]},
    "XLRE": {"name": "房地产", "a_sectors": ["房地产", "物业"]},
    "XLU": {"name": "公用事业", "a_sectors": ["电力", "水务", "燃气"]},
    "XLC": {"name": "通信服务", "a_sectors": ["传媒", "游戏", "通信", "互联网"]},
}

# 对A股有映射意义的美股龙头
US_KEY_STOCKS = {
    "NVDA": {"name": "英伟达", "a_sectors": ["芯片", "人工智能", "算力"]},
    "AAPL": {"name": "苹果", "a_sectors": ["消费电子", "苹果产业链"]},
    "TSLA": {"name": "特斯拉", "a_sectors": ["新能源车", "汽车零部件", "锂电"]},
    "MSFT": {"name": "微软", "a_sectors": ["云计算", "人工智能", "软件"]},
    "AMZN": {"name": "亚马逊", "a_sectors": ["电商", "云计算", "跨境电商"]},
    "GOOGL": {"name": "谷歌", "a_sectors": ["人工智能", "互联网", "广告"]},
    "META": {"name": "Meta", "a_sectors": ["元宇宙", "VR", "互联网"]},
    "AMD": {"name": "AMD", "a_sectors": ["芯片", "半导体", "算力"]},
    "AVGO": {"name": "博通", "a_sectors": ["芯片", "半导体", "通信"]},
    "LLY": {"name": "礼来", "a_sectors": ["创新药", "减肥药", "生物医药"]},
}

THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Referer": "https://data.10jqka.com.cn/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ──────────────── 热门板块获取 ────────────────

def fetch_hot_sectors(date_str: str = None) -> List[Dict]:
    """获取今日热门板块（同花顺涨停板块 API）

    Returns:
        [{name, code, limit_up_num, change, stocks: [{code, name}]}]
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    url = "https://data.10jqka.com.cn/dataapi/limit_up/block_top"
    params = f"filter=HS,GEM2STAR&date={date_str}"
    full_url = f"{url}?{params}"

    req = urllib.request.Request(full_url, headers=THS_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status_code") == 0 and "data" in data:
            raw = data["data"]
            sectors = []
            for item in raw:
                stocks = []
                for s in item.get("stock_list", []):
                    stocks.append({
                        "code": s.get("code", ""),
                        "name": s.get("name", ""),
                    })
                sectors.append({
                    "name": item.get("name", ""),
                    "code": item.get("code", ""),
                    "limit_up_num": item.get("limit_up_num", 0),
                    "change": item.get("change", 0),
                    "stocks": stocks,
                })
            print(f"  ✅ 获取 {len(sectors)} 个热门板块")
            return sectors
        else:
            print(f"  ⚠️ 同花顺 API 返回异常: {data.get('status_msg', 'unknown')}")
    except Exception as e:
        print(f"  ⚠️ 获取热门板块失败: {e}")
    return []


# ──────────────── 隔夜美股数据 ────────────────

def fetch_us_sector_performance() -> List[Dict]:
    """获取隔夜美股板块 ETF 表现（S&P 500 十一大板块）

    Returns:
        [{symbol, name, close, prev_close, change_pct}] 按涨跌幅降序
    """
    try:
        import akshare as ak
    except ImportError:
        print("  ⚠️ akshare 未安装，跳过美股板块")
        return []

    results = []
    for symbol, info in US_SECTOR_ETFS.items():
        try:
            df = ak.stock_us_daily(symbol=symbol, adjust="")
            if df is None or len(df) < 2:
                continue
            df = df.tail(2)
            prev_close = float(df.iloc[0]["close"])
            close = float(df.iloc[1]["close"])
            change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0
            results.append({
                "symbol": symbol,
                "name": info["name"],
                "close": close,
                "prev_close": prev_close,
                "change_pct": change_pct,
            })
        except Exception as e:
            print(f"  ⚠️ 获取 {symbol}({info['name']}) 失败: {e}")
    results.sort(key=lambda x: x["change_pct"], reverse=True)
    print(f"  ✅ 获取 {len(results)}/{len(US_SECTOR_ETFS)} 个美股板块")
    return results


def fetch_us_key_stocks() -> List[Dict]:
    """获取美股龙头个股表现

    Returns:
        [{symbol, name, close, change_pct, a_sectors}] 按涨跌幅降序
    """
    try:
        import akshare as ak
    except ImportError:
        return []

    results = []
    for symbol, info in US_KEY_STOCKS.items():
        try:
            df = ak.stock_us_daily(symbol=symbol, adjust="")
            if df is None or len(df) < 2:
                continue
            df = df.tail(2)
            prev_close = float(df.iloc[0]["close"])
            close = float(df.iloc[1]["close"])
            change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0
            results.append({
                "symbol": symbol,
                "name": info["name"],
                "close": close,
                "change_pct": change_pct,
                "a_sectors": info["a_sectors"],
            })
        except Exception as e:
            print(f"  ⚠️ 获取 {symbol}({info['name']}) 失败: {e}")
    results.sort(key=lambda x: x["change_pct"], reverse=True)
    print(f"  ✅ 获取 {len(results)}/{len(US_KEY_STOCKS)} 只美股龙头")
    return results


# ──────────────── 美股→A股板块轮动预判 ────────────────

def predict_a_sector_rotation(us_sectors: List[Dict], us_stocks: List[Dict],
                              a_hot_sectors: List[Dict]) -> Dict:
    """基于美股板块/龙头表现，预判A股板块轮动

    Returns:
        {bullish: [{a_sector, reason}], bearish: [{a_sector, reason}],
         continuing: [{a_sector, reason}]}
    """
    # 构建A股当前热门板块名称集合
    hot_names = {s.get("name", "") for s in a_hot_sectors}

    bullish = []   # 看多
    bearish = []   # 看空
    continuing = []  # 持续强势

    seen_bullish = set()
    seen_bearish = set()

    # 1. 美股涨幅前3板块（仅涨幅 > 0）→ 映射A股板块
    for s in us_sectors[:3]:
        if s["change_pct"] <= 0:
            continue
        symbol = s["symbol"]
        mapping = US_SECTOR_ETFS.get(symbol, {})
        for a_sec in mapping.get("a_sectors", []):
            if a_sec in seen_bullish:
                continue
            reason = f"美股{s['name']}板块({symbol})涨{s['change_pct']:+.2f}%"
            if a_sec in hot_names:
                continuing.append({"a_sector": a_sec, "reason": f"{reason}，A股已热门，持续强势"})
            else:
                bullish.append({"a_sector": a_sec, "reason": f"{reason}，潜在轮动机会"})
            seen_bullish.add(a_sec)

    # 2. 美股涨幅 > 1% 的龙头 → 映射A股板块
    for st in us_stocks:
        if st["change_pct"] <= 1.0:
            continue
        for a_sec in st.get("a_sectors", []):
            if a_sec in seen_bullish:
                continue
            reason = f"{st['name']}({st['symbol']})涨{st['change_pct']:+.2f}%"
            if a_sec in hot_names:
                continuing.append({"a_sector": a_sec, "reason": f"{reason}，A股已热门"})
            else:
                bullish.append({"a_sector": a_sec, "reason": f"{reason}，龙头带动"})
            seen_bullish.add(a_sec)

    # 3. 美股跌幅前3板块 → 对应A股板块可能承压
    for s in us_sectors[-3:]:
        if s["change_pct"] >= 0:
            continue
        symbol = s["symbol"]
        mapping = US_SECTOR_ETFS.get(symbol, {})
        for a_sec in mapping.get("a_sectors", []):
            if a_sec in seen_bearish or a_sec in seen_bullish:
                continue
            reason = f"美股{s['name']}板块({symbol})跌{s['change_pct']:+.2f}%，可能承压"
            bearish.append({"a_sector": a_sec, "reason": reason})
            seen_bearish.add(a_sec)

    return {"bullish": bullish, "bearish": bearish, "continuing": continuing}


# ──────────────── 个股板块查询 ────────────────

def get_stock_sectors_local(stock_code: str) -> List[str]:
    """本地 concepts.db 查询个股所属板块

    Args:
        stock_code: 纯数字股票代码，如 '000001' 或 '600519'
    """
    if not os.path.exists(CONCEPTS_DB_PATH):
        return []
    try:
        conn = sqlite3.connect(CONCEPTS_DB_PATH)
        cursor = conn.cursor()
        # 去掉可能的后缀
        base_code = stock_code.split(".")[0].lstrip("0") if len(stock_code) > 6 else stock_code
        # 先尝试精确匹配
        cursor.execute("""
            SELECT DISTINCT hc.concept_name
            FROM concept_stocks cs
            JOIN hot_concepts hc ON cs.concept_code = hc.concept_code
            WHERE cs.stock_code = ?
        """, (stock_code,))
        rows = cursor.fetchall()
        if not rows:
            # 尝试去掉前导零
            cursor.execute("""
                SELECT DISTINCT hc.concept_name
                FROM concept_stocks cs
                JOIN hot_concepts hc ON cs.concept_code = hc.concept_code
                WHERE cs.stock_code = ?
            """, (base_code,))
            rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows if row[0]]
    except Exception as e:
        print(f"  ⚠️ 本地板块查询失败 {stock_code}: {e}")
        return []


def get_stock_sectors_ths(stock_code: str) -> List[str]:
    """同花顺网页查询个股所属板块（备用）"""
    base_code = stock_code.split(".")[0]
    url = f"http://basic.10jqka.com.cn/{base_code}/concept.html"
    headers = {
        "User-Agent": THS_HEADERS["User-Agent"],
        "Referer": "http://basic.10jqka.com.cn/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        import requests
        resp = requests.get(url, headers=headers, timeout=2, allow_redirects=False)
        if resp.status_code != 200:
            return []
        resp.encoding = "gbk"
        pattern = r'class="gnName"[^>]*>\s*(.*?)\s*</td>'
        found = re.findall(pattern, resp.text, re.DOTALL)
        return [c.strip() for c in found if c.strip()]
    except Exception as e:
        print(f"  ⚠️ 同花顺网页板块查询失败 {stock_code}: {e}")
        return []


def get_stock_sectors(stock_code: str) -> List[str]:
    """统一入口：按优先级查询个股所属板块
    1. QMT RPC
    2. concepts.db 本地查询
    3. 同花顺网页爬取
    """
    base_code = stock_code.split(".")[0]

    # 1. QMT RPC（通过统一客户端）
    try:
        qm = get_qmt_manager()
        qmt_code = stock_code if "." in stock_code else stock_code
        data = qm.main.get_stock_sectors(qmt_code)
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass

    # 2. 本地 DB
    sectors = get_stock_sectors_local(base_code)
    if sectors:
        return sectors

    # 3. 同花顺网页
    time.sleep(0.3)  # 限速
    return get_stock_sectors_ths(base_code)


# ──────────────── 持仓获取 ────────────────

def fetch_positions() -> tuple[List[Dict], str]:
    """获取持仓：优先 QMT 双服务器实时，回退到 portfolio snapshot，再回退旧 snapshot。"""
    try:
        qm = get_qmt_manager()
        positions = qm.get_all_positions()
        if positions:
            return positions, "runtime"
    except Exception as e:
        print(f"  ⚠️ QMT 持仓获取失败: {e}")

    # 2. 从统一 bundle 读取 portfolio snapshot
    bundle = db.get_latest_analysis_context_bundle(packet_types=["prediction_context"])
    portfolio = bundle.get("portfolio_snapshot")
    if portfolio:
        cached = portfolio.get("data", {}).get("qmt_positions", [])
        if cached:
            print("  ℹ️ 使用 portfolio snapshot 持仓缓存")
            return cached, "portfolio_snapshot"

    # 3. 从最近旧 snapshot 读取缓存
    latest = db.get_latest_snapshot("daily_close")
    if latest:
        cached = latest.get("data", {}).get("qmt_positions", [])
        if cached:
            print("  ℹ️ 使用 daily_close snapshot 持仓缓存")
            return cached, "daily_close_snapshot"

    return [], "none"


# ──────────────── 持仓诊断 ────────────────

def diagnose_positions(hot_sectors: List[Dict], positions: List[Dict]) -> List[Dict]:
    """持仓诊断：与热门板块交叉比对

    Returns:
        [{stock_code, stock_name, volume, cost, profit_rate,
          sectors, hot_sectors_matched, hot_rank, is_core, label}]
    """
    if not positions:
        return []

    # 构建热门板块索引
    # sector_name -> {rank, limit_up_num, core_stock_codes}
    hot_index = {}
    for rank, sector in enumerate(hot_sectors, 1):
        core_codes = {s["code"] for s in sector.get("stocks", [])}
        hot_index[sector["name"]] = {
            "rank": rank,
            "limit_up_num": sector.get("limit_up_num", 0),
            "change": sector.get("change", 0),
            "core_codes": core_codes,
        }

    results = []
    for pos in positions:
        stock_code = pos.get("stock_code", pos.get("m_strInstrumentID", ""))
        stock_name = pos.get("stock_name", stock_code)
        volume = pos.get("volume", pos.get("m_nVolume", 0))
        cost = pos.get("open_price", pos.get("m_dOpenPrice", 0))
        profit_rate = pos.get("profit_rate", 0)

        # 提取纯数字代码
        base_code = stock_code.split(".")[0]
        # 去掉可能的 sh/sz 前缀
        if base_code.startswith(("sh", "sz", "SH", "SZ")):
            base_code = base_code[2:]

        # 查询所属板块
        sectors = get_stock_sectors(base_code)

        # 与热门板块交叉
        matched_hot = []
        is_core = False
        best_rank = 999

        for sec_name in sectors:
            if sec_name in hot_index:
                info = hot_index[sec_name]
                matched_hot.append({
                    "name": sec_name,
                    "rank": info["rank"],
                    "limit_up_num": info["limit_up_num"],
                })
                if info["rank"] < best_rank:
                    best_rank = info["rank"]
                # 检查是否为涨停核心股
                if base_code in info["core_codes"]:
                    is_core = True

        # 判定标签
        if is_core:
            label = "🟢 热门板块核心股"
        elif matched_hot:
            label = "🟡 热门板块关联股"
        else:
            label = "🔴 非热门板块"

        results.append({
            "stock_code": stock_code,
            "stock_name": stock_name,
            "volume": volume,
            "cost": cost,
            "profit_rate": profit_rate,
            "sectors": sectors[:10],  # 最多显示10个
            "hot_sectors_matched": matched_hot,
            "hot_rank": best_rank if matched_hot else None,
            "is_core": is_core,
            "label": label,
        })

    # 按热度排序：核心 > 关联 > 非热门
    results.sort(key=lambda x: (
        0 if x["is_core"] else (1 if x["hot_sectors_matched"] else 2),
        x.get("hot_rank") or 999,
    ))

    return results


# ──────────────── 报告生成 ────────────────

def generate_sector_report() -> Dict:
    """主函数：生成板块扫描 + 持仓诊断完整报告"""
    print(f"🔍 板块轮动扫描 [{datetime.now().strftime('%Y-%m-%d %H:%M')}]")

    # 1. 获取热门板块
    print("  📊 获取热门板块...")
    hot_sectors = fetch_hot_sectors()

    # 2. 获取隔夜美股板块 + 龙头个股
    print("  🇺🇸 获取隔夜美股板块...")
    us_sectors = fetch_us_sector_performance()
    us_stocks = fetch_us_key_stocks()

    # 3. 获取持仓
    print("  📋 获取持仓...")
    positions, portfolio_source = fetch_positions()

    # 4. 持仓诊断
    print("  🔬 持仓诊断...")
    diagnosis = diagnose_positions(hot_sectors, positions) if positions else []

    # 5. A股板块轮动预判
    print("  🔮 板块轮动预判...")
    rotation_prediction = predict_a_sector_rotation(us_sectors, us_stocks, hot_sectors)

    # 6. 组装报告
    report_data = {
        "timestamp": datetime.now().isoformat(),
        "hot_sectors": hot_sectors,
        "us_sectors": us_sectors,
        "us_stocks": us_stocks,
        "rotation_prediction": rotation_prediction,
        "positions_count": len(positions),
        "portfolio_source": portfolio_source,
        "diagnosis": diagnosis,
    }

    # 7. 存入 DB
    db.save_market_snapshot("sector_scan", report_data)

    # 8. 打印格式化报告
    _print_report(hot_sectors, diagnosis, us_sectors, us_stocks, rotation_prediction)

    return report_data


def _print_report(hot_sectors: List[Dict], diagnosis: List[Dict],
                   us_sectors: List[Dict] = None, us_stocks: List[Dict] = None,
                   rotation: Dict = None):
    """打印格式化报告"""
    print("\n" + "=" * 60)
    print("📊 板块轮动扫描报告")
    print("=" * 60)

    # 隔夜美股板块表现
    if us_sectors:
        print("\n🇺🇸 隔夜美股板块表现")
        print("-" * 50)
        for s in us_sectors:
            arrow = "🔺" if s["change_pct"] > 0 else "🔻" if s["change_pct"] < 0 else "➖"
            print(f"  {arrow} {s['name']:<8s}({s['symbol']:<4s}) | "
                  f"收盘 {s['close']:>8.2f} | 涨跌 {s['change_pct']:+.2f}%")

    # 美股龙头个股
    if us_stocks:
        print(f"\n🇺🇸 美股龙头个股")
        print("-" * 50)
        for st in us_stocks:
            arrow = "🔺" if st["change_pct"] > 0 else "🔻" if st["change_pct"] < 0 else "➖"
            a_sec_str = ", ".join(st["a_sectors"][:3])
            print(f"  {arrow} {st['name']:<6s}({st['symbol']:<5s}) | "
                  f"涨跌 {st['change_pct']:+.2f}% | A股关联: {a_sec_str}")

    # A股板块轮动预判
    if rotation:
        print(f"\n🔮 A股板块轮动预判")
        print("-" * 50)
        if rotation.get("continuing"):
            print("  📈 持续强势:")
            for item in rotation["continuing"]:
                print(f"     • {item['a_sector']} — {item['reason']}")
        if rotation.get("bullish"):
            print("  🟢 潜在轮动（看多）:")
            for item in rotation["bullish"]:
                print(f"     • {item['a_sector']} — {item['reason']}")
        if rotation.get("bearish"):
            print("  🔴 可能承压（看空）:")
            for item in rotation["bearish"]:
                print(f"     • {item['a_sector']} — {item['reason']}")
        if not any(rotation.get(k) for k in ("continuing", "bullish", "bearish")):
            print("  ℹ️ 美股板块波动不大，暂无明显轮动信号")

    # 热门板块 TOP10
    print("\n🔥 热门板块 TOP10（按涨停数排序）")
    print("-" * 50)
    for i, s in enumerate(hot_sectors[:10], 1):
        stock_names = ", ".join(st["name"] for st in s.get("stocks", [])[:5])
        extra = f"..." if len(s.get("stocks", [])) > 5 else ""
        print(f"  {i:2d}. {s['name']:<12s} | 涨停 {s.get('limit_up_num', 0):2d} 只 | "
              f"涨幅 {s.get('change', 0):+.2f}%")
        if stock_names:
            print(f"      龙头: {stock_names}{extra}")

    # 持仓诊断
    if diagnosis:
        print(f"\n💼 持仓诊断（共 {len(diagnosis)} 只）")
        print("-" * 50)
        for d in diagnosis:
            name = d["stock_name"] or d["stock_code"]
            profit = d.get("profit_rate", 0)
            if isinstance(profit, (int, float)):
                profit_str = f"{profit:+.2f}%"
            else:
                profit_str = str(profit)
            print(f"  {d['label']} {name}({d['stock_code']})")
            print(f"      持仓: {d['volume']}股 | 成本: {d['cost']} | 盈亏: {profit_str}")
            if d["hot_sectors_matched"]:
                hot_names = ", ".join(
                    f"{h['name']}(#{h['rank']})" for h in d["hot_sectors_matched"][:3]
                )
                print(f"      热门板块: {hot_names}")
            if d["sectors"]:
                print(f"      所属板块: {', '.join(d['sectors'][:5])}")

        # 汇总
        core_count = sum(1 for d in diagnosis if d["is_core"])
        hot_count = sum(1 for d in diagnosis if d["hot_sectors_matched"] and not d["is_core"])
        cold_count = sum(1 for d in diagnosis if not d["hot_sectors_matched"])
        print(f"\n  📈 汇总: 核心股 {core_count} 只 | 关联股 {hot_count} 只 | 非热门 {cold_count} 只")

        if cold_count > 0 and cold_count == len(diagnosis):
            print("  ⚠️ 建议: 持仓全部偏离当前热门板块，关注板块轮动机会")
        elif core_count > 0:
            print("  ✅ 建议: 持仓中有热门板块核心股，关注龙头持续性")
    else:
        print("\n💼 当前无持仓或无法获取持仓数据")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    db.init_db()
    generate_sector_report()

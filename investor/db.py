#!/usr/bin/env python3
"""
OpenClaw Investor - 数据库层
结构化存储：预测记录、策略、反馈、规则、Few-shot案例
"""

import sqlite3
import json
import os
from datetime import datetime, date
from typing import Optional, List, Dict, Any

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "investor.db")
_INIT_DB_LOGGED = False


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """初始化所有表"""
    global _INIT_DB_LOGGED
    conn = get_conn()
    conn.executescript("""
    -- 预测记录表
    CREATE TABLE IF NOT EXISTS prediction_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        target TEXT NOT NULL,            -- 标的：如 sh000001, 600519
        target_name TEXT,                -- 标的名称
        direction TEXT NOT NULL,         -- up / down / neutral
        confidence REAL NOT NULL,        -- 置信度 0-1
        reasoning TEXT,                  -- 推理过程
        strategy_used TEXT,              -- 使用的策略名称
        model_used TEXT,                 -- 使用的模型
        timeframe TEXT DEFAULT '1d',     -- 预测时间框架
        predicted_change REAL,           -- 预测涨跌幅 %
        -- 回测结果（后续填入）
        actual_price_at_predict REAL,
        actual_price_at_check REAL,
        actual_change REAL,              -- 实际涨跌幅 %
        is_correct INTEGER,              -- 1=正确 0=错误
        score REAL,                      -- 综合评分 0-100
        checked_at TEXT,                 -- 回测时间
        check_note TEXT                  -- 回测备注
    );

    -- 策略表
    CREATE TABLE IF NOT EXISTS strategy (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,       -- 策略名称
        category TEXT NOT NULL,          -- technical / fundamental / sentiment
        description TEXT,
        weight REAL NOT NULL DEFAULT 0.33,  -- 当前权重
        total_predictions INTEGER DEFAULT 0,
        correct_predictions INTEGER DEFAULT 0,
        win_rate REAL DEFAULT 0,
        avg_score REAL DEFAULT 0,
        last_updated TEXT DEFAULT (datetime('now')),
        enabled INTEGER DEFAULT 1
    );

    -- 反馈表
    CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        prediction_id INTEGER,
        interaction_id TEXT,
        action TEXT NOT NULL,            -- accepted / rejected / ignored
        reason TEXT,
        user_comment TEXT,
        FOREIGN KEY (prediction_id) REFERENCES prediction_log(id)
    );

    -- 规则库
    CREATE TABLE IF NOT EXISTS rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        rule_text TEXT NOT NULL,          -- 规则内容
        source TEXT,                     -- 来源：reflection / user / backtest
        category TEXT,                   -- entry / exit / risk / general
        confidence REAL DEFAULT 0.5,
        times_applied INTEGER DEFAULT 0,
        times_helpful INTEGER DEFAULT 0,
        enabled INTEGER DEFAULT 1,
        last_updated TEXT DEFAULT (datetime('now'))
    );

    -- Few-shot 案例库
    CREATE TABLE IF NOT EXISTS few_shot_examples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        category TEXT NOT NULL,          -- good_analysis / bad_analysis
        scenario TEXT NOT NULL,          -- 场景描述
        input_text TEXT NOT NULL,        -- 输入
        output_text TEXT NOT NULL,       -- 输出/分析
        score REAL,                      -- 质量评分
        times_used INTEGER DEFAULT 0,
        enabled INTEGER DEFAULT 1
    );

    -- 反思报告
    CREATE TABLE IF NOT EXISTS reflection_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        report_type TEXT NOT NULL,       -- daily / weekly / monthly
        period_start TEXT,
        period_end TEXT,
        total_predictions INTEGER,
        correct_predictions INTEGER,
        win_rate REAL,
        key_findings TEXT,               -- JSON: 主要发现
        failure_patterns TEXT,           -- JSON: 失败模式
        improvement_suggestions TEXT,    -- JSON: 改进建议
        actions_taken TEXT,              -- JSON: 已采取的行动
        full_report TEXT                 -- 完整报告文本
    );

    -- 预测评估结果（与 prediction_log 解耦，保留历史多次评估）
    CREATE TABLE IF NOT EXISTS prediction_evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        prediction_id INTEGER NOT NULL,
        target_date TEXT,
        actual_price REAL,
        actual_change REAL,
        is_correct INTEGER,
        score REAL,
        note TEXT DEFAULT '',
        source TEXT DEFAULT 'backtest',
        FOREIGN KEY (prediction_id) REFERENCES prediction_log(id)
    );

    -- 市场数据快照
    CREATE TABLE IF NOT EXISTS market_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at TEXT NOT NULL DEFAULT (datetime('now')),
        snapshot_type TEXT NOT NULL,     -- daily_close / intraday / news
        data TEXT NOT NULL              -- JSON: 原始数据
    );

    -- 研究包：将大 snapshot 拆成可独立读取的 packet
    CREATE TABLE IF NOT EXISTS research_packets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        as_of_date TEXT NOT NULL,
        packet_type TEXT NOT NULL,       -- market / macro / sector_rotation / prediction_context
        schema_version TEXT NOT NULL DEFAULT 'v1',
        source_snapshot_type TEXT DEFAULT '',
        data TEXT NOT NULL,
        metadata TEXT DEFAULT '{}'
    );

    -- 组合快照：持仓/委托/成交/账户状态
    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        as_of_date TEXT NOT NULL,
        account_scope TEXT NOT NULL,     -- combined / main / trade / guojin / dongguan
        schema_version TEXT NOT NULL DEFAULT 'v1',
        source_snapshot_type TEXT DEFAULT '',
        data TEXT NOT NULL,
        metadata TEXT DEFAULT '{}'
    );

    -- 交互记录（用于 RAG）
    CREATE TABLE IF NOT EXISTS interactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        session_id TEXT,
        user_query TEXT NOT NULL,
        assistant_response TEXT NOT NULL,
        model_used TEXT,
        has_prediction INTEGER DEFAULT 0,
        prediction_id INTEGER,
        quality_score REAL,
        FOREIGN KEY (prediction_id) REFERENCES prediction_log(id)
    );

    CREATE INDEX IF NOT EXISTS idx_prediction_target ON prediction_log(target);
    CREATE INDEX IF NOT EXISTS idx_prediction_date ON prediction_log(created_at);
    CREATE INDEX IF NOT EXISTS idx_prediction_unchecked ON prediction_log(checked_at) WHERE checked_at IS NULL;
    CREATE INDEX IF NOT EXISTS idx_strategy_name ON strategy(name);
    CREATE INDEX IF NOT EXISTS idx_rules_enabled ON rules(enabled);
    CREATE INDEX IF NOT EXISTS idx_fewshot_category ON few_shot_examples(category, enabled);
    CREATE INDEX IF NOT EXISTS idx_market_type ON market_snapshots(snapshot_type, captured_at);
    CREATE INDEX IF NOT EXISTS idx_prediction_eval_prediction
        ON prediction_evaluations(prediction_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_prediction_eval_target_date
        ON prediction_evaluations(target_date, created_at);
    CREATE INDEX IF NOT EXISTS idx_research_packets_type_date
        ON research_packets(packet_type, as_of_date, created_at);
    CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_scope_date
        ON portfolio_snapshots(account_scope, as_of_date, created_at);

    -- 日内风险快照
    CREATE TABLE IF NOT EXISTS intraday_risk_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at TEXT NOT NULL DEFAULT (datetime('now')),
        total_asset REAL,
        total_market_value REAL,
        cash_ratio REAL,
        max_single_position_ratio REAL,
        max_sector_concentration REAL,
        intraday_max_drawdown REAL,
        alert_triggers TEXT DEFAULT '[]'
    );

    -- 策略绩效指标
    CREATE TABLE IF NOT EXISTS strategy_performance_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        as_of_date TEXT NOT NULL,
        strategy_name TEXT NOT NULL,
        sharpe_ratio REAL,
        max_drawdown REAL,
        win_rate REAL,
        profit_loss_ratio REAL,
        total_trades INTEGER,
        avg_return REAL,
        volatility REAL
    );

    CREATE INDEX IF NOT EXISTS idx_risk_snapshots_date
        ON intraday_risk_snapshots(captured_at);
    CREATE INDEX IF NOT EXISTS idx_perf_metrics_date
        ON strategy_performance_metrics(as_of_date, strategy_name);
    """)

    # 初始化默认策略
    strategies = [
        ("technical", "technical", "技术分析：基于价格走势、成交量、K线形态、均线系统等", 0.30),
        ("fundamental", "fundamental", "基本面分析：基于财报数据、估值、行业地位、成长性等", 0.25),
        ("sentiment", "sentiment", "情绪面分析：基于新闻情绪、资金流向、市场热度等", 0.20),
        ("geopolitical", "geopolitical", "地缘宏观分析：国际局势、地缘冲突、大宗商品、全球市场联动、央行政策", 0.25),
    ]
    for name, cat, desc, weight in strategies:
        conn.execute(
            "INSERT OR IGNORE INTO strategy (name, category, description, weight) VALUES (?, ?, ?, ?)",
            (name, cat, desc, weight),
        )

    # 迁移：为已有数据库添加 geopolitical 策略
    existing = conn.execute("SELECT name FROM strategy WHERE name='geopolitical'").fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO strategy (name, category, description, weight) VALUES (?, ?, ?, ?)",
            ("geopolitical", "geopolitical",
             "地缘宏观分析：国际局势、地缘冲突、大宗商品、全球市场联动、央行政策", 0.25),
        )
        # 重新分配已有策略权重
        conn.execute("UPDATE strategy SET weight=0.30 WHERE name='technical'")
        conn.execute("UPDATE strategy SET weight=0.25 WHERE name='fundamental'")
        conn.execute("UPDATE strategy SET weight=0.20 WHERE name='sentiment'")
        print("  📌 已添加 geopolitical 策略并重新分配权重")

    # 初始化默认规则（先检查是否已存在）
    existing_rules = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    if existing_rules == 0:
        default_rules = [
            ("连续3天放量上涨后不宜追高，历史胜率较低", "backtest", "entry", 0.6),
            ("财报超预期但股价不涨，需关注前期涨幅是否过大（预期差）", "backtest", "general", 0.6),
            ("大盘缩量下跌时不宜抄底，等待放量企稳信号", "backtest", "entry", 0.5),
            ("利好出尽是利空，重大利好公布后观察资金是否出逃", "backtest", "exit", 0.5),
            ("板块轮动周期通常3-5天，追热点需在初期介入", "backtest", "entry", 0.5),
        ]
        for text, source, cat, conf in default_rules:
            conn.execute(
                "INSERT INTO rules (rule_text, source, category, confidence) VALUES (?, ?, ?, ?)",
                (text, source, cat, conf),
            )

    conn.commit()
    conn.close()
    if not _INIT_DB_LOGGED:
        print(f"✅ 数据库初始化完成: {DB_PATH}")
        _INIT_DB_LOGGED = True


# ──────────────── 预测记录操作 ────────────────

def add_prediction(target: str, direction: str, confidence: float,
                   reasoning: str = "", strategy_used: str = "",
                   model_used: str = "", timeframe: str = "1d",
                   predicted_change: float = None,
                   actual_price: float = None,
                   target_name: str = "") -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO prediction_log
           (target, target_name, direction, confidence, reasoning, strategy_used,
            model_used, timeframe, predicted_change, actual_price_at_predict)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (target, target_name, direction, confidence, reasoning, strategy_used,
         model_used, timeframe, predicted_change, actual_price),
    )
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


def update_prediction_result(pred_id: int, actual_price: float,
                             actual_change: float, is_correct: bool,
                             score: float, note: str = ""):
    conn = get_conn()
    conn.execute(
        """UPDATE prediction_log
           SET actual_price_at_check=?, actual_change=?, is_correct=?,
               score=?, checked_at=datetime('now'), check_note=?
           WHERE id=?""",
        (actual_price, actual_change, 1 if is_correct else 0, score, note, pred_id),
    )
    conn.commit()
    conn.close()


def add_prediction_evaluation(
    prediction_id: int,
    actual_price: float,
    actual_change: float,
    is_correct: bool,
    score: float,
    note: str = "",
    target_date: str = "",
    source: str = "backtest",
) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO prediction_evaluations
           (prediction_id, target_date, actual_price, actual_change, is_correct, score, note, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            prediction_id,
            target_date or "",
            actual_price,
            actual_change,
            1 if is_correct else 0,
            score,
            note,
            source,
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_unchecked_predictions(before_date: str = None) -> List[Dict]:
    conn = get_conn()
    sql = "SELECT * FROM prediction_log WHERE checked_at IS NULL"
    params = []
    if before_date:
        sql += " AND date(created_at) <= date(?)"
        params.append(before_date)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_predictions_in_range(start: str, end: str) -> List[Dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM prediction_log WHERE date(created_at) BETWEEN date(?) AND date(?)",
        (start, end),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_checked_predictions_in_range(start: str, end: str) -> List[Dict]:
    conn = get_conn()
    rows = conn.execute(
        """WITH latest_eval AS (
               SELECT e.*
               FROM prediction_evaluations e
               JOIN (
                   SELECT prediction_id, MAX(id) AS max_id
                   FROM prediction_evaluations
                   GROUP BY prediction_id
               ) m ON m.max_id = e.id
           )
           SELECT p.id,
                  p.created_at,
                  p.target,
                  p.target_name,
                  p.direction,
                  p.confidence,
                  p.reasoning,
                  p.strategy_used,
                  p.model_used,
                  p.timeframe,
                  p.predicted_change,
                  p.actual_price_at_predict,
                  COALESCE(le.actual_price, p.actual_price_at_check) AS actual_price_at_check,
                  COALESCE(le.actual_change, p.actual_change) AS actual_change,
                  COALESCE(le.is_correct, p.is_correct) AS is_correct,
                  COALESCE(le.score, p.score) AS score,
                  COALESCE(le.created_at, p.checked_at) AS checked_at,
                  COALESCE(le.note, p.check_note) AS check_note
           FROM prediction_log p
           LEFT JOIN latest_eval le ON le.prediction_id = p.id
           WHERE date(p.created_at) BETWEEN date(?) AND date(?)
             AND (le.id IS NOT NULL OR p.checked_at IS NOT NULL)""",
        (start, end),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ──────────────── 策略操作 ────────────────

def get_strategies(enabled_only: bool = True) -> List[Dict]:
    conn = get_conn()
    sql = "SELECT * FROM strategy"
    if enabled_only:
        sql += " WHERE enabled=1"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_strategy_weight(name: str, new_weight: float):
    conn = get_conn()
    conn.execute(
        "UPDATE strategy SET weight=?, last_updated=datetime('now') WHERE name=?",
        (new_weight, name),
    )
    conn.commit()
    conn.close()


def update_strategy_stats(name: str, total: int, correct: int, win_rate: float, avg_score: float):
    conn = get_conn()
    conn.execute(
        """UPDATE strategy SET total_predictions=?, correct_predictions=?,
           win_rate=?, avg_score=?, last_updated=datetime('now') WHERE name=?""",
        (total, correct, win_rate, avg_score, name),
    )
    conn.commit()
    conn.close()


# ──────────────── 规则操作 ────────────────

def get_rules(enabled_only: bool = True) -> List[Dict]:
    conn = get_conn()
    sql = "SELECT * FROM rules"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY confidence DESC"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_rule(text: str, source: str, category: str, confidence: float = 0.5) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO rules (rule_text, source, category, confidence) VALUES (?, ?, ?, ?)",
        (text, source, category, confidence),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


# ──────────────── Few-shot 操作 ────────────────

def get_few_shot_examples(category: str = "good_analysis", limit: int = 5) -> List[Dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM few_shot_examples
           WHERE category=? AND enabled=1 ORDER BY score DESC LIMIT ?""",
        (category, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_few_shot_example(category: str, scenario: str, input_text: str,
                         output_text: str, score: float = 0.5) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO few_shot_examples (category, scenario, input_text, output_text, score)
           VALUES (?, ?, ?, ?, ?)""",
        (category, scenario, input_text, output_text, score),
    )
    conn.commit()
    fid = cur.lastrowid
    conn.close()
    return fid


# ──────────────── 反思报告 ────────────────

def add_reflection_report(report_type: str, period_start: str, period_end: str,
                          stats: Dict, full_report: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO reflection_reports
           (report_type, period_start, period_end, total_predictions,
            correct_predictions, win_rate, key_findings, failure_patterns,
            improvement_suggestions, actions_taken, full_report)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (report_type, period_start, period_end,
         stats.get("total", 0), stats.get("correct", 0), stats.get("win_rate", 0),
         json.dumps(stats.get("findings", []), ensure_ascii=False),
         json.dumps(stats.get("failures", []), ensure_ascii=False),
         json.dumps(stats.get("suggestions", []), ensure_ascii=False),
         json.dumps(stats.get("actions", []), ensure_ascii=False),
         full_report),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


# ──────────────── 市场快照 ────────────────

def save_market_snapshot(snapshot_type: str, data: Dict):
    conn = get_conn()
    conn.execute(
        "INSERT INTO market_snapshots (snapshot_type, data) VALUES (?, ?)",
        (snapshot_type, json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def get_latest_snapshot(snapshot_type: str) -> Optional[Dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM market_snapshots WHERE snapshot_type=? ORDER BY captured_at DESC LIMIT 1",
        (snapshot_type,),
    ).fetchone()
    conn.close()
    if row:
        d = dict(row)
        d["data"] = json.loads(d["data"])
        return d
    return None


def _normalize_as_of_date(raw: Any = None) -> str:
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    value = str(raw or "").strip()
    if not value:
        return datetime.now().date().isoformat()
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return value[:10]


def save_research_packet(
    packet_type: str,
    data: Dict,
    as_of_date: Any = None,
    schema_version: str = "v1",
    source_snapshot_type: str = "",
    metadata: Optional[Dict] = None,
) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO research_packets
           (as_of_date, packet_type, schema_version, source_snapshot_type, data, metadata)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            _normalize_as_of_date(as_of_date),
            packet_type,
            schema_version,
            source_snapshot_type,
            json.dumps(data, ensure_ascii=False),
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_latest_research_packet(packet_type: str, as_of_date: Any = None) -> Optional[Dict]:
    conn = get_conn()
    sql = """SELECT * FROM research_packets
             WHERE packet_type=?"""
    params: List[Any] = [packet_type]
    if as_of_date:
        sql += " AND as_of_date=?"
        params.append(_normalize_as_of_date(as_of_date))
    sql += " ORDER BY as_of_date DESC, created_at DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    conn.close()
    if row is None:
        return None
    item = dict(row)
    item["data"] = json.loads(item["data"]) if item.get("data") else {}
    item["metadata"] = json.loads(item["metadata"]) if item.get("metadata") else {}
    return item


def get_latest_research_packets(packet_types: List[str], as_of_date: Any = None) -> Dict[str, Dict]:
    items: Dict[str, Dict] = {}
    for packet_type in packet_types:
        packet = get_latest_research_packet(packet_type, as_of_date=as_of_date)
        if packet is not None:
            items[packet_type] = packet
    return items


def save_portfolio_snapshot(
    account_scope: str,
    data: Dict,
    as_of_date: Any = None,
    schema_version: str = "v1",
    source_snapshot_type: str = "",
    metadata: Optional[Dict] = None,
) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO portfolio_snapshots
           (as_of_date, account_scope, schema_version, source_snapshot_type, data, metadata)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            _normalize_as_of_date(as_of_date),
            account_scope,
            schema_version,
            source_snapshot_type,
            json.dumps(data, ensure_ascii=False),
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_latest_portfolio_snapshot(account_scope: str = "combined", as_of_date: Any = None) -> Optional[Dict]:
    conn = get_conn()
    sql = """SELECT * FROM portfolio_snapshots
             WHERE account_scope=?"""
    params: List[Any] = [account_scope]
    if as_of_date:
        sql += " AND as_of_date=?"
        params.append(_normalize_as_of_date(as_of_date))
    sql += " ORDER BY as_of_date DESC, created_at DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    conn.close()
    if row is None:
        return None
    item = dict(row)
    item["data"] = json.loads(item["data"]) if item.get("data") else {}
    item["metadata"] = json.loads(item["metadata"]) if item.get("metadata") else {}
    return item


def get_latest_analysis_context_bundle(
    as_of_date: Any = None,
    account_scope: str = "combined",
    packet_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    selected_packet_types = packet_types or ["market", "macro", "sector_rotation", "prediction_context"]
    packets = get_latest_research_packets(selected_packet_types, as_of_date=as_of_date)
    portfolio = get_latest_portfolio_snapshot(account_scope=account_scope, as_of_date=as_of_date)
    return {
        "as_of_date": _normalize_as_of_date(as_of_date) if as_of_date else "",
        "research_packets": packets,
        "portfolio_snapshot": portfolio,
        "packet_hits": len(packets) + (1 if portfolio else 0),
    }


def summarize_analysis_context_bundle(
    as_of_date: Any = None,
    account_scope: str = "combined",
    packet_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    bundle = get_latest_analysis_context_bundle(
        as_of_date=as_of_date,
        account_scope=account_scope,
        packet_types=packet_types,
    )
    packets = bundle.get("research_packets", {}) or {}
    portfolio = bundle.get("portfolio_snapshot")
    market = (packets.get("market") or {}).get("data", {}) or {}
    prediction_context = (packets.get("prediction_context") or {}).get("data", {}) or {}
    portfolio_data = (portfolio or {}).get("data", {}) or {}
    trading_summary = (
        prediction_context.get("qmt_trading_summary")
        or portfolio_data.get("qmt_trading_summary")
        or {}
    )
    positions = (
        prediction_context.get("qmt_positions")
        or portfolio_data.get("qmt_positions")
        or []
    )
    return {
        "as_of_date": bundle.get("as_of_date", ""),
        "packet_hits": int(bundle.get("packet_hits", 0) or 0),
        "packet_types": list(packets.keys()),
        "has_portfolio_snapshot": portfolio is not None,
        "quote_count": len(market.get("quotes", []) or []),
        "has_flow": bool(market.get("flow")),
        "has_market_regime": bool(market.get("market_regime")),
        "positions_count": trading_summary.get("positions_count", len(positions)),
        "today_trade_count": trading_summary.get("today_trade_count", 0),
        "today_order_count": trading_summary.get("today_order_count", 0),
        "total_unrealized_pnl": trading_summary.get("total_unrealized_pnl", 0),
    }


def save_daily_close_packets(snapshot_data: Dict, snapshot_type: str = "daily_close") -> Dict[str, int]:
    as_of_date = _normalize_as_of_date(
        snapshot_data.get("date")
        or snapshot_data.get("trade_date")
        or snapshot_data.get("timestamp")
        or snapshot_data.get("captured_at")
    )
    packet_ids: Dict[str, int] = {}

    market_packet = {
        "quotes": snapshot_data.get("quotes", []),
        "flow": snapshot_data.get("flow", {}),
        "market_regime": snapshot_data.get("market_regime", {}),
        "openclaw_quotes": snapshot_data.get("openclaw_quotes", []),
    }
    packet_ids["market"] = save_research_packet(
        "market",
        market_packet,
        as_of_date=as_of_date,
        source_snapshot_type=snapshot_type,
        metadata={"fields": list(market_packet.keys())},
    )

    macro_packet = {
        "global_indices": snapshot_data.get("global_indices", []),
        "commodities": snapshot_data.get("commodities", []),
        "macro_news": snapshot_data.get("macro_news", []),
        "news_eastmoney": snapshot_data.get("news_eastmoney", [])[:50],
        "news_rss": snapshot_data.get("news_rss", [])[:50],
    }
    packet_ids["macro"] = save_research_packet(
        "macro",
        macro_packet,
        as_of_date=as_of_date,
        source_snapshot_type=snapshot_type,
        metadata={"fields": list(macro_packet.keys())},
    )

    sector_packet = {
        "sectors": snapshot_data.get("sectors", []),
    }
    packet_ids["sector_rotation"] = save_research_packet(
        "sector_rotation",
        sector_packet,
        as_of_date=as_of_date,
        source_snapshot_type=snapshot_type,
        metadata={"fields": list(sector_packet.keys())},
    )

    prediction_context_packet = {
        "market_regime": snapshot_data.get("market_regime", {}),
        "qmt_trading_summary": snapshot_data.get("qmt_trading_summary", {}),
        "qmt_account": snapshot_data.get("qmt_account", {}),
        "qmt_positions": snapshot_data.get("qmt_positions", []),
        "qmt_orders": snapshot_data.get("qmt_orders", []),
        "qmt_trades": snapshot_data.get("qmt_trades", []),
        "openclaw_quotes": snapshot_data.get("openclaw_quotes", []),
    }
    packet_ids["prediction_context"] = save_research_packet(
        "prediction_context",
        prediction_context_packet,
        as_of_date=as_of_date,
        source_snapshot_type=snapshot_type,
        metadata={"fields": list(prediction_context_packet.keys())},
    )

    portfolio_packet = {
        "qmt_account": snapshot_data.get("qmt_account", {}),
        "qmt_positions": snapshot_data.get("qmt_positions", []),
        "qmt_orders": snapshot_data.get("qmt_orders", []),
        "qmt_trades": snapshot_data.get("qmt_trades", []),
        "qmt_trading_summary": snapshot_data.get("qmt_trading_summary", {}),
    }
    packet_ids["portfolio"] = save_portfolio_snapshot(
        "combined",
        portfolio_packet,
        as_of_date=as_of_date,
        source_snapshot_type=snapshot_type,
        metadata={"fields": list(portfolio_packet.keys())},
    )

    return packet_ids


# ──────────────── 交互记录 ────────────────

def save_interaction(user_query: str, response: str, session_id: str = "",
                     model_used: str = "", has_prediction: bool = False,
                     prediction_id: int = None, quality_score: float = None) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO interactions
           (session_id, user_query, assistant_response, model_used,
            has_prediction, prediction_id, quality_score)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, user_query, response, model_used,
         1 if has_prediction else 0, prediction_id, quality_score),
    )
    conn.commit()
    iid = cur.lastrowid
    conn.close()
    return iid


# ──────────────── 反馈操作 ────────────────

def add_feedback(action: str, prediction_id: int = None,
                 interaction_id: str = "", reason: str = "",
                 user_comment: str = "") -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO feedback (prediction_id, interaction_id, action, reason, user_comment)
           VALUES (?, ?, ?, ?, ?)""",
        (prediction_id, interaction_id, action, reason, user_comment),
    )
    conn.commit()
    fid = cur.lastrowid
    conn.close()
    return fid


# ──────────────── 统计查询 ────────────────

def get_strategy_performance(start: str, end: str) -> List[Dict]:
    """获取指定时间范围内各策略的表现"""
    conn = get_conn()
    rows = conn.execute(
        """WITH latest_eval AS (
               SELECT e.*
               FROM prediction_evaluations e
               JOIN (
                   SELECT prediction_id, MAX(id) AS max_id
                   FROM prediction_evaluations
                   GROUP BY prediction_id
               ) m ON m.max_id = e.id
           ),
           merged AS (
               SELECT p.strategy_used AS strategy_used,
                      COALESCE(le.is_correct, p.is_correct) AS is_correct,
                      COALESCE(le.score, p.score) AS score
               FROM prediction_log p
               LEFT JOIN latest_eval le ON le.prediction_id = p.id
               WHERE date(p.created_at) BETWEEN date(?) AND date(?)
                 AND (le.id IS NOT NULL OR p.checked_at IS NOT NULL)
                 AND p.strategy_used != ''
           )
           SELECT strategy_used,
                  COUNT(*) as total,
                  SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) as correct,
                  ROUND(AVG(CASE WHEN is_correct IS NOT NULL THEN is_correct ELSE NULL END) * 100, 1) as win_rate,
                  ROUND(AVG(score), 1) as avg_score
           FROM merged
           GROUP BY strategy_used""",
        (start, end),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_overall_stats(start: str = None, end: str = None) -> Dict:
    """获取整体统计"""
    conn = get_conn()
    where = "WHERE (le.id IS NOT NULL OR p.checked_at IS NOT NULL)"
    params = []
    if start and end:
        where += " AND date(p.created_at) BETWEEN date(?) AND date(?)"
        params = [start, end]
    row = conn.execute(
        f"""WITH latest_eval AS (
                SELECT e.*
                FROM prediction_evaluations e
                JOIN (
                    SELECT prediction_id, MAX(id) AS max_id
                    FROM prediction_evaluations
                    GROUP BY prediction_id
                ) m ON m.max_id = e.id
            )
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN COALESCE(le.is_correct, p.is_correct)=1 THEN 1 ELSE 0 END) as correct,
                   ROUND(
                     AVG(
                       CASE WHEN COALESCE(le.is_correct, p.is_correct) IS NOT NULL
                       THEN COALESCE(le.is_correct, p.is_correct)
                       ELSE NULL END
                     ) * 100, 1
                   ) as win_rate,
                   ROUND(AVG(COALESCE(le.score, p.score)), 1) as avg_score,
                   ROUND(AVG(COALESCE(le.actual_change, p.actual_change)), 2) as avg_change
            FROM prediction_log p
            LEFT JOIN latest_eval le ON le.prediction_id = p.id
            {where}""",
        params,
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


if __name__ == "__main__":
    init_db()

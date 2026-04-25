#!/usr/bin/env python3
"""
Loop 2: 记忆 — 知识库自动积累
使用 SQLite FTS5 全文搜索（无需下载模型，完全离线工作）
+ 结构化数据库 (SQLite) 双存储
RAG 检索增强回答
"""

import json
import os
import sys
import hashlib
import sqlite3
from datetime import datetime
from typing import List, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(__file__))
import db

KB_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "knowledge.db")


def get_kb_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(KB_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(KB_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_kb():
    """初始化知识库 FTS5 表"""
    conn = get_kb_conn()
    conn.executescript("""
    -- 知识文档表
    CREATE TABLE IF NOT EXISTS kb_documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id TEXT UNIQUE NOT NULL,
        doc_type TEXT NOT NULL,          -- news / analysis / interaction / reflection
        title TEXT,
        content TEXT NOT NULL,
        source TEXT DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        metadata TEXT DEFAULT '{}'
    );

    -- FTS5 全文搜索索引
    CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
        title, content, doc_type, source,
        content=kb_documents,
        content_rowid=id,
        tokenize='unicode61'
    );

    -- 触发器：同步 FTS 索引
    CREATE TRIGGER IF NOT EXISTS kb_ai AFTER INSERT ON kb_documents BEGIN
        INSERT INTO kb_fts(rowid, title, content, doc_type, source)
        VALUES (new.id, new.title, new.content, new.doc_type, new.source);
    END;

    CREATE TRIGGER IF NOT EXISTS kb_ad AFTER DELETE ON kb_documents BEGIN
        INSERT INTO kb_fts(kb_fts, rowid, title, content, doc_type, source)
        VALUES ('delete', old.id, old.title, old.content, old.doc_type, old.source);
    END;

    CREATE TRIGGER IF NOT EXISTS kb_au AFTER UPDATE ON kb_documents BEGIN
        INSERT INTO kb_fts(kb_fts, rowid, title, content, doc_type, source)
        VALUES ('delete', old.id, old.title, old.content, old.doc_type, old.source);
        INSERT INTO kb_fts(rowid, title, content, doc_type, source)
        VALUES (new.id, new.title, new.content, new.doc_type, new.source);
    END;

    CREATE INDEX IF NOT EXISTS idx_kb_type ON kb_documents(doc_type);
    CREATE INDEX IF NOT EXISTS idx_kb_created ON kb_documents(created_at);
    """)
    conn.commit()
    conn.close()


# ──────────────── 知识库操作 ────────────────

class KnowledgeBase:
    """知识库管理器（基于 SQLite FTS5）"""

    def __init__(self):
        init_kb()

    def _make_id(self, text: str, prefix: str = "") -> str:
        h = hashlib.md5(text.encode()).hexdigest()[:12]
        return f"{prefix}_{h}" if prefix else h

    def _add_doc(self, doc_type: str, title: str, content: str,
                 source: str = "", metadata: Dict = None):
        conn = get_kb_conn()
        doc_id = self._make_id(f"{title}{content}", doc_type)
        meta_str = json.dumps(metadata or {}, ensure_ascii=False)
        try:
            conn.execute(
                """INSERT OR REPLACE INTO kb_documents
                   (doc_id, doc_type, title, content, source, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (doc_id, doc_type, title, content, source, meta_str),
            )
            conn.commit()
        except Exception as e:
            print(f"⚠️ 写入知识库失败: {e}")
        finally:
            conn.close()

    def add_news(self, title: str, summary: str, source: str = "",
                 published: str = "", metadata: Dict = None):
        meta = {"published": published or datetime.now().isoformat()}
        if metadata:
            meta.update(metadata)
        self._add_doc("news", title, summary, source, meta)

    def add_analysis(self, title: str, content: str, target: str = "",
                     score: float = 0, metadata: Dict = None):
        meta = {"target": target, "score": score}
        if metadata:
            meta.update(metadata)
        self._add_doc("analysis", title, content, "investor", meta)

    def add_interaction(self, query: str, response: str, session_id: str = "",
                        quality: float = 0):
        title = query[:100]
        content = f"Q: {query}\nA: {response}"
        self._add_doc("interaction", title, content, session_id,
                      {"quality": quality})

    def add_reflection(self, title: str, content: str, report_type: str = "daily"):
        self._add_doc("reflection", title, content, "system",
                      {"report_type": report_type})

    def search(self, query: str, doc_type: str = None,
               n_results: int = 5) -> List[Dict]:
        """搜索知识库（LIKE 模糊匹配，支持中文）"""
        conn = get_kb_conn()
        terms = query.strip().split()
        if not terms:
            return []

        # 构建 LIKE 条件：任意 term 匹配 title 或 content
        conditions = []
        params = []
        for t in terms:
            if len(t) > 0:
                conditions.append("(title LIKE ? OR content LIKE ?)")
                params.extend([f"%{t}%", f"%{t}%"])

        if not conditions:
            return []

        where = " OR ".join(conditions)
        if doc_type:
            where = f"({where}) AND doc_type = ?"
            params.append(doc_type)

        # 按匹配数排序（匹配更多 term 的排前面）
        score_expr = " + ".join(
            f"(CASE WHEN title LIKE '%{t}%' OR content LIKE '%{t}%' THEN 1 ELSE 0 END)"
            for t in terms if len(t) > 0
        )

        sql = f"""SELECT *, ({score_expr}) as match_score
                  FROM kb_documents
                  WHERE {where}
                  ORDER BY match_score DESC, created_at DESC
                  LIMIT ?"""
        params.append(n_results)

        try:
            rows = conn.execute(sql, params).fetchall()
        except Exception as e:
            print(f"⚠️ 搜索失败: {e}")
            rows = []

        conn.close()
        results = []
        for r in rows:
            d = dict(r)
            d["relevance"] = d.get("match_score", 1) / max(len(terms), 1)
            if d.get("metadata"):
                try:
                    d["metadata"] = json.loads(d["metadata"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results

    def search_all(self, query: str, n_per_type: int = 3) -> Dict[str, List[Dict]]:
        """跨类别搜索"""
        results = {}
        for doc_type in ["news", "analysis", "interaction", "reflection"]:
            results[doc_type] = self.search(query, doc_type, n_per_type)
        return results

    def get_recent(self, doc_type: str = None, limit: int = 10) -> List[Dict]:
        """获取最近的文档"""
        conn = get_kb_conn()
        if doc_type:
            rows = conn.execute(
                "SELECT * FROM kb_documents WHERE doc_type=? ORDER BY created_at DESC LIMIT ?",
                (doc_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM kb_documents ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def count(self, doc_type: str = None) -> int:
        conn = get_kb_conn()
        if doc_type:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM kb_documents WHERE doc_type=?",
                (doc_type,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as cnt FROM kb_documents").fetchone()
        conn.close()
        return row["cnt"] if row else 0


# ──────────────── RAG 检索增强 ────────────────

def build_rag_context(query: str, n_results: int = 5) -> str:
    """构建 RAG 上下文"""
    kb = KnowledgeBase()
    all_results = kb.search_all(query, n_per_type=n_results)

    context_parts = []

    # 相关分析报告
    analyses = all_results.get("analysis", [])
    if analyses:
        context_parts.append("## 📊 相关历史分析")
        for r in analyses[:3]:
            context_parts.append(f"- {r.get('title', '')}: {r.get('content', '')[:300]}")

    # 相关新闻
    news = all_results.get("news", [])
    if news:
        context_parts.append("\n## 📰 相关新闻")
        for r in news[:3]:
            context_parts.append(f"- {r.get('title', '')}: {r.get('content', '')[:200]}")

    # 相关问答
    interactions = all_results.get("interaction", [])
    if interactions:
        context_parts.append("\n## 💬 相关历史问答")
        for r in interactions[:2]:
            context_parts.append(f"- {r.get('content', '')[:300]}")

    # 相关反思
    reflections = all_results.get("reflection", [])
    if reflections:
        context_parts.append("\n## 🔍 相关反思")
        for r in reflections[:2]:
            context_parts.append(f"- {r.get('content', '')[:300]}")

    # 从结构化数据库获取规则
    rules = db.get_rules(enabled_only=True)
    if rules:
        context_parts.append("\n## 📏 当前投资规则")
        for rule in rules[:10]:
            context_parts.append(f"- [{rule['category']}] {rule['rule_text']} (置信度: {rule['confidence']:.0%})")

    # 策略权重
    strategies = db.get_strategies(enabled_only=True)
    if strategies:
        context_parts.append("\n## ⚖️ 当前策略权重")
        for s in strategies:
            context_parts.append(f"- {s['name']}: {s['weight']:.0%} (胜率: {s['win_rate']:.1f}%)")

    return "\n".join(context_parts) if context_parts else ""


def build_few_shot_prompt() -> str:
    """构建 few-shot 示例 prompt"""
    examples = db.get_few_shot_examples("good_analysis", limit=3)
    if not examples:
        return ""
    parts = ["## 📝 分析示例（请参考以下风格）"]
    for ex in examples:
        parts.append(f"\n### 场景：{ex['scenario']}")
        parts.append(f"**用户问：** {ex['input_text'][:200]}")
        parts.append(f"**分析：** {ex['output_text'][:500]}")
    return "\n".join(parts)


# ──────────────── 自动记忆写入 ────────────────

def auto_memorize_interaction(user_query: str, response: str,
                               session_id: str = "", model: str = "",
                               prediction_id: int = None) -> int:
    """每次交互自动写入记忆"""
    has_pred = prediction_id is not None
    iid = db.save_interaction(
        user_query=user_query,
        response=response,
        session_id=session_id,
        model_used=model,
        has_prediction=has_pred,
        prediction_id=prediction_id,
    )
    kb = KnowledgeBase()
    kb.add_interaction(user_query, response, session_id)
    return iid


def auto_memorize_news(news_list: List[Dict]):
    """批量写入新闻到知识库"""
    kb = KnowledgeBase()
    count = 0
    for n in news_list:
        title = n.get("title", "")
        summary = n.get("summary", n.get("content", ""))
        if title:
            kb.add_news(title, summary, n.get("source", ""), n.get("published", ""))
            count += 1
    print(f"  📝 已写入 {count} 条新闻到知识库")


def auto_memorize_analysis(title: str, content: str, target: str = "",
                            score: float = 0.5):
    """写入分析报告到知识库"""
    kb = KnowledgeBase()
    kb.add_analysis(title, content, target, score)
    print(f"  📝 分析已写入知识库: {title[:50]}")


if __name__ == "__main__":
    db.init_db()
    init_kb()
    print("✅ 知识库初始化完成")

    kb = KnowledgeBase()
    kb.add_news("上证指数收涨1.5%", "A股市场今日全面上涨，上证指数收涨1.5%至3250点", "测试")
    kb.add_analysis("上证指数日线分析", "上证指数站上3200点，MACD金叉，均线多头排列", "sh000001")

    results = kb.search("上证指数")
    print(f"\n搜索结果: {len(results)} 条")
    for r in results:
        print(f"  - [{r.get('doc_type')}] {r.get('title', '')[:60]}")

    print(f"\n知识库统计:")
    print(f"  新闻: {kb.count('news')}")
    print(f"  分析: {kb.count('analysis')}")
    print(f"  问答: {kb.count('interaction')}")
    print(f"  总计: {kb.count()}")

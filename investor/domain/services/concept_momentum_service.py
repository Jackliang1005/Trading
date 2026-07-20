#!/usr/bin/env python3
"""Map fresh event themes to hot concepts and rank their stocks by cached momentum."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence


CONCEPT_DB = Path("/root/qmttrader_v2/concept_db/concepts.db")
MOMENTUM_CACHE = Path("/root/.openclaw/workspace/investor/data/mootdx_finance_cache.json")

THEME_CONCEPT_KEYWORDS = {
    "AI算力": ["算力", "人工智能", "AI", "大模型", "智算", "数据中心", "液冷", "服务器", "光模块"],
    "AI Chips": ["半导体", "芯片", "存储", "先进封装", "HBM", "光刻"],
    "半导体": ["半导体", "芯片", "存储", "先进封装", "HBM", "光刻"],
    "机器人": ["机器人", "具身智能", "减速器"],
    "华为": ["华为", "鸿蒙", "昇腾", "鲲鹏"],
    "Energy Commodities": ["油气", "石油", "黄金", "有色", "煤炭", "航运", "化工"],
    "Geopolitics": ["军工", "航运", "油气", "黄金"],
    "Global Macro": ["银行", "高股息", "黄金"],
    "Global EV": ["新能源汽车", "锂电池", "固态电池", "汽车零部件"],
}


def _normal_code(value: Any) -> str:
    code = str(value or "").strip().upper().split(".", 1)[0]
    if len(code) != 6 or not code.isdigit():
        return ""
    suffix = "SH" if code[0] in "569" else "SZ" if code[0] in "0123" else ""
    return f"{code}.{suffix}" if suffix else ""


def _theme_names(theme_heat: Sequence[Any]) -> List[str]:
    names: List[str] = []
    for item in theme_heat or []:
        name = item[0] if isinstance(item, (list, tuple)) and item else item.get("theme") if isinstance(item, dict) else item
        text = str(name or "").strip()
        if text and text not in names:
            names.append(text)
    return names


def _trend(momentum_20d: float, momentum_60d: float) -> str:
    if momentum_20d > 25:
        return "overheated"
    if momentum_20d >= 8 and momentum_60d >= 12:
        return "strong_up"
    if momentum_20d >= 5 and momentum_20d >= momentum_60d + 3:
        return "improving"
    if momentum_20d > 0 and momentum_60d > 0:
        return "positive"
    return "weak"


def _load_momentum(path: Path) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: Dict[str, Dict[str, Any]] = {}
    for item in payload.get("rows") or []:
        code = _normal_code(item.get("code"))
        if code:
            rows[code] = item
    return rows, payload


def build_concept_momentum_candidates(
    theme_heat: Sequence[Any],
    report_date: str = "",
    top_n: int = 8,
    concept_db: Path = CONCEPT_DB,
    momentum_cache: Path = MOMENTUM_CACHE,
) -> Dict[str, Any]:
    themes = _theme_names(theme_heat)
    keywords = []
    for theme in themes:
        keywords.extend(THEME_CONCEPT_KEYWORDS.get(theme, []))
    keywords = list(dict.fromkeys(keywords))
    if not keywords:
        return {"available": False, "reason": "no_theme_concept_mapping", "themes": themes, "candidates": []}
    if not concept_db.exists():
        return {"available": False, "reason": "concept_db_missing", "themes": themes, "candidates": []}
    if not momentum_cache.exists():
        return {"available": False, "reason": "momentum_cache_missing", "themes": themes, "candidates": []}

    target = str(report_date or date.today().isoformat()).replace("-", "")[:8]
    with sqlite3.connect(str(concept_db)) as conn:
        row = conn.execute("SELECT max(date) FROM hot_concepts WHERE date <= ?", (target,)).fetchone()
        concept_date = str((row or [""])[0] or "")
        concepts = conn.execute(
            "SELECT concept_code, concept_name, limit_up_num, change FROM hot_concepts WHERE date = ?",
            (concept_date,),
        ).fetchall() if concept_date else []
        matched = []
        for code, name, limit_up_num, change in concepts:
            concept_name = str(name or "")
            hits = [keyword for keyword in keywords if keyword.lower() in concept_name.lower()]
            if hits:
                matched.append({"code": str(code), "name": concept_name, "limit_up_num": int(limit_up_num or 0), "change": float(change or 0), "keywords": hits})
        matched.sort(key=lambda item: (item["limit_up_num"], item["change"]), reverse=True)
        matched = matched[:20]
        concept_codes = [item["code"] for item in matched]
        stocks = []
        if concept_codes:
            placeholders = ",".join("?" for _ in concept_codes)
            stocks = conn.execute(
                f"SELECT concept_code, stock_code, stock_name, reason_type FROM concept_stocks WHERE date = ? AND concept_code IN ({placeholders})",
                [concept_date, *concept_codes],
            ).fetchall()

    momentum, cache_meta = _load_momentum(momentum_cache)
    momentum_dates = sorted({str(item.get("momentum_as_of") or "")[:10] for item in momentum.values() if item.get("momentum_as_of")})
    momentum_date = momentum_dates[-1] if momentum_dates else ""
    expected_date = f"{concept_date[:4]}-{concept_date[4:6]}-{concept_date[6:8]}" if len(concept_date) == 8 else ""
    if not concept_date or not matched:
        return {"available": False, "reason": "no_matching_hot_concept", "themes": themes, "keywords": keywords, "concept_date": concept_date, "candidates": []}
    if momentum_date != expected_date:
        return {
            "available": False,
            "reason": "concept_momentum_date_mismatch",
            "themes": themes,
            "concept_date": concept_date,
            "momentum_date": momentum_date,
            "candidates": [],
        }

    concept_by_code = {item["code"]: item for item in matched}
    stock_rows: Dict[str, Dict[str, Any]] = {}
    for concept_code, raw_code, stock_name, reason_type in stocks:
        code = _normal_code(raw_code)
        item = momentum.get(code)
        if not code or not item or "ST" in str(stock_name or item.get("name") or "").upper():
            continue
        try:
            m20 = float(item["momentum_20d"])
            m60 = float(item["momentum_60d"])
        except (KeyError, TypeError, ValueError):
            continue
        row = stock_rows.setdefault(code, {
            "code": code,
            "name": str(stock_name or item.get("name") or code),
            "momentum_20d": m20,
            "momentum_60d": m60,
            "momentum_as_of": str(item.get("momentum_as_of") or ""),
            "concepts": [],
            "concept_evidence": [],
            "reasons": [],
        })
        concept = concept_by_code.get(str(concept_code))
        if concept and concept["name"] not in row["concepts"]:
            row["concepts"].append(concept["name"])
            row["concept_evidence"].append({"name": concept["name"], "change": concept["change"], "limit_up_num": concept["limit_up_num"]})
        reason = str(reason_type or "").strip()
        if reason and reason not in row["reasons"]:
            row["reasons"].append(reason)

    eligible = []
    trend_counts: Dict[str, int] = defaultdict(int)
    for item in stock_rows.values():
        trend = _trend(item["momentum_20d"], item["momentum_60d"])
        item["trend"] = trend
        item["best_concept_change"] = max((float(value.get("change") or 0) for value in item.get("concept_evidence") or []), default=-100.0)
        trend_counts[trend] += 1
        item["momentum_score"] = round(
            max(0.0, min(100.0, 50 + item["momentum_20d"] * 1.2 + item["momentum_60d"] * 0.4 + min(len(item["concepts"]), 3) * 3 + item["best_concept_change"])),
            2,
        )
        if trend in {"strong_up", "improving", "positive"} and item["momentum_20d"] >= 3 and item["momentum_60d"] >= 0 and item["best_concept_change"] > -2.0:
            eligible.append(item)
    eligible.sort(key=lambda item: (item["momentum_score"], item["momentum_20d"], item["momentum_60d"]), reverse=True)
    return {
        "available": True,
        "source": "qmttrader_v2.concepts.db+mootdx_finance_cache",
        "themes": themes,
        "keywords": keywords,
        "concept_date": concept_date,
        "momentum_date": momentum_date,
        "momentum_coverage": int(cache_meta.get("momentum_coverage") or 0),
        "universe_size": int(cache_meta.get("universe_size") or 0),
        "matched_concepts": matched,
        "concept_stock_count": len(stock_rows),
        "trend_counts": dict(trend_counts),
        "candidates": eligible[: max(1, top_n)],
    }

from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
import time
from html import unescape
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib import import_module
from pathlib import Path
from typing import Dict, List

import requests

from .industry_normalizer import normalize_industry_name


class OpenClawChinaStockDataSource:
    """Read-only datasource via openclaw-data-china-stock plugin."""

    def __init__(self) -> None:
        self.plugin_root = Path.home() / ".openclaw" / "extensions" / "openclaw-data-china-stock"
        if self.plugin_root.exists():
            sys.path.insert(0, str(self.plugin_root))
            sys.path.insert(0, str(self.plugin_root / "plugins"))
        self.cache_file = Path(__file__).resolve().parents[2] / "trading_data" / "longterm" / "industry_cache.json"
        self.ths_sector_cache_file = (
            Path(__file__).resolve().parents[2] / "trading_data" / "longterm" / "ths_sector_cache.json"
        )
        self.concept_db_path = Path("/root/qmttrader/concept_db/concepts.db")
        self._lock = threading.RLock()
        self._last_concept_request = 0.0

    def _load_tool(self, module_path: str, func: str):
        if not self.plugin_root.exists():
            return None
        try:
            module = import_module(module_path)
            return getattr(module, func, None)
        except Exception:
            return None

    def _normalize_code(self, code: str) -> str:
        text = str(code or "").strip()
        upper = text.upper()
        if "." in upper:
            return upper.split(".")[0]
        if upper.startswith(("SH", "SZ", "BJ")) and len(upper) > 2:
            return upper[2:]
        return upper

    def fetch_hot_concept_candidates(
        self,
        *,
        top_concepts: int = 5,
        max_per_concept: int = 8,
        max_total: int = 30,
    ) -> List[Dict]:
        """从概念数据库提取热门概念龙头股，用于扩展选股universe。

        流程：
        1. 取最新日期的 top_concepts 个热门概念（按涨停数+涨幅排序）
        2. 每个概念取前 max_per_concept 只成分股（按涨停次数排序）
        3. 去重，最多返回 max_total 只
        """
        if not self.concept_db_path.exists():
            return []
        try:
            with sqlite3.connect(str(self.concept_db_path)) as conn:
                cursor = conn.cursor()
                # 1. Get the latest date
                cursor.execute("SELECT MAX(date) FROM hot_concepts")
                row = cursor.fetchone()
                if not row or not row[0]:
                    return []
                latest_date = str(row[0])
                # 2. Get top concepts sorted by heat (limit_up_num + change)
                cursor.execute(
                    """
                    SELECT concept_code, concept_name, limit_up_num, change
                    FROM hot_concepts
                    WHERE date = ?
                    ORDER BY (limit_up_num * 2.0 + change) DESC
                    LIMIT ?
                    """,
                    (latest_date, int(top_concepts)),
                )
                top_concept_rows = cursor.fetchall()
                if not top_concept_rows:
                    return []
                # 3. For each top concept, get constituent stocks
                all_stocks: Dict[str, Dict] = {}
                concept_names: Dict[str, List[str]] = {}  # code -> [concept names]
                for ccode, cname, limit_up_num, change in top_concept_rows:
                    cursor.execute(
                        """
                        SELECT cs.stock_code, cs.stock_name,
                               COUNT(*) OVER (PARTITION BY cs.stock_code) as concept_count
                        FROM concept_stocks cs
                        WHERE cs.concept_code = ? AND cs.date = ?
                        LIMIT ?
                        """,
                        (str(ccode), latest_date, int(max_per_concept)),
                    )
                    stock_rows = cursor.fetchall()
                    for srow in stock_rows:
                        scode = self._normalize_code(str(srow[0] or ""))
                        sname = str(srow[1] or "").strip()
                        if not scode or not sname:
                            continue
                        if scode not in all_stocks:
                            all_stocks[scode] = {
                                "code": scode,
                                "name": sname,
                                "heat_score": 0.0,
                                "hot_concepts": [],
                            }
                            concept_names.setdefault(scode, [])
                        cname_clean = str(cname or "").strip()
                        if cname_clean and cname_clean not in concept_names.get(scode, []):
                            concept_names[scode].append(cname_clean)
                        # Accumulate heat score weighted by concept rank
                        raw_strength = float(limit_up_num or 0) * 2.0 + float(change or 0)
                        all_stocks[scode]["heat_score"] += raw_strength
                # 4. Cap heat scores, sort, deduplicate
                for scode in all_stocks:
                    all_stocks[scode]["hot_concepts"] = concept_names.get(scode, [])[:5]
                max_heat = max((item["heat_score"] for item in all_stocks.values()), default=0.0)
                if max_heat > 0:
                    for scode in all_stocks:
                        raw = all_stocks[scode]["heat_score"]
                        all_stocks[scode]["heat_score"] = round(min(100.0, (raw / max_heat) * 100.0), 2)
                result = sorted(all_stocks.values(), key=lambda x: x["heat_score"], reverse=True)
                return result[: int(max_total)]
        except Exception:
            return []

    def fetch_sector_rotation_candidates(
        self,
        *,
        top_sectors: int = 8,
        max_per_sector: int = 5,
        max_total: int = 30,
    ) -> List[Dict]:
        """板块轮动预判选股：先定强势板块，再全市场筛选未涨停但有动能的个股。

        Phase A: 识别强势板块（综合涨跌幅+资金净流入+排名+双确认）
        Phase B: 全市场批量报价 → 涨幅/流动性过滤
        Phase C: 概念匹配（通过概念DB匹配强势板块）
        Phase D: 综合评分排序
        """
        # ── Phase A: 识别强势板块 ──────────────────────────────────
        sector_tool = self._load_tool("plugins.data_collection.sector", "tool_fetch_sector_data")
        if not sector_tool:
            return []

        all_sector_rows: List[Dict] = []
        for stype in ("industry", "concept"):
            try:
                resp = sector_tool(sector_type=stype, period="today")
            except Exception:
                continue
            if not isinstance(resp, dict) or str(resp.get("status", "")) != "success":
                continue
            rows = resp.get("all_data")
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and str(row.get("sector_name", "")).strip():
                        row_copy = dict(row)
                        row_copy["sector_type"] = stype
                        all_sector_rows.append(row_copy)

        if not all_sector_rows:
            return []

        # Deduplicate by sector_name
        seen_names: set = set()
        deduped_sectors: List[Dict] = []
        for row in all_sector_rows:
            name = str(row.get("sector_name", "")).strip()
            if name not in seen_names:
                seen_names.add(name)
                deduped_sectors.append(row)

        # Normalize and score sectors
        sector_changes = [float(s.get("change_percent", 0) or 0) for s in deduped_sectors]
        sector_inflows = [float(s.get("net_inflow", 0) or 0) for s in deduped_sectors]
        max_change = max(abs(c) for c in sector_changes) if sector_changes else 1.0
        max_inflow = max(abs(n) for n in sector_inflows) if sector_inflows else 1.0

        total = len(deduped_sectors)
        for idx, s in enumerate(deduped_sectors):
            chg = float(s.get("change_percent", 0) or 0)
            ni = float(s.get("net_inflow", 0) or 0)
            rank = int(s.get("rank", idx + 1))

            change_score = chg / max(max_change, 0.01)
            inflow_score = ni / max(max_inflow, 1.0)
            rank_score = max(0.0, 1.0 - (rank - 1) / max(total, 1))
            dual_score = 1.0 if (chg > 0 and ni > 0) else (0.5 if (chg > 0 or ni > 0) else 0.0)

            composite = (
                change_score * 0.40
                + inflow_score * 0.30
                + rank_score * 0.15
                + dual_score * 0.15
            )
            s["composite_score"] = round(composite, 4)
            s["change_pct"] = round(chg, 2)
            s["net_inflow_val"] = round(ni, 2)

        deduped_sectors.sort(key=lambda x: float(x.get("composite_score", 0)), reverse=True)
        strong_sectors = deduped_sectors[: max(1, int(top_sectors))]
        strong_names = {str(s["sector_name"]).strip() for s in strong_sectors}

        # ── Phase B: 全市场批量报价 → 涨幅/流动性过滤 ─────────────
        universe_tool = self._load_tool(
            "plugins.data_collection.stock.fundamentals_extended", "tool_fetch_a_share_universe"
        )
        if not universe_tool:
            return []

        try:
            universe_resp = universe_tool(provider_preference="standard")
        except Exception:
            return []

        all_stock_rows = []
        if isinstance(universe_resp, dict) and bool(universe_resp.get("success")):
            data = universe_resp.get("data")
            if isinstance(data, list):
                all_stock_rows = data

        if not all_stock_rows:
            return []

        # Extract codes (skip ST, BSE, NEEQ)
        all_codes = []
        for row in all_stock_rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code", "")).strip()
            name = str(row.get("name", "")).strip()
            if not code or "ST" in name:
                continue
            # Skip Beijing Stock Exchange (920xxx) and NEEQ (8xxxxx, 4xxxxx)
            if code.startswith(("920", "8", "4")):
                continue
            all_codes.append(code)
        # Batch-fetch quotes (200 codes per batch)
        quotes_all: Dict[str, Dict] = {}
        batch_size = 200
        for i in range(0, len(all_codes), batch_size):
            batch = all_codes[i : i + batch_size]
            try:
                batch_quotes = self.fetch_quotes(batch)
                quotes_all.update(batch_quotes)
            except Exception:
                continue

        if not quotes_all:
            return []

        # Filter: change 2-8%, amount > 50M (no turnover data available from quotes)
        filtered_codes: List[str] = []
        quote_by_code: Dict[str, Dict] = {}
        for code, q in quotes_all.items():
            chg_pct = float(q.get("change_percent", 0) or 0)
            amount = float(q.get("amount", 0) or 0)
            price = float(q.get("price", 0) or 0)
            if price <= 0:
                continue
            # Limit-up exclusion
            is_kcb = code.startswith("688")
            is_cyb = code.startswith("300")
            limit_up_threshold = 19.5 if (is_kcb or is_cyb) else 9.5
            if chg_pct >= limit_up_threshold:
                continue
            if not (2.0 <= chg_pct <= 8.0):
                continue
            if amount < 50_000_000:
                continue
            filtered_codes.append(code)
            quote_by_code[code] = q

        if not filtered_codes:
            return []

        # ── Phase C: 概念匹配 ──────────────────────────────────
        # Strategy: use all available local data (cache + concept DB + industry cache)
        # Fall back to THS web lookup for top candidates without local data

        # 1. Gather all available local tags for filtered codes
        code_tags: Dict[str, List[str]] = {}
        ths_cache = self._read_ths_sector_cache()
        industry_cache = self._read_industry_cache()

        for code in filtered_codes:
            tags: List[str] = []
            # THS sector cache
            if code in ths_cache:
                tags.extend(ths_cache.get(code, []))
            # Industry cache
            ind = industry_cache.get(code, "")
            if ind:
                tags.append(ind)
            code_tags[code] = tags

        # 2. Also query concept DB for any direct matches
        if self.concept_db_path.exists():
            try:
                with sqlite3.connect(str(self.concept_db_path)) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT MAX(date) FROM concept_stocks")
                    row = cursor.fetchone()
                    latest_date = str(row[0]) if row and row[0] else ""
                    if latest_date:
                        placeholders = ",".join(["?"] * len(filtered_codes))
                        cursor.execute(
                            f"""
                            SELECT cs.stock_code, hc.concept_name
                            FROM concept_stocks cs
                            JOIN hot_concepts hc ON cs.concept_code = hc.concept_code
                                AND cs.date = hc.date
                            WHERE cs.date = ?
                                AND cs.stock_code IN ({placeholders})
                            """,
                            [latest_date, *filtered_codes],
                        )
                        for srow in cursor.fetchall():
                            scode = self._normalize_code(str(srow[0] or ""))
                            cname = str(srow[1] or "").strip()
                            if scode and cname:
                                code_tags.setdefault(scode, []).append(cname)
            except Exception:
                pass

        # 3. Match locally; identify codes that need web lookup
        matched_codes: set = set()
        code_sector: Dict[str, str] = {}  # code -> matched strong sector name
        for code in filtered_codes:
            tags = code_tags.get(code, [])
            for sname in strong_names:
                for tag in tags:
                    if _fuzzy_match_sector(tag, {sname}):
                        code_sector[code] = sname
                        matched_codes.add(code)
                        break
                if code in matched_codes:
                    break

        # 4. For unmatched codes, fetch THS sectors from web (limited batch)
        unmatched = [c for c in filtered_codes if c not in matched_codes]
        web_fetch_limit = min(30, len(unmatched))  # Cap at ~15s of web requests
        if unmatched:
            web_batch = unmatched[:web_fetch_limit]
            try:
                web_sectors = self.fetch_ths_sectors(web_batch, refresh=True)
                for code, sector_tags in web_sectors.items():
                    for sname in strong_names:
                        for tag in sector_tags:
                            if _fuzzy_match_sector(tag, {sname}):
                                code_sector[code] = sname
                                break
                        if code in code_sector:
                            break
            except Exception:
                pass

        # 5. Build candidate list
        candidates: List[Dict] = []
        for code in filtered_codes:
            matched_sector = code_sector.get(code, "")
            if not matched_sector:
                continue

            sector_momentum = 0.0
            for s in strong_sectors:
                if str(s.get("sector_name", "")).strip() == matched_sector:
                    sector_momentum = float(s.get("composite_score", 0))
                    break

            q = quote_by_code[code]
            candidates.append({
                "code": code,
                "name": str(q.get("name") or code),
                "change_pct": round(float(q.get("change_percent", 0) or 0), 2),
                "amount": float(q.get("amount", 0) or 0),
                "matched_sector": matched_sector,
                "sector_momentum": sector_momentum,
            })

        if not candidates:
            return []

        # ── Phase D: 综合评分 ────────────────────────────────────
        max_amount = max(c["amount"] for c in candidates)

        for c in candidates:
            chg = c["change_pct"]
            amount = c["amount"]
            sector_mom = c["sector_momentum"]

            sector_momentum_score = min(1.0, max(0.0, sector_mom))

            # relative_strength: optimal range 2-7%, peak at ~4.5%
            if 2.0 <= chg <= 7.0:
                dist_from_peak = abs(chg - 4.5) / 2.5
                relative_strength = max(0.3, 1.0 - dist_from_peak)
            elif 1.0 <= chg < 2.0:
                relative_strength = 0.4
            elif 7.0 < chg <= 8.0:
                relative_strength = 0.5
            else:
                relative_strength = 0.2

            # liquidity_quality: amount-based
            amount_score = min(1.0, amount / max(max_amount, 1.0))
            liquidity_quality = amount_score

            # technical_setup: price ideally in 2-7% sweet spot
            tech_score = 1.0 - abs(chg - 4.5) / 3.5
            tech_score = max(0.0, min(1.0, tech_score))

            momentum_score = (
                sector_momentum_score * 0.40
                + relative_strength * 0.30
                + liquidity_quality * 0.20
                + tech_score * 0.10
            )
            c["momentum_score"] = round(momentum_score * 100.0, 1)

        # Sort by momentum_score descending
        candidates.sort(key=lambda x: float(x.get("momentum_score", 0)), reverse=True)

        # Deduplicate by code
        seen_codes: set = set()
        deduped: List[Dict] = []
        for c in candidates:
            if c["code"] not in seen_codes:
                seen_codes.add(c["code"])
                deduped.append(c)

        # Cap results per sector
        sector_counts: Dict[str, int] = {}
        capped: List[Dict] = []
        for c in deduped:
            sec = c["matched_sector"]
            cnt = sector_counts.get(sec, 0)
            if cnt >= max(1, int(max_per_sector)):
                continue
            sector_counts[sec] = cnt + 1
            capped.append(c)
            if len(capped) >= max(1, int(max_total)):
                break

        return capped

    def _read_industry_cache(self) -> Dict[str, str]:
        try:
            raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {}
            result = {}
            for code, industry in raw.items():
                c = self._normalize_code(str(code))
                i = normalize_industry_name(str(industry or "").strip())
                if c and i:
                    result[c] = i
            return result
        except Exception:
            return {}

    def _write_industry_cache(self, payload: Dict[str, str]) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.cache_file)

    def _read_ths_sector_cache(self) -> Dict[str, List[str]]:
        try:
            raw = json.loads(self.ths_sector_cache_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {}
            out: Dict[str, List[str]] = {}
            for code, sectors in raw.items():
                c = self._normalize_code(str(code))
                if not c:
                    continue
                if isinstance(sectors, list):
                    cleaned = [str(x).strip() for x in sectors if str(x).strip()]
                    out[c] = cleaned
            return out
        except Exception:
            return {}

    def _write_ths_sector_cache(self, payload: Dict[str, List[str]]) -> None:
        self.ths_sector_cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ths_sector_cache_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.ths_sector_cache_file)

    def _fetch_industry_single(self, code: str) -> str:
        # Prefer AkShare's individual profile route when available.
        try:
            import akshare as ak  # type: ignore

            df = ak.stock_individual_info_em(symbol=code)
            if df is None or df.empty:
                return ""
            records = df.to_dict(orient="records")
            for row in records:
                item = str((row or {}).get("item", "") or "").strip()
                val = str((row or {}).get("value", "") or "").strip()
                if not item or not val:
                    continue
                if item in {"行业", "所属行业", "行业分类", "申万行业", "所属申万行业"}:
                    return val
        except Exception:
            return ""
        return ""

    def _fetch_ths_sectors_from_db(self, code: str) -> List[str]:
        if not self.concept_db_path.exists():
            return []
        try:
            with sqlite3.connect(str(self.concept_db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT DISTINCT hc.concept_name
                    FROM concept_stocks cs
                    JOIN hot_concepts hc ON cs.concept_code = hc.concept_code
                    WHERE cs.stock_code = ?
                    """,
                    (code,),
                )
                rows = cursor.fetchall()
            return [str(row[0]).strip() for row in rows if row and str(row[0]).strip()]
        except Exception:
            return []

    def _extract_ths_sectors_from_html(self, html: str) -> List[str]:
        class _THSConceptParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.capture = False
                self.parts: List[str] = []
                self.out: List[str] = []

            def handle_starttag(self, tag, attrs):
                if tag.lower() != "td":
                    return
                attr_map = {str(k).lower(): str(v) for k, v in attrs}
                cls = str(attr_map.get("class", "") or "")
                if "gnName" in cls:
                    self.capture = True
                    self.parts = []

            def handle_data(self, data):
                if self.capture:
                    text = str(data or "").strip()
                    if text:
                        self.parts.append(text)

            def handle_endtag(self, tag):
                if tag.lower() == "td" and self.capture:
                    text = unescape("".join(self.parts)).strip()
                    if text:
                        self.out.append(text)
                    self.capture = False
                    self.parts = []

        parser = _THSConceptParser()
        try:
            parser.feed(str(html or ""))
        except Exception:
            return []
        cleaned = [str(x).strip() for x in parser.out if str(x).strip()]
        if cleaned:
            return cleaned
        # regex fallback for unexpected malformed HTML
        found = re.findall(r'class="gnName"[^>]*>\s*(.*?)\s*</td>', str(html or ""), re.DOTALL)
        return [str(x).strip() for x in found if str(x).strip()]

    def _fetch_ths_sectors_from_web(self, code: str) -> List[str]:
        with self._lock:
            now = time.time()
            if now - float(self._last_concept_request or 0.0) < 0.5:
                time.sleep(0.5)
            self._last_concept_request = time.time()
        url = f"https://basic.10jqka.com.cn/{code}/concept.html"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
            ),
            "Referer": "https://basic.10jqka.com.cn/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        delays = [0.0, 0.25, 0.5]
        for delay in delays:
            if delay > 0:
                time.sleep(delay)
            try:
                resp = requests.get(url, headers=headers, timeout=2.5, allow_redirects=False)
                if int(resp.status_code) != 200:
                    continue
                try:
                    if not resp.encoding:
                        resp.encoding = resp.apparent_encoding or "gbk"
                except Exception:
                    resp.encoding = "gbk"
                html = str(resp.text or "")
                cleaned = self._extract_ths_sectors_from_html(html)
                if cleaned:
                    return cleaned
            except Exception:
                continue
        return []

    def _normalize_trade_date_key(self, trade_date: str) -> str:
        digits = "".join(ch for ch in str(trade_date or "") if ch.isdigit())
        if len(digits) >= 8:
            return digits[:8]
        return ""

    def fetch_industries(self, codes: List[str], *, refresh: bool = False) -> Dict[str, str]:
        normalized = [self._normalize_code(x) for x in codes if str(x or "").strip()]
        if not normalized:
            return {}
        cache = {} if refresh else self._read_industry_cache()
        out: Dict[str, str] = {}
        updated = dict(cache)
        for code in normalized:
            if code in cache and str(cache.get(code, "")).strip():
                out[code] = str(cache[code]).strip()
                continue
            industry = self._fetch_industry_single(code)
            if industry:
                canonical = normalize_industry_name(industry)
                out[code] = canonical
                updated[code] = canonical
        if updated != cache:
            self._write_industry_cache(updated)
        return out

    def fetch_ths_sectors(self, codes: List[str], *, refresh: bool = False) -> Dict[str, List[str]]:
        normalized = [self._normalize_code(x) for x in codes if str(x or "").strip()]
        if not normalized:
            return {}
        cache = {} if refresh else self._read_ths_sector_cache()
        out: Dict[str, List[str]] = {}
        updated = dict(cache)
        for code in normalized:
            if code in cache and isinstance(cache.get(code), list):
                out[code] = list(cache.get(code) or [])
                continue
            sectors = self._fetch_ths_sectors_from_db(code)
            if not sectors:
                sectors = self._fetch_ths_sectors_from_web(code)
            cleaned = [str(x).strip() for x in sectors if str(x).strip()]
            out[code] = cleaned
            updated[code] = cleaned
        if updated != cache:
            self._write_ths_sector_cache(updated)
        return out

    def fetch_ths_hotness(
        self,
        codes: List[str],
        *,
        trade_date: str = "",
        limit_up_weight: float = 2.0,
        change_weight: float = 18.0,
    ) -> Dict[str, Dict]:
        normalized = [self._normalize_code(x) for x in codes if str(x or "").strip()]
        if not normalized:
            return {}
        if not self.concept_db_path.exists():
            return {}
        date_key = self._normalize_trade_date_key(trade_date)
        out: Dict[str, Dict] = {code: {"heat_score": 0.0, "hot_concepts": [], "hot_date": ""} for code in normalized}
        target_date = ""
        try:
            with sqlite3.connect(str(self.concept_db_path)) as conn:
                cursor = conn.cursor()
                if date_key:
                    cursor.execute("SELECT MAX(date) FROM hot_concepts WHERE date <= ?", (date_key,))
                    row = cursor.fetchone()
                    target_date = str((row or [""])[0] or "").strip()
                else:
                    cursor.execute("SELECT MAX(date) FROM hot_concepts")
                    row = cursor.fetchone()
                    target_date = str((row or [""])[0] or "").strip()
                if not target_date:
                    return out
                placeholders = ",".join(["?"] * len(normalized))
                sql = f"""
                    SELECT cs.stock_code, hc.concept_name, hc.limit_up_num, hc.change
                    FROM concept_stocks cs
                    JOIN hot_concepts hc
                      ON cs.concept_code = hc.concept_code
                     AND cs.date = hc.date
                    WHERE cs.date = ?
                      AND cs.stock_code IN ({placeholders})
                """
                cursor.execute(sql, [target_date, *normalized])
                rows = cursor.fetchall()
                if not rows:
                    # Fallback: for each stock use its latest concept date.
                    sql_latest = f"""
                        SELECT cs.stock_code, cs.date, hc.concept_name, hc.limit_up_num, hc.change
                        FROM concept_stocks cs
                        JOIN hot_concepts hc
                          ON cs.concept_code = hc.concept_code
                         AND cs.date = hc.date
                        WHERE cs.stock_code IN ({placeholders})
                          AND cs.date = (
                            SELECT MAX(cs2.date)
                            FROM concept_stocks cs2
                            WHERE cs2.stock_code = cs.stock_code
                          )
                    """
                    cursor.execute(sql_latest, [*normalized])
                    rows = cursor.fetchall()
        except Exception:
            return out

        raw_map: Dict[str, List[Dict]] = {code: [] for code in normalized}
        date_map: Dict[str, str] = {code: target_date for code in normalized}
        for row in rows:
            if not row:
                continue
            if len(row) == 4:
                code, concept_name, limit_up_val, change_val = row
                row_date = target_date
            else:
                code, row_date, concept_name, limit_up_val, change_val = row
            code = self._normalize_code(str(code or ""))
            if code not in raw_map:
                continue
            concept_name = str(concept_name or "").strip()
            limit_up_num = float(limit_up_val or 0.0)
            change = float(change_val or 0.0)
            strength = max(0.0, limit_up_num) * float(limit_up_weight) + max(0.0, change) * float(change_weight)
            raw_map[code].append(
                {
                    "concept": concept_name,
                    "limit_up_num": limit_up_num,
                    "change": change,
                    "strength": strength,
                }
            )
            if row_date:
                date_map[code] = str(row_date)

        # Fallback: if stock is not in concept_stocks hot constituents,
        # estimate heat by matching its THS concept tags to hot_concepts.
        missing_codes = [code for code in normalized if not raw_map.get(code)]
        if missing_codes:
            try:
                with sqlite3.connect(str(self.concept_db_path)) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT concept_name, limit_up_num, change FROM hot_concepts WHERE date = ?",
                        (target_date,),
                    )
                    hot_rows = cursor.fetchall()
                hot_concept_map: Dict[str, Dict] = {}
                for item in hot_rows:
                    if not item:
                        continue
                    name = str(item[0] or "").strip()
                    if not name:
                        continue
                    hot_concept_map[name] = {
                        "limit_up_num": float(item[1] or 0.0),
                        "change": float(item[2] or 0.0),
                    }
                sector_cache = self._read_ths_sector_cache()
                for code in missing_codes:
                    tags = [str(x).strip() for x in (sector_cache.get(code) or []) if str(x).strip()]
                    if not tags:
                        continue
                    matched: List[Dict] = []
                    for tag in tags:
                        best_name = ""
                        best_val = None
                        if tag in hot_concept_map:
                            best_name = tag
                            best_val = hot_concept_map[tag]
                        else:
                            for hot_name, hot_val in hot_concept_map.items():
                                if (tag in hot_name) or (hot_name in tag):
                                    best_name = hot_name
                                    best_val = hot_val
                                    break
                        if not best_name or not best_val:
                            continue
                        limit_up_num = float(best_val.get("limit_up_num", 0.0))
                        change = float(best_val.get("change", 0.0))
                        strength = max(0.0, limit_up_num) * float(limit_up_weight) + max(0.0, change) * float(change_weight)
                        matched.append(
                            {
                                "concept": best_name,
                                "limit_up_num": limit_up_num,
                                "change": change,
                                "strength": strength,
                            }
                        )
                    if matched:
                        matched_sorted = sorted(matched, key=lambda x: float(x.get("strength", 0.0)), reverse=True)
                        dedup: List[Dict] = []
                        seen = set()
                        for item in matched_sorted:
                            concept = str(item.get("concept") or "")
                            if concept in seen:
                                continue
                            seen.add(concept)
                            dedup.append(item)
                            if len(dedup) >= 3:
                                break
                        raw_map[code] = dedup
            except Exception:
                pass

        code_raw_sum: Dict[str, float] = {}
        for code in normalized:
            items = sorted(raw_map.get(code, []), key=lambda x: float(x.get("strength", 0.0)), reverse=True)[:3]
            raw_sum = float(sum(float(x.get("strength", 0.0)) for x in items))
            code_raw_sum[code] = raw_sum
            out[code] = {
                "heat_score": 0.0,
                "raw_strength": round(raw_sum, 4),
                "hot_concepts": [str(x.get("concept") or "").strip() for x in items if str(x.get("concept") or "").strip()],
                "hot_date": str(date_map.get(code, target_date) or ""),
            }

        max_raw = max(code_raw_sum.values()) if code_raw_sum else 0.0
        if max_raw > 0:
            for code in normalized:
                raw_sum = float(code_raw_sum.get(code, 0.0))
                out[code]["heat_score"] = round((raw_sum / max_raw) * 100.0, 3)
        return out

    def fetch_quotes(self, codes: List[str]) -> Dict[str, Dict]:
        codes = [self._normalize_code(item) for item in codes if str(item or "").strip()]
        if not codes:
            return {}
        tool = self._load_tool("plugins.data_collection.stock.fetch_realtime", "tool_fetch_stock_realtime")
        if not tool:
            return {}
        try:
            result = tool(stock_code=",".join(codes), mode="test", include_depth=False)
        except Exception:
            return {}
        data = (result or {}).get("data") if isinstance(result, dict) else None
        items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        quotes: Dict[str, Dict] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            code = self._normalize_code(str(item.get("stock_code") or item.get("code") or ""))
            if not code:
                continue
            price = float(item.get("current_price", item.get("price", 0)) or 0)
            pre_close = float(item.get("prev_close", item.get("pre_close", 0)) or 0)
            if price <= 0:
                continue
            change_pct = float(item.get("change_percent", 0) or 0)
            if change_pct == 0 and pre_close > 0:
                change_pct = (price - pre_close) / pre_close * 100
            quotes[code] = {
                "code": code,
                "name": str(item.get("name") or code),
                "price": round(price, 3),
                "pre_close": round(pre_close, 3),
                "change_percent": round(change_pct, 3),
                "volume": float(item.get("volume", 0) or 0),
                "amount": float(item.get("amount", 0) or 0),
                "turnover_rate": float(item.get("turnover_rate", item.get("turnover", 0)) or 0),
                "high": float(item.get("high", 0) or 0),
                "low": float(item.get("low", 0) or 0),
            }
        return quotes

    def fetch_daily_history(self, code: str, lookback_days: int = 60, end_date: str = "") -> Dict[str, List[float]]:
        code = self._normalize_code(code)
        if not code:
            return {}
        tool = self._load_tool("plugins.data_collection.stock.fetch_historical", "tool_fetch_stock_historical")
        if not tool:
            return {}
        kwargs = {"stock_code": code, "period": "daily", "lookback_days": max(20, int(lookback_days or 60))}
        if end_date:
            kwargs["end_date"] = str(end_date)
        try:
            result = tool(**kwargs)
        except Exception:
            return {}
        data = (result or {}).get("data") if isinstance(result, dict) else None
        payload = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)
        if not isinstance(payload, dict):
            return {}
        klines = payload.get("klines")
        if not isinstance(klines, list) or not klines:
            return {}
        out = {"close": [], "high": [], "low": [], "volume": [], "amount": []}
        for item in klines[-max(20, int(lookback_days or 60)):]:
            if not isinstance(item, dict):
                continue
            try:
                close = float(item.get("close", 0) or 0)
                high = float(item.get("high", 0) or 0)
                low = float(item.get("low", 0) or 0)
                volume = float(item.get("volume", 0) or 0)
                amount = float(item.get("amount", 0) or 0)
            except Exception:
                continue
            if close <= 0 or high <= 0 or low <= 0:
                continue
            out["close"].append(close)
            out["high"].append(high)
            out["low"].append(low)
            out["volume"].append(volume)
            out["amount"].append(amount)
        if len(out["close"]) < 20:
            return {}
        return out

    def fetch_batch_daily_history(self, codes: List[str], lookback_days: int = 60, end_date: str = "") -> Dict[str, Dict[str, List[float]]]:
        result: Dict[str, Dict[str, List[float]]] = {}
        normalized_codes = []
        for code in codes:
            normalized = self._normalize_code(code)
            if normalized:
                normalized_codes.append(normalized)
        if not normalized_codes:
            return result
        max_workers = max(1, min(8, len(normalized_codes)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self.fetch_daily_history, code, lookback_days, end_date): code
                for code in normalized_codes
            }
            for fut in as_completed(future_map):
                code = future_map[fut]
                try:
                    series = fut.result()
                except Exception:
                    continue
                if series:
                    result[code] = series
        return result

    def fetch_gem_candidates(self, settings) -> List[dict]:
        """Fetch GEM small-cap candidates.

        Filters: board=30xxxx, exclude ST.
        Returns list of {code, name} sorted by code.
        Market cap / revenue filtering is done downstream (scanner).
        """
        gem_board = str(getattr(settings, "gem_board_prefix", "30") or "30")
        universe_limit = int(getattr(settings, "gem_universe_limit", 200) or 200)

        universe_tool = self._load_tool(
            "plugins.data_collection.stock.fundamentals_extended", "tool_fetch_a_share_universe"
        )
        if not universe_tool:
            return []

        try:
            universe_resp = universe_tool(provider_preference="standard")
        except Exception:
            return []

        all_rows = []
        if isinstance(universe_resp, dict) and bool(universe_resp.get("success")):
            data = universe_resp.get("data")
            if isinstance(data, list):
                all_rows = data

        if not all_rows:
            return []

        candidates = []
        for row in all_rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code", "")).strip()
            name = str(row.get("name", "")).strip()
            if not code or "ST" in name or "退" in name:
                continue
            # GEM only (30xxxx)
            if not code.startswith(gem_board):
                continue
            candidates.append({"code": code, "name": name})

        # Sort by code DESCENDING (newer listings → typically smaller cap)
        # Skip gem_rank_start smallest codes, take final_pool_size
        candidates.sort(key=lambda x: x["code"], reverse=True)
        rank_start = int(getattr(settings, "gem_rank_start", 10) or 10)
        final_pool = int(getattr(settings, "gem_final_pool_size", 20) or 20)
        # Take from the middle: skip very new (possibly problematic) and keep the small-cap range
        return candidates[rank_start:rank_start + final_pool]


def _fuzzy_match_sector(industry: str, strong_sector_names: set) -> str:
    """Fuzzy match a stock's industry string against known strong sector names.

    Returns the matched sector name, or empty string if no match.
    """
    if not industry or not strong_sector_names:
        return ""
    industry = str(industry).strip()
    # Direct match
    if industry in strong_sector_names:
        return industry
    # Substring match: sector name contained in industry or vice versa
    for sname in strong_sector_names:
        if sname in industry or industry in sname:
            return sname
    # Word-level match: check individual words
    industry_words = set(industry.replace("、", " ").replace("/", " ").split())
    for sname in strong_sector_names:
        sname_words = set(sname.replace("、", " ").replace("/", " ").split())
        if industry_words & sname_words:
            return sname
        # Cross-word partial match
        for iw in industry_words:
            for sw in sname_words:
                if len(iw) >= 2 and len(sw) >= 2 and (sw in iw or iw in sw):
                    return sname
        # Character-level overlap: check 2-char bigram overlap
        def _bigrams(s):
            return {s[j:j+2] for j in range(len(s)-1)}
        ib = _bigrams(industry)
        sb = _bigrams(sname)
        if len(ib & sb) >= 1:
            return sname
    return ""

#!/usr/bin/env python3
"""Rank verified events by their auditable relevance to known portfolio exposure."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set

from domain.services.event_service import THEME_MAP


THEME_ALIASES = {
    "AI Chips": "半导体",
    "海外AI芯片": "半导体",
}

# THEME_MAP intentionally contains only representative event beneficiaries.
# These additions classify currently common holdings that are not present in
# that short list.  Exact codes make the inference auditable and avoid broad
# matches such as treating every company ending in “科技” as a semiconductor.
POSITION_THEME_OVERRIDES = {
    "603986": {"半导体"},  # 兆易创新：存储/MCU 芯片设计
    "300475": {"半导体"},  # 香农芯创：存储产品分销
    "600584": {"半导体"},  # 长电科技：集成电路封测
}

POSITION_NAME_KEYWORDS = {
    "半导体": ("半导体", "芯片", "存储", "集成电路", "微电子", "封测", "先进封装"),
    "AI算力": ("算力", "数据中心", "光模块", "服务器", "液冷"),
    "机器人": ("机器人", "减速器", "伺服"),
    "华为": ("鸿蒙", "昇腾", "鲲鹏"),
    "Energy Commodities": ("石油", "油气", "黄金", "有色", "航运", "煤炭"),
    "Geopolitics": ("军工",),
    "Global EV": ("新能源车", "新能源汽车", "锂电", "动力电池"),
}

DOWNSIDE_WORDS = (
    "跳水", "暴跌", "大跌", "下挫", "下调", "减持", "亏损", "违约", "制裁", "出口管制",
    "加息", "plunge", "sell-off", "crash", "slump", "warning", "sanction", "restriction",
)
CATALYST_WORDS = ("突破", "涨价", "订单", "量产", "中标", "上调", "回购", "beat", "upgrade", "rally")


def _code_key(value: Any) -> str:
    digits = "".join(char for char in str(value or "") if char.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


def _canonical_theme(value: Any) -> str:
    raw = str(value or "").strip()
    return THEME_ALIASES.get(raw, raw)


def _weight(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _event_themes(event: Dict[str, Any]) -> Set[str]:
    values = []
    for item in event.get("themes") or []:
        values.append(item.get("theme") if isinstance(item, dict) else item)
    return {_canonical_theme(value) for value in values if str(value or "").strip()}


def _mapped_theme_codes() -> Dict[str, Set[str]]:
    result: Dict[str, Set[str]] = {}
    for theme, config in THEME_MAP.items():
        canonical = _canonical_theme(theme)
        result.setdefault(canonical, set()).update(
            _code_key(item.get("code")) for item in config.get("stocks") or [] if _code_key(item.get("code"))
        )
    return result


THEME_CODES = _mapped_theme_codes()


def position_themes(position: Dict[str, Any]) -> Set[str]:
    """Return conservative theme exposure for one holding."""
    code = _code_key(position.get("code") or position.get("stock_code"))
    name = str(position.get("name") or position.get("stock_name") or "")
    themes = {_canonical_theme(value) for value in position.get("themes") or [] if str(value or "").strip()}
    themes.update(POSITION_THEME_OVERRIDES.get(code, set()))
    for theme, codes in THEME_CODES.items():
        if code and code in codes:
            themes.add(theme)
    lowered = name.lower()
    for theme, keywords in POSITION_NAME_KEYWORDS.items():
        if any(keyword.lower() in lowered for keyword in keywords):
            themes.add(_canonical_theme(theme))
    return themes


def _impact_tone(event: Dict[str, Any]) -> tuple[str, int]:
    text = f"{event.get('title') or ''} {event.get('summary') or ''}".lower()
    if any(word.lower() in text for word in DOWNSIDE_WORDS):
        return "downside_risk", 10
    if any(word.lower() in text for word in CATALYST_WORDS):
        return "catalyst", 4
    return "unclear", 0


def rank_portfolio_events(
    events: Iterable[Dict[str, Any]],
    positions: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Add portfolio relevance evidence and rank without inventing direction."""
    position_rows = [dict(item) for item in positions or [] if isinstance(item, dict)]
    ranked: List[Dict[str, Any]] = []
    for sequence, raw_event in enumerate(events or []):
        event = dict(raw_event)
        event_themes = _event_themes(event)
        related_codes = {
            _code_key(item.get("code"))
            for item in event.get("related_stocks") or []
            if isinstance(item, dict) and _code_key(item.get("code"))
        }
        matches = []
        exposure_weight = 0.0
        exact_weight = 0.0
        for position in position_rows:
            code = _code_key(position.get("code") or position.get("stock_code"))
            themes = position_themes(position)
            shared_themes = sorted(event_themes & themes)
            exact = bool(code and code in related_codes)
            if not exact and not shared_themes:
                continue
            weight = _weight(position.get("weight"))
            exposure_weight += weight
            if exact:
                exact_weight += weight
            matches.append(
                {
                    "code": str(position.get("code") or position.get("stock_code") or ""),
                    "name": str(position.get("name") or position.get("stock_name") or code),
                    "weight": round(weight, 4),
                    "match_basis": "exact_security" if exact else "shared_theme",
                    "shared_themes": shared_themes,
                    "stale": bool(position.get("stale_sources")),
                }
            )
        matches.sort(key=lambda item: float(item.get("weight") or 0), reverse=True)
        tone, tone_bonus = _impact_tone(event)
        base_score = float(event.get("score") or 0)
        exposure_weight = min(1.0, exposure_weight)
        exact_weight = min(1.0, exact_weight)
        event.update(
            {
                "portfolio_positions": matches,
                "portfolio_exposure_weight": round(exposure_weight, 4),
                "portfolio_exact_weight": round(exact_weight, 4),
                "portfolio_relevance": "exact_security" if exact_weight else "shared_theme" if matches else "market_wide",
                "impact_tone": tone,
                "portfolio_priority_score": round(base_score + exposure_weight * 50 + exact_weight * 20 + tone_bonus, 2),
                "_original_sequence": sequence,
            }
        )
        ranked.append(event)
    ranked.sort(
        key=lambda item: (
            float(item.get("portfolio_priority_score") or 0),
            float(item.get("score") or 0),
            -int(item.get("_original_sequence") or 0),
        ),
        reverse=True,
    )
    return ranked

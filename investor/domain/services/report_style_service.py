#!/usr/bin/env python3
"""Shared human-facing wording helpers for Feishu investor reports."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Iterable


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
from shared_report_card_renderer import build_feishu_report_card


RISK_LABELS = {
    "snapshot_date_unknown": "持仓快照日期未知",
    "top1_concentration_high": "单一持仓集中度过高",
    "top3_concentration_high": "前三持仓集中度过高",
    "portfolio_unrealized_loss": "组合累计持仓盈亏为负",
    "position_pnl_conflict": "持仓盈亏字段与成本口径冲突",
    "position_coverage_incomplete": "持仓明细覆盖不完整",
    "account_source_incomplete": "部分账户接口缺失",
    "stale_account_source": "部分账户持仓使用历史快照",
    "no_major_snapshot_risk_flag": "未发现重大持仓风险",
}

SOURCE_LABELS = {
    "main": "国金",
    "trade": "东莞",
    "guojin": "国金",
    "dongguan": "东莞",
    "combined": "跨账户合计",
    "unknown": "来源待确认",
}

TREND_LABELS = {
    "strong_up": "强势上行",
    "up": "上行",
    "turning_up": "转强",
    "sideways": "震荡",
    "weak": "偏弱",
    "down": "下行",
    "improving": "动量改善",
    "positive": "温和上行",
    "overheated": "短线过热",
}

THEME_LABELS = {
    "Energy Commodities": "能源与大宗商品",
    "Geopolitics": "地缘风险",
    "Global Macro": "全球宏观",
    "AI Chips": "海外AI芯片",
    "Global EV": "海外新能源车",
}

EVENT_IMPACT_LABELS = {
    "P0": "重大影响",
    "P1": "高影响",
    "P2": "重点关注",
    "P3": "一般关注",
}

NORMAL_REPORT_BANNED_PATTERNS = (
    (r"\bgenerated_at\s*[:=]", "包含 generated_at 机器字段"),
    (r"\bsnapshot_age_days\s*[:=]", "包含 snapshot_age_days 机器字段"),
    (r"\brisk_flags\s*[:=]", "包含 risk_flags 机器字段"),
    (r"\bpriority\s*=", "包含 priority= 机器评分"),
    (r"\bevents\s*=", "包含 events= 机器计数"),
    (r"\bsource\s*=", "包含 source= 机器字段"),
    (r"\bscore\s*=", "包含 score= 机器字段"),
    (r"\blines\s*=", "包含 lines= 运维字段"),
    (r"\bas_of\s*=", "包含 as_of= 机器字段"),
    (r"\bnav\s*=", "包含 NAV= 机器字段"),
    (r"\bcash\s*=", "包含 cash= 机器字段"),
    (r"\bholdings\s*=", "包含 holdings= 机器字段"),
    (r"\bplan_actions\s*=", "包含 plan_actions= 机器字段"),
    (r"\brejected\s*=", "包含 rejected= 机器字段"),
    (r"\b(?:status|ok|process_matches|window_active|script_success|returncode)\s*=", "包含策略接口机器字段"),
    (r"\b(?:reachable|pos|ord|trd|filled_match|packets|quotes|positions|trades)\s*=", "包含监控计数机器字段"),
    (r"(?:数据源|成交额|收盘|趋势|均线交叉|净买额|总收益|年化|最大回撤|胜率|营收|归母净利|经营现金流|资产负债率|营收增速|净利增速|标题级初筛|主力净流入)\s*=", "包含等号拼接的分析字段"),
    (r"\b(?:MA\d+|RSI\d*|MACD|KDJ|PE|PB|PS|PEG|ROE)\s*=", "包含等号拼接的指标字段"),
    (r"(?:^|\s)/root/", "包含服务器内部路径"),
    (r"\brc\s*=\s*0\b", "包含成功退出码噪声"),
    (r"\b(?:blocked|warning)\s*=", "包含英文能力审计计数字段"),
    (r"\b(?:holdings_account_monitor|service_health_diagnostics)\b", "包含内部能力检查名称"),
    (r"数据日\s*\d{8}\b", "数据日使用未格式化的紧凑日期"),
    (r"\bstatus\s*:\s*ok\b", "包含成功状态噪声"),
    (r"\bLLM\b", "直接暴露 LLM 内部术语"),
    (r"\bP[0-3]\b", "包含内部事件等级"),
    (r"能力检查\s*[：:]\s*ok\b", "包含能力检查运维噪声"),
    (r"\b(?:improving|positive|overheated)\b", "包含未翻译的趋势标签"),
)


def money(value: Any) -> str:
    try:
        amount = float(value or 0)
    except Exception:
        amount = 0.0
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if amount >= 100_000_000:
        return f"{sign}{amount / 100_000_000:.2f}亿"
    if amount >= 10_000:
        return f"{sign}{amount / 10_000:.2f}万"
    return f"{sign}{amount:.2f}元"


def pct(value: Any, digits: int = 1, signed: bool = False) -> str:
    try:
        number = float(value or 0)
    except Exception:
        number = 0.0
    spec = f"+.{digits}f" if signed else f".{digits}f"
    return format(number, spec) + "%"


def risk_label(flag: Any) -> str:
    raw = str(flag or "").strip()
    if raw.startswith("snapshot_stale_"):
        days = raw.removeprefix("snapshot_stale_").removesuffix("d")
        return f"持仓快照已过期 {days} 天"
    return RISK_LABELS.get(raw, raw.replace("_", " ") or "风险状态未知")


def source_label(source: Any) -> str:
    raw = str(source or "unknown").strip().lower()
    return SOURCE_LABELS.get(raw, raw)


def trend_label(trend: Any) -> str:
    raw = str(trend or "").strip().lower()
    return TREND_LABELS.get(raw, raw or "趋势待确认")


def theme_label(theme: Any) -> str:
    raw = str(theme or "").strip()
    return THEME_LABELS.get(raw, raw or "主题待确认")


def event_impact_label(severity: Any) -> str:
    raw = str(severity or "P3").strip().upper()
    return EVENT_IMPACT_LABELS.get(raw, "影响待核验")


def join_cn(items: Iterable[Any], empty: str = "无") -> str:
    values = [str(item).strip() for item in items if str(item).strip()]
    return "、".join(dict.fromkeys(values)) if values else empty


def data_status(generated_at: Any, notes: Iterable[str] = ()) -> str:
    details = [str(item).strip() for item in notes if str(item).strip()]
    suffix = "；" + "；".join(details) if details else ""
    return f"生成时间 {generated_at or '未知'}{suffix}"


def event_summary_cn(title: Any, themes: Iterable[Any] = ()) -> str:
    text = re.sub(r"^\d{1,2}:\s*\d{2}\s+", "", str(title or "").strip())
    lowered = text.lower()
    if any(token in lowered for token in ("iran", "hormuz", "strait")) and any(token in lowered for token in ("strike", "attack", "shipping", "tanker", "military")):
        if "hormuz" in lowered or "shipping" in lowered or "tanker" in lowered:
            return "美伊冲突加剧霍尔木兹航运与能源供应风险"
        return "美伊军事冲突升级，避险与能源风险升温"
    if ("chip stocks" in lowered or "semiconductor shares" in lowered) and any(token in lowered for token in ("sink", "sell-off", "rout", "plunge")):
        return "亚洲AI芯片股跟随美股下跌"
    if "treasury yield" in lowered and "oil" in lowered and any(token in lowered for token in ("iran", "de-escalation")):
        return "伊朗局势缓和预期推动油价与美债收益率回落"
    if "treasury yield" in lowered and "iran" in lowered:
        return "伊朗局势变化牵动美国国债收益率"
    if "russia sanctions" in lowered:
        return "美国参议院推进对俄罗斯的新制裁法案"
    if "oil" in lowered and "iran" in lowered and any(token in lowered for token in ("hormuz", "strait")):
        return "伊朗霍尔木兹海峡限制方案推升石油供应担忧"
    if any(token in lowered for token in ("nvidia", "ai chip", "semiconductor")):
        return "海外AI芯片与半导体产业出现新变化"
    if any(token in lowered for token in ("fed", "rate cut", "rate hike", "inflation")):
        return "海外利率与通胀预期出现新变化"
    if any(token in lowered for token in ("oil", "brent", "wti")):
        return "国际油价与能源供应预期出现新变化"
    if re.search(r"[\u4e00-\u9fff]", text):
        return text
    return f"{join_cn((theme_label(item) for item in themes), '海外市场')}相关海外事件更新"


def longterm_summary_cn(summary: dict[str, Any]) -> str:
    if not summary.get("available"):
        return "- 长线组合快照不可用。"
    nav = float(summary.get("nav", 0) or 0)
    cash = float(summary.get("cash", 0) or 0)
    cash_ratio = float(summary.get("cash_ratio", 0) or 0) * 100
    holdings = int(summary.get("holdings_count", 0) or 0)
    actions = int(summary.get("actions_count", 0) or 0)
    rejected = int(summary.get("rejected_actions_count", 0) or 0)
    return f"- 数据日 {summary.get('as_of') or '未知'}；净值 {nav:,.2f}，现金 {cash:,.2f}（{cash_ratio:.1f}%），持仓 {holdings} 只；计划 {actions} 笔，被拒 {rejected} 笔。"


def report_freshness_issues(text: Any) -> list[str]:
    """Require an explicit data-date contract for market-data report families."""
    body = str(text or "").strip()
    issues: list[str] = []
    contracts = (
        ("🔥 概念板块强弱", ("数据交易日", "抓取时间")),
        ("📈 行情快照", ("数据时间",)),
        ("📐 技术分析", ("数据日",)),
        ("🌡️ A股市场情绪", ("数据日",)),
        ("📣 公司公告", ("检索日期",)),
        ("📰 公告标题初筛", ("检索日期",)),
        ("🧪 策略回测", ("区间",)),
    )
    for marker, required in contracts:
        if marker not in body:
            continue
        for label in required:
            if label not in body:
                issues.append(f"{marker.lstrip('🔥📈📐🌡️📣📰🧪 ').strip()}缺少{label}")
    if "🌏 中国宏观数据快照" in body and not any(label in body for label in ("月份", "报告期", "发布日期")):
        issues.append("中国宏观数据快照缺少指标报告期")
    return list(dict.fromkeys(issues))


def report_quality_issues(text: Any, max_chars: int = 5500) -> list[str]:
    """Fail-closed checks for normal investor-facing Feishu report bodies."""
    body = str(text or "").strip()
    issues: list[str] = []
    if not body:
        return ["报告正文为空"]
    if len(body) > max_chars:
        issues.append(f"正文过长（{len(body)} > {max_chars} 字符）")
    if "\ufffd" in body:
        issues.append("包含乱码替换字符")
    if re.search(r"\|\s*-{3,}\s*\|", body):
        issues.append("包含不适合飞书卡片的 Markdown 表格")
    for pattern, message in NORMAL_REPORT_BANNED_PATTERNS:
        if re.search(pattern, body, flags=re.IGNORECASE):
            issues.append(message)
    issues.extend(report_freshness_issues(body))
    return list(dict.fromkeys(issues))


def report_title(text: Any, default: str = "OpenClaw 投资助理") -> str:
    first = next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")
    first = re.sub(r"^[#>\s]+", "", first)
    first = re.sub(r"[*_`~]+", "", first).strip()
    if "暂不可用" in first:
        subject = first.split("暂不可用", 1)[0].rstrip("：:；;，,。 ")
        return f"{subject[:36]}状态" if subject else "OpenClaw 数据状态"
    if "生成失败" in first:
        subject = first.split("生成失败", 1)[0].rstrip("：:；;，,。 ")
        return f"❌ {subject[:32]}生成失败" if subject else "❌ OpenClaw 报告生成失败"
    if "查询失败" in first:
        subject = first.split("查询失败", 1)[0].rstrip("：:；;，,。 ")
        return f"❌ {subject[:32]}查询失败" if subject else "❌ OpenClaw 查询失败"
    if first.startswith(("未识别", "未找到", "暂无", "尚无")):
        return "OpenClaw 数据提示"
    return (first[:56] + "…") if len(first) > 57 else (first or default)


def _card_body_without_redundant_title(text: str) -> str:
    """Remove a report's title line because the card header already displays it."""
    lines = str(text or "").splitlines()
    first_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_index is None or not any(line.strip() for line in lines[first_index + 1 :]):
        return str(text or "").strip()
    raw_first = lines[first_index].strip()
    first = report_title(raw_first, default="")
    title_tokens = (
        "OpenClaw", "简报", "报告", "周报", "预测", "复盘", "概览", "分析",
        "检查", "回读", "状态", "名单", "组合", "提醒", "情绪", "筛选",
        "公告", "行情快照", "能力审计", "投资助理",
    )
    decorated_title = bool(re.match(r"^[^\w\u4e00-\u9fff#*]", raw_first))
    if not decorated_title and not any(token in first for token in title_tokens):
        return str(text or "").strip()
    return "\n".join(lines[first_index + 1 :]).strip()


def build_report_card(
    text: Any,
    *,
    title: str = "",
    template: str = "blue",
    generated_at: str = "",
) -> dict[str, Any]:
    """Shared Feishu card for normal investor-facing reports and manual queries."""
    body = str(text or "").strip()
    card_title = title or report_title(body)
    card_body = _card_body_without_redundant_title(body) or body
    return build_feishu_report_card(
        title=card_title,
        body=card_body,
        template=template,
        generated_at=generated_at,
    )


def is_diagnostic_message(text: Any) -> bool:
    """Only classify explicit operational failure payloads as diagnostics.

    Data degradation wording such as “行情不可用” remains a normal report and must
    still pass the report quality gate.
    """
    body = str(text or "").strip()
    if body.startswith("❌"):
        return True
    return bool(
        re.search(r"(?:Traceback|故障行[:：]|退出码[:：]|returncode\s*=|stderr\s*=)", body, flags=re.IGNORECASE)
        or re.search(r"(?:任务|服务|命令|脚本).{0,16}(?:执行)?失败", body)
        or re.search(r"(?:晨报|周报|报告|简报|审计|预测|复盘|查询).{0,12}(?:生成|读取|处理)?失败", body)
    )

#!/usr/bin/env python3
"""Feishu interactive card message builder.

Generates Feishu card JSON payloads for alerts, briefings,
and interactive trading assistant messages.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

# Color palette for severity levels
SEVERITY_COLORS = {
    "P0": "red",
    "P1": "orange",
    "P2": "yellow",
    "P3": "blue",
    "info": "blue",
    "ok": "green",
}


def _card_header(title: str, color: str = "blue") -> Dict:
    return {
        "header": {
            "title": {"tag": "plain_text", "content": str(title)[:60]},
            "template": str(color),
        }
    }


def _card_div(text: str) -> Dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": str(text)}}


def _card_hr() -> Dict:
    return {"tag": "hr"}


def _card_button(text: str, value: str, button_type: str = "default") -> Dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": str(text)},
        "type": str(button_type),
        "value": {"command": str(value)},
    }


def build_alert_card(
    severity: str,
    title: str,
    summary: str,
    evidence: Dict | None = None,
    actions: Sequence[Dict] | None = None,
) -> Dict:
    """Build a Feishu card for a risk/incident alert.

    Parameters
    ----------
    severity : str
        P0 / P1 / P2 / P3
    title : str
        Card header (e.g. "🔴 仓位集中度告警")
    summary : str
        Main alert text.
    evidence : dict, optional
        Additional details to show.
    actions : list of dict, optional
        Button definitions. Each dict should have 'text' and 'value'.
    """
    color = SEVERITY_COLORS.get(str(severity).upper(), "blue")
    card = {
        **_card_header(title, color=color),
        "elements": [
            _card_div(f"**{summary}**"),
        ],
    }

    if evidence:
        lines = [f"- {k}: {v}" for k, v in evidence.items() if v is not None]
        if lines:
            card["elements"].append(_card_hr())
            card["elements"].append(_card_div("\n".join(lines)))

    if actions:
        card["elements"].append(_card_hr())
        card["elements"].append(
            {
                "tag": "action",
                "actions": [
                    _card_button(
                        str(item.get("text", "")),
                        str(item.get("value", "")),
                        button_type=str(item.get("type", "default")),
                    )
                    for item in actions[:4]
                ],
            }
        )

    return {
        "msg_type": "interactive",
        "card": card,
    }


def build_briefing_card(
    title: str,
    sections: Sequence[Dict],
    footer: str = "",
) -> Dict:
    """Build a Feishu card for scheduled briefings.

    Parameters
    ----------
    title : str
        Card header title.
    sections : list of dict
        Each section: {"label": "标题", "text": "内容", "highlight": False}.
    footer : str
        Footer timestamp or note.
    """
    elements = []
    for i, section in enumerate(sections or []):
        if i > 0:
            elements.append(_card_hr())
        label = str(section.get("label", ""))
        content = str(section.get("text", ""))
        if section.get("highlight"):
            elements.append(_card_div(f"**{label}**"))
            elements.append(_card_div(f"**{content}**"))
        else:
            elements.append(_card_div(f"**{label}**"))
            elements.append(_card_div(content))

    if footer:
        elements.append(_card_hr())
        elements.append(
            {"tag": "note", "elements": [{"tag": "plain_text", "content": str(footer)}]}
        )

    return {
        "msg_type": "interactive",
        "card": {
            **_card_header(title, color="blue"),
            "elements": elements,
        },
    }


def build_trade_notification_card(
    event_type: str,
    code: str,
    name: str,
    price: float,
    volume: int,
    amount: float,
    server: str = "",
    timestamp: str = "",
) -> Dict:
    """Build a trade execution notification card.

    Parameters
    ----------
    event_type : str
        'buy_filled' / 'sell_filled' / 'order_submitted' / 'order_cancelled'
    """
    emoji = {"buy_filled": "📈", "sell_filled": "📉", "order_submitted": "📝", "order_cancelled": "❌"}
    labels = {"buy_filled": "买入成交", "sell_filled": "卖出成交", "order_submitted": "委托已提交", "order_cancelled": "委托已撤"}

    label = labels.get(event_type, event_type)
    em = emoji.get(event_type, "🔔")
    color = {"buy_filled": "red", "sell_filled": "green", "order_submitted": "blue", "order_cancelled": "yellow"}.get(
        event_type, "blue"
    )

    sections = [
        {"label": f"{em} {label}", "text": f"{code} {name}", "highlight": True},
        {"label": "价格", "text": f"{price:.3f}"},
        {"label": "数量", "text": f"{volume}股"},
        {"label": "金额", "text": f"{amount:,.2f}"},
    ]
    if server:
        sections.append({"label": "账户", "text": str(server)})

    return build_briefing_card(
        title=f"{em} {label}",
        sections=sections,
        footer=timestamp or "",
    )


def build_empty_state_card(title: str, message: str = "暂无数据") -> Dict:
    """Build a card for empty state (no data available)."""
    return build_briefing_card(
        title=title,
        sections=[{"label": "提示", "text": message}],
    )

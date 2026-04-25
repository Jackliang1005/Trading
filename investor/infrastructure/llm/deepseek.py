#!/usr/bin/env python3
"""DeepSeek HTTP adapter."""

from __future__ import annotations

import json
import urllib.request


SYSTEM_ROLE_TEXT = (
    "你是专业的A股投资分析师，擅长结合全球市场、地缘政治、大宗商品等宏观因素进行综合分析。"
    "横盘市场优先预测neutral。请严格按要求的 JSON 格式输出预测。"
)


def call_deepseek_chat(prompt: str, api_key: str, model: str = "deepseek-chat") -> str:
    url = "https://api.deepseek.com/chat/completions"
    body = {
        "model": model or "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_ROLE_TEXT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 1500,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    return result["choices"][0]["message"]["content"]


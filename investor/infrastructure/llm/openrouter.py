#!/usr/bin/env python3
"""OpenRouter HTTP adapter."""

from __future__ import annotations

import json
import urllib.request

from infrastructure.llm.deepseek import SYSTEM_ROLE_TEXT


def call_openrouter_chat(prompt: str, api_key: str, model: str) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    body = {
        "model": model,
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


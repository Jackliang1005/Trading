#!/usr/bin/env python3
"""Unified LLM client for prediction generation."""

from __future__ import annotations

import json
import os
from typing import Tuple

from infrastructure.llm.deepseek import call_deepseek_chat
from infrastructure.llm.openrouter import call_openrouter_chat


def _resolve_deepseek_key() -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if api_key:
        return api_key
    config_paths = [
        os.path.expanduser("~/.openclaw/config.json"),
        os.path.expanduser("~/.openclaw/workspace/config/llm_config.json"),
        os.path.expanduser("~/.openclaw/openclaw.json"),
        os.path.expanduser("~/.openclaw/agents/main/agent/models.json"),
    ]
    for path in config_paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            found = cfg.get("deepseek_api_key", cfg.get("api_key", ""))
            if not found:
                providers = cfg.get("models", cfg).get("providers", {})
                deepseek_cfg = providers.get("deepseek", {})
                found = deepseek_cfg.get("apiKey", deepseek_cfg.get("api_key", ""))
            if found:
                return str(found)
        except Exception:
            continue
    return ""


def resolve_available_provider() -> Tuple[str, str]:
    deepseek_key = _resolve_deepseek_key()
    if deepseek_key:
        return "deepseek", deepseek_key
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if openrouter_key:
        return "openrouter", openrouter_key
    return "", ""


def call_prediction_llm(prompt: str, model: str = "deepseek/deepseek-chat") -> str:
    provider, key = resolve_available_provider()
    if provider == "deepseek":
        # DeepSeek 官方 endpoint 只接受 deepseek-chat / deepseek-reasoner.
        model_name = "deepseek-chat"
        if model in {"deepseek-chat", "deepseek-reasoner"}:
            model_name = model
        return call_deepseek_chat(prompt, key, model=model_name)
    if provider == "openrouter":
        return call_openrouter_chat(prompt, key, model=model)
    raise RuntimeError("no available llm api key")


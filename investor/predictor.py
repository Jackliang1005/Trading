#!/usr/bin/env python3
"""Prediction compatibility facade."""

import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(__file__))

from domain.services.prediction_service import (
    PREDICTION_TARGETS,
    load_prediction_snapshot_data,
)
from domain.services.prediction_prompt_service import (
    build_market_context_text as _build_market_context_text,
    build_prediction_context as _build_prediction_context,
    build_prediction_prompt as _build_prediction_prompt,
)
from domain.services.prediction_orchestrator import (
    call_llm_for_prediction as _call_llm_for_prediction,
    generate_predictions as _generate_predictions,
    parse_predictions as _parse_predictions,
    render_rule_based_prediction_json,
)

def build_prediction_context(snapshot_data: Dict) -> Dict:
    return _build_prediction_context(snapshot_data)


def build_market_context_text(snapshot_data: Dict) -> str:
    return _build_market_context_text(snapshot_data)


def build_prediction_prompt(snapshot_data: Dict, rag_context: str, few_shot: str, system_prompt: str) -> str:
    return _build_prediction_prompt(snapshot_data, rag_context, few_shot, system_prompt)


def call_llm_for_prediction(prompt: str, model: str = "deepseek/deepseek-chat") -> str:
    return _call_llm_for_prediction(prompt, model=model)


def _rule_based_prediction() -> str:
    """无 LLM 时的规则预测回退"""
    return render_rule_based_prediction_json()


def parse_predictions(llm_output: str) -> List[Dict]:
    """从 LLM 输出中解析预测 JSON"""
    return _parse_predictions(llm_output)


def generate_predictions(model: str = "deepseek/deepseek-chat") -> List[int]:
    return _generate_predictions(model=model)

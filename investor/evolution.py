#!/usr/bin/env python3
"""Evolution compatibility facade."""

from __future__ import annotations

import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(__file__))
import db
from domain.services.evolution_service import (
    adjust_strategy_weights as adjust_strategy_weights_service,
    evolve as evolve_service,
    generate_system_prompt as generate_system_prompt_service,
    load_strategy_config as load_strategy_config_service,
    update_few_shot_examples as update_few_shot_examples_service,
    update_rules_from_failures as update_rules_from_failures_service,
)


def load_strategy_config() -> Dict:
    return load_strategy_config_service()


def adjust_strategy_weights(lookback_days: int = 14) -> Dict:
    return adjust_strategy_weights_service(lookback_days=lookback_days)


def update_rules_from_failures(lookback_days: int = 7) -> List[Dict]:
    return update_rules_from_failures_service(lookback_days=lookback_days)


def update_few_shot_examples(lookback_days: int = 14) -> Dict:
    return update_few_shot_examples_service(lookback_days=lookback_days)


def generate_system_prompt() -> str:
    return generate_system_prompt_service()


def evolve() -> Dict:
    return evolve_service()


if __name__ == "__main__":
    db.init_db()
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "weights":
            adjust_strategy_weights()
        elif cmd == "rules":
            update_rules_from_failures()
        elif cmd == "fewshot":
            update_few_shot_examples()
        elif cmd == "prompt":
            print(generate_system_prompt())
        else:
            print(f"未知命令: {cmd}")
            print("可用: weights, rules, fewshot, prompt")
    else:
        evolve()

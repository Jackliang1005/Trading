#!/usr/bin/env python3
from __future__ import annotations

from typing import Dict, List


def build_validation_plan(task: Dict) -> List[str]:
    commands = list(task.get("suggested_commands", []) or [])
    suspicious_files = list(task.get("suspicious_files", []) or [])
    for path in suspicious_files[:5]:
        if str(path).endswith(".py"):
            commands.append(f"python3 -m py_compile {path}")
    deduped = []
    for cmd in commands:
        if cmd not in deduped:
            deduped.append(cmd)
    return deduped

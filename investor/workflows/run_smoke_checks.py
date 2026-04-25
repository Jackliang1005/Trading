#!/usr/bin/env python3
"""Run investor smoke checks."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class SmokeStep:
    name: str
    command: str


DEFAULT_SMOKE_STEPS: List[SmokeStep] = [
    SmokeStep("py_compile", "python3 -m py_compile main.py investor_agent.py app/cli.py"),
    SmokeStep("monitor_trading", "python3 main.py monitor-trading 20260320"),
    SmokeStep("today_summary_text", "python3 main.py today-summary 20260320 --text"),
    SmokeStep("today_account", "python3 main.py today-account 20260320"),
    SmokeStep("agent_context", "python3 investor_agent.py context \"今天候选和买入情况\""),
    SmokeStep("evolve", "python3 main.py evolve"),
    SmokeStep("reflection_weekly", "python3 reflection.py weekly"),
    SmokeStep("reflection_monthly", "python3 reflection.py monthly"),
    SmokeStep("runtime_check", "python3 main.py runtime-check 2026-04-25"),
    SmokeStep("packet_backfill_dryrun", "python3 main.py backfill-packets --type daily_close --limit 2"),
    SmokeStep("packet_maintain_dryrun", "python3 main.py packet-maintain --dry-run --limit 2"),
]


def run_smoke_checks(stop_on_fail: bool = False, max_output_chars: int = 2000) -> Dict:
    started_at = time.time()
    results = []
    ok = True

    for step in DEFAULT_SMOKE_STEPS:
        step_start = time.time()
        try:
            proc = subprocess.run(
                shlex.split(step.command),
                check=False,
                capture_output=True,
                text=True,
            )
            output = ((proc.stdout or "") + (proc.stderr or "")).strip()
            passed = proc.returncode == 0
            if not passed:
                ok = False
            results.append(
                {
                    "name": step.name,
                    "command": step.command,
                    "ok": passed,
                    "exit_code": proc.returncode,
                    "duration_ms": round((time.time() - step_start) * 1000, 1),
                    "output": output[:max_output_chars],
                }
            )
            if stop_on_fail and not passed:
                break
        except Exception as exc:
            ok = False
            results.append(
                {
                    "name": step.name,
                    "command": step.command,
                    "ok": False,
                    "exit_code": -1,
                    "duration_ms": round((time.time() - step_start) * 1000, 1),
                    "output": str(exc)[:max_output_chars],
                }
            )
            if stop_on_fail:
                break

    return {
        "ok": ok,
        "step_count": len(DEFAULT_SMOKE_STEPS),
        "executed_steps": len(results),
        "duration_ms": round((time.time() - started_at) * 1000, 1),
        "results": results,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run investor smoke checks.")
    parser.add_argument("--stop-on-fail", action="store_true", help="Stop after first failed step.")
    parser.add_argument(
        "--max-output-chars",
        type=int,
        default=2000,
        help="Truncate each step output to this length.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    result = run_smoke_checks(
        stop_on_fail=bool(args.stop_on_fail),
        max_output_chars=max(200, int(args.max_output_chars)),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

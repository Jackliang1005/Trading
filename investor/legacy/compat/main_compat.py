"""Legacy main.py compatibility helpers."""

from __future__ import annotations

from app.cli import (
    get_new_command_usage,
    is_new_command,
    print_result,
    run_command,
)


def run_new_command(command: str):
    result = run_command(command)
    print_result(result)
    return result


def handle_new_command(command: str) -> bool:
    if not is_new_command(command):
        return False
    try:
        run_new_command(command)
    except ValueError as exc:
        usage = get_new_command_usage(command)
        message = str(exc).strip()
        if message and message != usage:
            print(f"❌ {message}")
        if usage:
            print(f"usage: {usage}")
    return True


def print_new_command_help() -> None:
    if is_new_command("help"):
        print()
        print_result(run_command("help"))

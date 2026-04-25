#!/usr/bin/env python3
"""Feishu plugin bridge entrypoint.

Usage:
  python3 feishu_plugin_bridge.py --query "国金今天持仓"
  echo '{"event":{"message":{"content":"{\"text\":\"东莞委托\"}"}}}' | python3 feishu_plugin_bridge.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from domain.services.feishu_bridge_service import build_bridge_response


def _arg_value(name: str, args: list[str]) -> str:
    for idx, arg in enumerate(args):
        if arg == name and idx + 1 < len(args):
            return args[idx + 1].strip()
        if arg.startswith(f"{name}="):
            return arg.split("=", 1)[1].strip()
    return ""


def _load_payload(args: list[str]) -> dict:
    query = _arg_value("--query", args)
    if query:
        return {"query": query}

    raw = ""
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
        return {"query": str(payload)}
    except Exception:
        return {"query": raw}


def main() -> int:
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__.strip())
        return 0

    payload = _load_payload(args)
    result = build_bridge_response(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

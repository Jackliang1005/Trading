#!/usr/bin/env python3
"""Feishu plugin query entrypoint (no direct Feishu OpenAPI calls)."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from domain.services.feishu_query_service import handle_feishu_query


def main() -> int:
    query = " ".join(sys.argv[1:]).strip()
    if not query:
        print("usage: python3 feishu_plugin_query.py \"国金今天持仓\"")
        return 2
    payload = {
        "query": query,
        "reply": handle_feishu_query(query),
        "channel": "feishu-plugin",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

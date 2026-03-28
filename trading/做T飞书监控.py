#!/usr/bin/env python3
"""
做T半自动监控入口。

实际逻辑已迁移到 trading_core.engine，保留当前脚本名用于兼容现有 cron/手动命令。
"""

from trading_core.engine import main


if __name__ == "__main__":
    raise SystemExit(main())

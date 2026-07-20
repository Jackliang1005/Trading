#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path


QMTTRADER_ROOT = Path(
    os.getenv(
        "QMTTRADER_V2_ROOT",
        os.getenv("QMTTRADER_ROOT", "/root/qmttrader_v2"),
    )
).resolve()
QMTTRADER_LOG_ROOT = Path(
    os.getenv(
        "QMTTRADER_V2_LOG_ROOT",
        os.getenv("QMTTRADER_LOG_ROOT", str(QMTTRADER_ROOT / "logs")),
    )
).resolve()
QMTTRADER_RUNTIME_ROOT = Path(
    os.getenv(
        "QMTTRADER_V2_RUNTIME_ROOT",
        os.getenv("QMTTRADER_RUNTIME_ROOT", str(QMTTRADER_ROOT / "runtime")),
    )
).resolve()

#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path


QMTTRADER_ROOT = Path(os.getenv("QMTTRADER_ROOT", "/root/qmttrader")).resolve()
QMTTRADER_LOG_ROOT = Path(
    os.getenv("QMTTRADER_LOG_ROOT", str(QMTTRADER_ROOT / "logs"))
).resolve()
QMTTRADER_RUNTIME_ROOT = QMTTRADER_LOG_ROOT / "runtime"

#!/usr/bin/env python3
"""Update qmttrader_v2 local THS hot concept database."""
from __future__ import annotations

import argparse
import json
import importlib.util
from datetime import date, timedelta
from pathlib import Path

QMTTRADER_V2_ROOT = Path("/root/qmttrader_v2")
CONCEPTS_MODULE = QMTTRADER_V2_ROOT / "data" / "concepts.py"
spec = importlib.util.spec_from_file_location("qmttrader_v2_concepts", CONCEPTS_MODULE)
if spec is None or spec.loader is None:
    raise RuntimeError(f"failed to load concepts module: {CONCEPTS_MODULE}")
concepts_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(concepts_module)
default_concept_db_path = concepts_module.default_concept_db_path
update_concepts_db = concepts_module.update_concepts_db


def previous_weekday(value: date) -> date:
    current = value
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description="Update qmttrader_v2 concept_db/concepts.db")
    parser.add_argument("--date", default="", help="target date, YYYYMMDD or YYYY-MM-DD; defaults to today or previous weekday")
    parser.add_argument("--db", default=str(default_concept_db_path()), help="target sqlite db path")
    args = parser.parse_args()

    target = args.date.strip()
    if not target:
        target = previous_weekday(date.today()).strftime("%Y%m%d")
    result = update_concepts_db(target_date=target, db_path=args.db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

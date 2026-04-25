#!/usr/bin/env python3
"""Shared qmt2http auth helpers for collectors."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict


TOKEN_FILES = (
    "/root/qmt2http/qmt2http_main.env",
    "/root/qmt2http/.env",
    "/root/.openclaw/workspace/investor/.env",
)


def resolve_qmt_api_token() -> str:
    token = os.getenv("QMT2HTTP_API_TOKEN", "").strip()
    if token:
        return token
    for file_path in TOKEN_FILES:
        path = Path(file_path)
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for line in lines:
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            if key.strip() != "QMT2HTTP_API_TOKEN":
                continue
            candidate = value.strip().strip('"').strip("'")
            if candidate:
                return candidate
    return ""


def build_qmt_auth_headers() -> Dict[str, str]:
    headers = {"Accept": "application/json"}
    token = resolve_qmt_api_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-API-Token"] = token
    return headers

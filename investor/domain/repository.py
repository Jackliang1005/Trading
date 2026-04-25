#!/usr/bin/env python3
"""Repository layer for analysis and prediction/evaluation read models."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import db


class PredictionEvaluationRepository:
    """Read/write facade for prediction evaluation workflows."""

    def get_checked_predictions_in_range(self, start: str, end: str) -> List[Dict]:
        return db.get_checked_predictions_in_range(start, end)

    def get_strategy_performance(self, start: str, end: str) -> List[Dict]:
        return db.get_strategy_performance(start, end)

    def get_overall_stats(self, start: str, end: str) -> Dict:
        return db.get_overall_stats(start, end)


def get_prediction_evaluation_repository() -> PredictionEvaluationRepository:
    return PredictionEvaluationRepository()


class AnalysisContextRepository:
    """Read facade for packetized analysis context."""

    def get_latest_bundle(
        self,
        as_of_date: Optional[str] = None,
        account_scope: str = "combined",
        packet_types: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return db.get_latest_analysis_context_bundle(
            as_of_date=as_of_date,
            account_scope=account_scope,
            packet_types=packet_types,
        )

    def summarize_bundle(
        self,
        as_of_date: Optional[str] = None,
        account_scope: str = "combined",
        packet_types: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return db.summarize_analysis_context_bundle(
            as_of_date=as_of_date,
            account_scope=account_scope,
            packet_types=packet_types,
        )


def get_analysis_context_repository() -> AnalysisContextRepository:
    return AnalysisContextRepository()


class LiveMonitorRepository:
    """Read/write facade for live monitor persistence."""

    def ensure_tables(self) -> None:
        conn = db.get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS live_monitor_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                data TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS live_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                severity TEXT NOT NULL,
                kind TEXT NOT NULL,
                signature TEXT DEFAULT '',
                summary TEXT NOT NULL,
                action TEXT DEFAULT '',
                evidence TEXT DEFAULT '{}',
                status TEXT DEFAULT 'open'
            );

            CREATE TABLE IF NOT EXISTS codex_fix_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                incident_signature TEXT NOT NULL,
                severity TEXT NOT NULL,
                summary TEXT NOT NULL,
                suspicious_files TEXT DEFAULT '[]',
                suggested_commands TEXT DEFAULT '[]',
                evidence TEXT DEFAULT '{}',
                status TEXT DEFAULT 'open'
            );
            """
        )
        live_incident_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(live_incidents)").fetchall()
        }
        if "signature" not in live_incident_cols:
            conn.execute("ALTER TABLE live_incidents ADD COLUMN signature TEXT DEFAULT ''")
        conn.commit()
        conn.close()

    def save_snapshot(self, snapshot: Dict) -> int:
        conn = db.get_conn()
        cur = conn.execute(
            "INSERT INTO live_monitor_snapshots (data) VALUES (?)",
            (json.dumps(snapshot, ensure_ascii=False),),
        )
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return int(row_id)

    def save_incidents(self, incidents: List[Dict]) -> List[int]:
        conn = db.get_conn()
        ids: List[int] = []
        for item in incidents:
            signature = item.get("signature", f"{item.get('kind', 'unknown')}::{item.get('summary', '')}")
            evidence_json = json.dumps(item.get("evidence", {}), ensure_ascii=False, sort_keys=True)
            existing = conn.execute(
                """SELECT id FROM live_incidents
                   WHERE signature=? AND status='open'
                   ORDER BY id DESC LIMIT 1""",
                (signature,),
            ).fetchone()
            if existing:
                ids.append(int(existing[0]))
                continue
            cur = conn.execute(
                """INSERT INTO live_incidents (severity, kind, signature, summary, action, evidence)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    item.get("severity", "P2"),
                    item.get("kind", "unknown"),
                    signature,
                    item.get("summary", ""),
                    item.get("action", ""),
                    evidence_json,
                ),
            )
            ids.append(int(cur.lastrowid))
        conn.commit()
        conn.close()
        return ids

    @staticmethod
    def _extract_suspicious_files(item: Dict) -> List[str]:
        evidence = item.get("evidence", {})
        files = []
        if isinstance(evidence, dict):
            path = evidence.get("path")
            if isinstance(path, str) and path:
                files.append(path)
            matches = evidence.get("matches", [])
            for line in matches:
                text = str(line)
                for module_prefix in ("jq_engine_v2.", "jq_engine.", "strategies.", "core.", "adapters."):
                    marker = f"{module_prefix}"
                    if marker in text:
                        start = text.find(marker)
                        end = text.find(":", start)
                        module_name = text[start:end] if end != -1 else text[start:]
                        candidate = "/root/qmttrader/" + module_name.replace(".", "/") + ".py"
                        files.append(candidate)
                if "/root/qmttrader/" in text:
                    start = text.find("/root/qmttrader/")
                    end = len(text)
                    for stop in (" ", "\"", "'", ",", ":", ")"):
                        pos = text.find(stop, start)
                        if pos != -1:
                            end = min(end, pos)
                    files.append(text[start:end])
        normalized = []
        for path in files:
            if path not in normalized:
                normalized.append(path)
        return normalized[:10]

    @staticmethod
    def _suggest_commands(files: List[str]) -> List[str]:
        commands = []
        for path in files[:5]:
            commands.append(f"sed -n '1,220p' {path}")
        if not commands:
            commands.append("rg -n 'Traceback|ERROR|CRITICAL|ImportError|AttributeError|KeyError|TypeError' /root/qmttrader -S")
        return commands

    def save_codex_fix_tasks(self, incidents: List[Dict]) -> List[int]:
        conn = db.get_conn()
        ids: List[int] = []
        for item in incidents:
            if item.get("action") != "codex_fix_task":
                continue
            evidence = item.get("evidence", {})
            signature = item.get("signature", f"{item.get('kind', 'unknown')}::{item.get('summary', '')}")
            existing = conn.execute(
                """SELECT id FROM codex_fix_runs
                   WHERE incident_signature=? AND status='open'
                   ORDER BY id DESC LIMIT 1""",
                (signature,),
            ).fetchone()
            if existing:
                ids.append(int(existing[0]))
                continue
            suspicious_files = self._extract_suspicious_files(item)
            suggested_commands = self._suggest_commands(suspicious_files)
            cur = conn.execute(
                """INSERT INTO codex_fix_runs
                   (incident_signature, severity, summary, suspicious_files, suggested_commands, evidence, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'open')""",
                (
                    signature,
                    item.get("severity", "P2"),
                    item.get("summary", ""),
                    json.dumps(suspicious_files, ensure_ascii=False),
                    json.dumps(suggested_commands, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                ),
            )
            ids.append(int(cur.lastrowid))
        conn.commit()
        conn.close()
        return ids


def get_live_monitor_repository() -> LiveMonitorRepository:
    return LiveMonitorRepository()

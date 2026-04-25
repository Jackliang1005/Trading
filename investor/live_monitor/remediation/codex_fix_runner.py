#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import subprocess
import shlex
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from live_monitor.remediation.escalation_policy import should_require_human_review
from live_monitor.remediation.patch_validator import build_validation_plan


DB_PATH = Path("/root/.openclaw/workspace/investor/data/investor.db")
QMTTRADER_ROOT = Path("/root/qmttrader")


def _ensure_fix_task_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(codex_fix_runs)").fetchall()}
    if "status_updated_at" not in cols:
        try:
            conn.execute("ALTER TABLE codex_fix_runs ADD COLUMN status_updated_at TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise
    if "status_note" not in cols:
        try:
            conn.execute("ALTER TABLE codex_fix_runs ADD COLUMN status_note TEXT DEFAULT ''")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise
    if "owner" not in cols:
        try:
            conn.execute("ALTER TABLE codex_fix_runs ADD COLUMN owner TEXT DEFAULT ''")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise
    if "status_history" not in cols:
        try:
            conn.execute("ALTER TABLE codex_fix_runs ADD COLUMN status_history TEXT DEFAULT '[]'")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS codex_fix_validation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            run_group TEXT NOT NULL DEFAULT '',
            task_id INTEGER NOT NULL,
            command TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            ok INTEGER NOT NULL,
            triggered_by TEXT DEFAULT '',
            output TEXT DEFAULT '',
            FOREIGN KEY (task_id) REFERENCES codex_fix_runs(id)
        )
        """
    )
    validation_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(codex_fix_validation_runs)").fetchall()
    }
    if "run_group" not in validation_cols:
        try:
            conn.execute("ALTER TABLE codex_fix_validation_runs ADD COLUMN run_group TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise
    if "triggered_by" not in validation_cols:
        try:
            conn.execute("ALTER TABLE codex_fix_validation_runs ADD COLUMN triggered_by TEXT DEFAULT ''")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise
    conn.commit()


def _hydrate_task(row: sqlite3.Row) -> Dict:
    item = dict(row)
    for key in ("suspicious_files", "suggested_commands", "evidence", "status_history"):
        try:
            if key == "evidence":
                item[key] = json.loads(item[key]) if item.get(key) else {}
            else:
                item[key] = json.loads(item[key]) if item.get(key) else []
        except Exception:
            item[key] = {} if key == "evidence" else []
    item["requires_human_review"] = should_require_human_review(item)
    item["validation_plan"] = build_validation_plan(item)
    return item


def _decode_json(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except Exception:
        return fallback


def _run_command(parts: List[str], cwd: Optional[Path] = None) -> Dict:
    try:
        proc = subprocess.run(
            parts,
            check=False,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
        )
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": str(exc),
        }


def _get_related_incidents(conn: sqlite3.Connection, incident_signature: str, limit: int = 3) -> List[Dict]:
    if not incident_signature:
        return []
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, created_at, severity, kind, signature, summary, action, status
           FROM live_incidents
           WHERE signature=?
           ORDER BY id DESC
           LIMIT ?""",
        (incident_signature, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _summarize_snapshot_payload(payload: Dict, evidence_path: str = "") -> Dict:
    qmt_health = payload.get("qmt_health", {}) or {}
    qmt_trade_log = payload.get("qmt_trade_log", {}) or {}
    runtime_status = payload.get("runtime_status", {}) or {}
    observability = payload.get("observability", {}) or {}
    strategy_logs = payload.get("strategy_logs", {}) or {}
    strategy_entries = strategy_logs.get("entries", []) or []
    matching_logs = []
    if evidence_path:
        matching_logs = [entry.get("path", "") for entry in strategy_entries if entry.get("path") == evidence_path]
    return {
        "captured_at": payload.get("captured_at", ""),
        "qmt_server_count": len(qmt_health.get("servers", []) or []),
        "trade_log_server_count": len(qmt_trade_log.get("servers", []) or []),
        "runtime_status_count": len(runtime_status.get("statuses", []) or []),
        "observability_count": len(observability.get("entries", []) or []),
        "strategy_log_count": len(strategy_entries),
        "matched_strategy_logs": matching_logs,
    }


def _get_recent_snapshots(conn: sqlite3.Connection, evidence_path: str = "", limit: int = 3) -> List[Dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, created_at, data
           FROM live_monitor_snapshots
           ORDER BY id DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    snapshots = []
    for row in rows:
        payload = _decode_json(row["data"], {})
        snapshots.append(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "summary": _summarize_snapshot_payload(payload, evidence_path=evidence_path),
            }
        )
    return snapshots


def _collect_file_repo_status(path_str: str) -> Dict:
    path = Path(path_str)
    item = {
        "path": path_str,
        "exists": path.exists(),
        "repo_tracked": False,
        "git_status": "",
        "diff_stat": "",
        "diff_preview": [],
    }
    try:
        rel = path.relative_to(QMTTRADER_ROOT)
    except Exception:
        return item
    if not QMTTRADER_ROOT.exists():
        return item
    ls_files = _run_command(["git", "ls-files", "--error-unmatch", str(rel)], cwd=QMTTRADER_ROOT)
    item["repo_tracked"] = ls_files["ok"]
    status = _run_command(["git", "status", "--short", "--", str(rel)], cwd=QMTTRADER_ROOT)
    item["git_status"] = status["stdout"]
    if item["repo_tracked"]:
        diff = _run_command(["git", "diff", "--shortstat", "--", str(rel)], cwd=QMTTRADER_ROOT)
        cached = _run_command(["git", "diff", "--cached", "--shortstat", "--", str(rel)], cwd=QMTTRADER_ROOT)
        item["diff_stat"] = diff["stdout"] or cached["stdout"]
        diff_preview = _run_command(["git", "diff", "--unified=0", "--", str(rel)], cwd=QMTTRADER_ROOT)
        preview_lines = []
        for line in (diff_preview["stdout"] or "").splitlines():
            if line.startswith(("diff --git", "index ", "--- ", "+++ ")):
                continue
            if line.startswith("@@") or line.startswith("+") or line.startswith("-"):
                preview_lines.append(line)
            if len(preview_lines) >= 20:
                break
        item["diff_preview"] = preview_lines
    return item


def _get_suspicious_file_context(paths: List[str]) -> List[Dict]:
    return [_collect_file_repo_status(path) for path in paths[:10]]


def _read_file_head(path_str: str, max_lines: int = 120, max_chars: int = 6000) -> Dict:
    path = Path(path_str)
    item = {
        "path": path_str,
        "exists": path.exists(),
        "content": "",
        "line_count": 0,
    }
    if not path.exists() or not path.is_file():
        return item
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = []
            for idx, line in enumerate(handle):
                if idx >= max_lines:
                    break
                lines.append(line)
        content = "".join(lines)
        if len(content) > max_chars:
            content = content[:max_chars]
        item["content"] = content
        item["line_count"] = len(lines)
    except Exception as exc:
        item["content"] = f"<read_failed: {exc}>"
    return item


def _build_validation_summary(conn: sqlite3.Connection, task_id: int) -> Dict:
    latest_group = conn.execute(
        """SELECT run_group, MAX(created_at) AS latest_at
           FROM codex_fix_validation_runs
           WHERE task_id=?
           GROUP BY run_group
           ORDER BY MAX(created_at) DESC
           LIMIT 1""",
        (task_id,),
    ).fetchone()
    totals = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END), 0)
           FROM codex_fix_validation_runs
           WHERE task_id=?""",
        (task_id,),
    ).fetchone()
    summary = {
        "total_runs": int(totals[0] or 0),
        "total_ok": int(totals[1] or 0),
        "total_failed": int((totals[0] or 0) - (totals[1] or 0)),
        "latest_group": None,
    }
    if latest_group is None:
        return summary
    latest_rows = conn.execute(
        """SELECT run_group, created_at, command, exit_code, ok, triggered_by
           FROM codex_fix_validation_runs
           WHERE task_id=? AND run_group=?
           ORDER BY id DESC""",
        (task_id, latest_group["run_group"]),
    ).fetchall()
    latest_total = len(latest_rows)
    latest_ok = sum(1 for row in latest_rows if int(row["ok"]) == 1)
    summary["latest_group"] = {
        "run_group": latest_group["run_group"],
        "created_at": latest_group["latest_at"],
        "count": latest_total,
        "ok_count": latest_ok,
        "failed_count": latest_total - latest_ok,
        "overall_ok": latest_total > 0 and latest_ok == latest_total,
        "triggered_by": latest_rows[0]["triggered_by"] if latest_rows else "",
        "commands": [row["command"] for row in latest_rows],
    }
    return summary


def list_fix_tasks(limit: int = 20, status: str = "open") -> List[Dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_fix_task_columns(conn)
    if status == "all":
        rows = conn.execute(
            """SELECT id, created_at, incident_signature, severity, summary,
                      suspicious_files, suggested_commands, evidence, status,
                      status_updated_at, status_note, owner, status_history
               FROM codex_fix_runs
               ORDER BY id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, created_at, incident_signature, severity, summary,
                      suspicious_files, suggested_commands, evidence, status,
                      status_updated_at, status_note, owner, status_history
               FROM codex_fix_runs
               WHERE status=?
               ORDER BY id DESC
               LIMIT ?""",
            (status, limit),
        ).fetchall()
    conn.close()
    return [_hydrate_task(row) for row in rows]


def list_open_fix_tasks(limit: int = 20) -> List[Dict]:
    return list_fix_tasks(limit=limit, status="open")


def get_fix_task(task_id: int) -> Optional[Dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_fix_task_columns(conn)
    row = conn.execute(
        """SELECT id, created_at, incident_signature, severity, summary,
                  suspicious_files, suggested_commands, evidence, status,
                  status_updated_at, status_note, owner, status_history
           FROM codex_fix_runs
           WHERE id=?""",
        (task_id,),
    ).fetchone()
    if row is None:
        conn.close()
        return None
    item = _hydrate_task(row)
    item["validation_summary"] = _build_validation_summary(conn, task_id)
    item["validation_groups"] = get_fix_task_validation_groups(task_id, limit=5)
    evidence_path = ""
    if isinstance(item.get("evidence"), dict):
        evidence_path = str(item["evidence"].get("path", "") or "")
    item["related_incidents"] = _get_related_incidents(conn, item.get("incident_signature", ""), limit=3)
    item["recent_snapshots"] = _get_recent_snapshots(conn, evidence_path=evidence_path, limit=3)
    item["suspicious_file_context"] = _get_suspicious_file_context(item.get("suspicious_files", []))
    conn.close()
    return item


def get_fix_task_context(task_id: int) -> Optional[Dict]:
    task = get_fix_task(task_id)
    if task is None:
        return None
    return {
        "id": task["id"],
        "severity": task.get("severity", ""),
        "summary": task.get("summary", ""),
        "status": task.get("status", ""),
        "status_updated_at": task.get("status_updated_at", ""),
        "status_note": task.get("status_note", ""),
        "owner": task.get("owner", ""),
        "incident_signature": task.get("incident_signature", ""),
        "evidence_path": (task.get("evidence") or {}).get("path", ""),
        "top_matches": list(((task.get("evidence") or {}).get("matches") or [])[:5]),
        "related_incidents": list((task.get("related_incidents") or [])[:3]),
        "recent_snapshots": list((task.get("recent_snapshots") or [])[:3]),
        "suspicious_file_context": list((task.get("suspicious_file_context") or [])[:5]),
        "validation_summary": task.get("validation_summary", {}),
        "validation_groups": list((task.get("validation_groups") or [])[:3]),
        "suggested_commands": list((task.get("suggested_commands") or [])[:5]),
        "validation_plan": list((task.get("validation_plan") or [])[:5]),
        "requires_human_review": bool(task.get("requires_human_review", False)),
    }


def get_fix_task_pack(task_id: int) -> Optional[Dict]:
    task = get_fix_task(task_id)
    if task is None:
        return None
    evidence = task.get("evidence") or {}
    validation_summary = task.get("validation_summary") or {}
    latest_group = validation_summary.get("latest_group") or {}
    suspicious_context = list(task.get("suspicious_file_context") or [])
    tracked_files = [item for item in suspicious_context if item.get("repo_tracked")]
    changed_files = [item for item in suspicious_context if item.get("git_status") or item.get("diff_stat")]
    pack = {
        "task": {
            "id": task["id"],
            "severity": task.get("severity", ""),
            "summary": task.get("summary", ""),
            "status": task.get("status", ""),
            "status_note": task.get("status_note", ""),
            "owner": task.get("owner", ""),
            "requires_human_review": bool(task.get("requires_human_review", False)),
        },
        "incident": {
            "signature": task.get("incident_signature", ""),
            "evidence_path": evidence.get("path", ""),
            "top_matches": list((evidence.get("matches") or [])[:8]),
            "related_incidents": list((task.get("related_incidents") or [])[:3]),
        },
        "monitoring": {
            "recent_snapshots": list((task.get("recent_snapshots") or [])[:3]),
        },
        "files": {
            "suspicious_files": list((task.get("suspicious_files") or [])[:10]),
            "tracked_count": len(tracked_files),
            "changed_count": len(changed_files),
            "contexts": suspicious_context[:5],
        },
        "validation": {
            "summary": validation_summary,
            "recent_groups": list((task.get("validation_groups") or [])[:3]),
            "latest_group_commands": list((latest_group.get("commands") or [])[:10]),
            "plan": list((task.get("validation_plan") or [])[:8]),
        },
        "execution": {
            "suggested_commands": list((task.get("suggested_commands") or [])[:8]),
            "primary_targets": [item.get("path", "") for item in changed_files[:5]],
        },
    }
    return pack


def get_fix_task_bundle(task_id: int) -> Optional[Dict]:
    pack = get_fix_task_pack(task_id)
    task = get_fix_task(task_id)
    if pack is None or task is None:
        return None
    validation_runs = get_fix_task_validation_runs(task_id, limit=5)
    recent_validation_outputs = []
    for run in validation_runs[:3]:
        recent_validation_outputs.append(
            {
                "id": run.get("id"),
                "created_at": run.get("created_at", ""),
                "run_group": run.get("run_group", ""),
                "command": run.get("command", ""),
                "ok": bool(run.get("ok")),
                "exit_code": run.get("exit_code"),
                "output_excerpt": str(run.get("output", "") or "")[:2500],
            }
        )
    suspicious_files = list((task.get("suspicious_files") or [])[:5])
    primary_targets = list((pack.get("execution", {}) or {}).get("primary_targets", [])[:5])
    snippet_targets = []
    for path in primary_targets + suspicious_files:
        if path not in snippet_targets:
            snippet_targets.append(path)
    file_snippets = [_read_file_head(path) for path in snippet_targets[:4]]
    bundle = {
        "bundle_version": "v1",
        "task_id": task_id,
        "pack": pack,
        "recent_validation_outputs": recent_validation_outputs,
        "file_snippets": file_snippets,
        "agent_prompt_hints": {
            "goal": pack.get("task", {}).get("summary", ""),
            "primary_targets": primary_targets,
            "validation_plan": list((pack.get("validation", {}) or {}).get("plan", [])[:5]),
            "suggested_commands": list((pack.get("execution", {}) or {}).get("suggested_commands", [])[:5]),
        },
    }
    return bundle


def export_fix_task_bundle(task_id: int, output_path: str) -> Dict:
    bundle = get_fix_task_bundle(task_id)
    if bundle is None:
        return {
            "task_id": task_id,
            "found": False,
            "written": False,
            "path": output_path,
        }
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {
        "task_id": task_id,
        "found": True,
        "written": True,
        "path": str(path),
        "bytes": path.stat().st_size,
    }


def update_fix_task_status(task_id: int, status: str, note: Optional[str] = None, owner: Optional[str] = None) -> bool:
    allowed = {"open", "acknowledged", "patched", "closed"}
    if status not in allowed:
        raise ValueError(f"unsupported status: {status}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_fix_task_columns(conn)
    row = conn.execute(
        "SELECT status, status_history, owner FROM codex_fix_runs WHERE id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        conn.close()
        return False

    current_status = (row["status"] or "open").strip() if row["status"] else "open"
    transition_whitelist = {
        "open": {"open", "acknowledged"},
        "acknowledged": {"open", "acknowledged", "patched"},
        "patched": {"open", "acknowledged", "patched", "closed"},
        "closed": {"open", "closed"},
    }
    allowed_next = transition_whitelist.get(current_status, {"open", "acknowledged", "patched", "closed"})
    if status not in allowed_next:
        conn.close()
        return False

    try:
        history = json.loads(row["status_history"]) if row["status_history"] else []
    except Exception:
        history = []
    history.append(
        {
            "status": status,
            "note": note or "",
            "owner": owner or row["owner"] or "",
        }
    )
    final_owner = owner if owner is not None else row["owner"]
    cur = conn.execute(
        """UPDATE codex_fix_runs
           SET status=?, status_updated_at=datetime('now'), status_note=?, owner=?, status_history=?
           WHERE id=?""",
        (status, note or "", final_owner or "", json.dumps(history, ensure_ascii=False), task_id),
    )
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


def add_fix_task_note(task_id: int, note: str, owner: Optional[str] = None) -> bool:
    existing = get_fix_task(task_id)
    if existing is None:
        return False
    current_status = existing.get("status", "open")
    current_owner = existing.get("owner", "")
    return update_fix_task_status(
        task_id,
        current_status,
        note=note,
        owner=owner if owner is not None else current_owner,
    )


def summarize_fix_tasks() -> Dict[str, int]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM codex_fix_runs GROUP BY status"
    ).fetchall()
    conn.close()
    summary = {"all": 0}
    for status, count in rows:
        summary[str(status)] = int(count)
        summary["all"] += int(count)
    for status in ("open", "acknowledged", "patched", "closed"):
        summary.setdefault(status, 0)
    return summary


def _is_safe_validation_command(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    if parts[0] == "sed" and len(parts) >= 3 and parts[1] == "-n":
        return True
    if parts[:3] == ["python3", "-m", "py_compile"]:
        return True
    if parts[0] == "rg":
        return True
    return False


def run_fix_task_validation(task_id: int, limit: Optional[int] = None, triggered_by: str = "codex") -> Dict:
    task = get_fix_task(task_id)
    if task is None:
        return {"task_id": task_id, "found": False}

    commands = list(task.get("validation_plan", []) or [])
    if limit is not None:
        commands = commands[:limit]

    conn = sqlite3.connect(DB_PATH)
    _ensure_fix_task_columns(conn)
    run_group = uuid.uuid4().hex[:12]

    results = []
    overall_ok = True
    for command in commands:
        if not _is_safe_validation_command(command):
            result = {
                "command": command,
                "ok": False,
                "exit_code": -1,
                "output": "blocked: command is not in validation allowlist",
            }
            overall_ok = False
        else:
            try:
                proc = subprocess.run(
                    shlex.split(command),
                    check=False,
                    capture_output=True,
                    text=True,
                )
                output = (proc.stdout or "") + (proc.stderr or "")
                result = {
                    "command": command,
                    "ok": proc.returncode == 0,
                    "exit_code": proc.returncode,
                    "output": output[:4000],
                }
                if proc.returncode != 0:
                    overall_ok = False
            except Exception as exc:
                result = {
                    "command": command,
                    "ok": False,
                    "exit_code": -1,
                    "output": str(exc),
                }
                overall_ok = False

        conn.execute(
            """INSERT INTO codex_fix_validation_runs (run_group, task_id, command, exit_code, ok, triggered_by, output)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                run_group,
                task_id,
                result["command"],
                result["exit_code"],
                1 if result["ok"] else 0,
                triggered_by,
                result["output"],
            ),
        )
        results.append(result)

    conn.commit()
    conn.close()
    return {
        "task_id": task_id,
        "found": True,
        "run_group": run_group,
        "overall_ok": overall_ok,
        "validation_count": len(results),
        "results": results,
    }


def get_fix_task_validation_runs(task_id: int, limit: int = 20, run_group: Optional[str] = None) -> List[Dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_fix_task_columns(conn)
    if run_group:
        rows = conn.execute(
            """SELECT id, created_at, run_group, task_id, command, exit_code, ok, triggered_by, output
               FROM codex_fix_validation_runs
               WHERE task_id=? AND run_group=?
               ORDER BY id DESC
               LIMIT ?""",
            (task_id, run_group, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, created_at, run_group, task_id, command, exit_code, ok, triggered_by, output
               FROM codex_fix_validation_runs
               WHERE task_id=?
               ORDER BY id DESC
               LIMIT ?""",
            (task_id, limit),
        ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_fix_task_validation_groups(task_id: int, limit: int = 10) -> List[Dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_fix_task_columns(conn)
    rows = conn.execute(
        """SELECT run_group,
                  MAX(created_at) AS created_at,
                  COUNT(*) AS command_count,
                  COALESCE(SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END), 0) AS ok_count,
                  COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END), 0) AS failed_count,
                  MAX(triggered_by) AS triggered_by
           FROM codex_fix_validation_runs
           WHERE task_id=?
           GROUP BY run_group
           ORDER BY MAX(created_at) DESC
           LIMIT ?""",
        (task_id, limit),
    ).fetchall()
    conn.close()
    groups = []
    for row in rows:
        item = dict(row)
        item["overall_ok"] = int(item["failed_count"] or 0) == 0 and int(item["command_count"] or 0) > 0
        groups.append(item)
    return groups


def promote_fix_task_from_validation(task_id: int, target_status: str = "patched", note: str = "") -> Dict:
    if target_status not in {"patched", "closed"}:
        raise ValueError("target_status must be patched or closed")
    task = get_fix_task(task_id)
    if task is None:
        return {
            "task_id": task_id,
            "updated": False,
            "target_status": target_status,
            "reason": "task_not_found",
        }
    current_status = str(task.get("status", "open"))

    group_limit = 2 if target_status == "closed" else 1
    groups = get_fix_task_validation_groups(task_id, limit=group_limit)
    if not groups:
        return {
            "task_id": task_id,
            "updated": False,
            "target_status": target_status,
            "reason": "no_validation_runs",
        }

    latest = groups[0]
    if not latest["overall_ok"]:
        return {
            "task_id": task_id,
            "updated": False,
            "target_status": target_status,
            "reason": "latest_validation_failed",
            "latest_group": latest,
        }

    if target_status == "closed":
        if current_status != "patched":
            return {
                "task_id": task_id,
                "updated": False,
                "target_status": target_status,
                "reason": "must_be_patched_before_close",
                "current_status": current_status,
                "latest_group": latest,
            }
        if len(groups) < 2:
            return {
                "task_id": task_id,
                "updated": False,
                "target_status": target_status,
                "reason": "need_two_validation_groups_for_close",
                "latest_group": latest,
            }
        previous = groups[1]
        if not previous["overall_ok"]:
            return {
                "task_id": task_id,
                "updated": False,
                "target_status": target_status,
                "reason": "previous_validation_failed",
                "latest_group": latest,
                "previous_group": previous,
            }

    final_note = note or f"validated by run_group={latest['run_group']}"
    updated = update_fix_task_status(task_id, target_status, note=final_note)
    return {
        "task_id": task_id,
        "updated": updated,
        "target_status": target_status,
        "latest_group": latest,
        "status_note": final_note,
    }

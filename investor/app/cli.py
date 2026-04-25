#!/usr/bin/env python3
"""New CLI entrypoint for incremental investor refactor."""

from __future__ import annotations

import json
import sys

import db
from domain.services.prediction_orchestrator import parse_predictions
from domain.services.prediction_service import save_predictions
from live_monitor.remediation.codex_fix_runner import (
    add_fix_task_note,
    export_fix_task_bundle,
    get_fix_task,
    get_fix_task_bundle,
    get_fix_task_context,
    get_fix_task_pack,
    get_fix_task_validation_groups,
    get_fix_task_validation_runs,
    list_fix_tasks,
    promote_fix_task_from_validation,
    run_fix_task_validation,
    summarize_fix_tasks,
    update_fix_task_status,
)
from domain.services.live_monitor_service import run_live_monitor
from domain.services.live_monitor_view_service import (
    format_today_summary_text,
    get_today_account,
    get_today_buys,
    get_today_candidates,
    get_today_summary,
    run_trading_monitor,
)
from domain.services.feishu_query_service import handle_feishu_query
from domain.services.feishu_bridge_service import build_bridge_response
from workflows.daily_maintenance import run_daily_maintenance
from workflows.backfill_packets import backfill_packets
from workflows.packet_maintenance import run_packet_maintenance
from workflows.runtime_check import run_runtime_check
from workflows.run_smoke_checks import run_smoke_checks
from workflows.scheduled_briefings import run_scheduled_briefing
from workflows.sync_handoff_snapshot import sync_handoff_snapshot


NEW_COMMAND_SPECS = {
    "help": {
        "description": "查看新 CLI 命令帮助",
        "usage": "python3 main.py help [command]",
    },
    "record": {
        "description": "记录预测 (--json '[...]' 或 stdin)",
        "usage": "python3 main.py record --json '[{\"code\":\"sh000001\",\"name\":\"上证指数\",\"direction\":\"up\",\"confidence\":0.6,\"predicted_change\":0.5,\"strategy_used\":\"technical\",\"reasoning\":\"...\"}]'",
    },
    "monitor": {
        "description": "实盘监控（qmt2http + qmttrader）",
        "usage": "python3 main.py monitor [YYYY-MM-DD|YYYYMMDD]",
    },
    "monitor-trading": {
        "description": "交易监控视图（候选/买入/持仓读口）",
        "usage": "python3 main.py monitor-trading [YYYY-MM-DD|YYYYMMDD]",
    },
    "runtime-check": {
        "description": "运行诊断（qmt2http 健康/交易读口/日志）",
        "usage": "python3 main.py runtime-check [YYYY-MM-DD|YYYYMMDD]",
    },
    "today-candidates": {
        "description": "查看最新候选与最终选股",
        "usage": "python3 main.py today-candidates [YYYY-MM-DD|YYYYMMDD]",
    },
    "today-buys": {
        "description": "查看最新买入提交与成交摘要",
        "usage": "python3 main.py today-buys [YYYY-MM-DD|YYYYMMDD]",
    },
    "today-account": {
        "description": "查看双账户读口与对账状态",
        "usage": "python3 main.py today-account [YYYY-MM-DD|YYYYMMDD]",
    },
    "today-summary": {
        "description": "查看候选/买入/账户/告警简报（支持日期与 --text）",
        "usage": "python3 main.py today-summary [YYYY-MM-DD|YYYYMMDD] [--text]",
    },
    "fix-task-summary": {
        "description": "查看修复任务状态摘要",
        "usage": "python3 main.py fix-task-summary",
    },
    "fix-tasks": {
        "description": "查看 Codex 修复任务（默认 open，可传 all/patched/closed）",
        "usage": "python3 main.py fix-tasks [open|acknowledged|patched|closed|all] [limit]",
    },
    "fix-task-show": {
        "description": "查看单个修复任务详情",
        "usage": "python3 main.py fix-task-show <task_id>",
    },
    "fix-task-context": {
        "description": "查看修复任务精简排障上下文",
        "usage": "python3 main.py fix-task-context <task_id>",
    },
    "fix-task-pack": {
        "description": "查看修复任务自动修复 payload",
        "usage": "python3 main.py fix-task-pack <task_id>",
    },
    "fix-task-bundle": {
        "description": "查看修复任务可投喂 agent 的最小输入",
        "usage": "python3 main.py fix-task-bundle <task_id>",
    },
    "fix-task-export": {
        "description": "导出修复任务 bundle 到 JSON 文件",
        "usage": "python3 main.py fix-task-export <task_id> <path>",
    },
    "fix-task-run-validation": {
        "description": "执行修复任务验证计划",
        "usage": "python3 main.py fix-task-run-validation <task_id> [limit]",
    },
    "fix-task-validations": {
        "description": "查看修复任务验证记录",
        "usage": "python3 main.py fix-task-validations <task_id> [limit]",
    },
    "fix-task-validation-groups": {
        "description": "查看修复任务验证批次摘要",
        "usage": "python3 main.py fix-task-validation-groups <task_id> [limit]",
    },
    "fix-task-promote": {
        "description": "按最新验证结果推进 patched/closed",
        "usage": "python3 main.py fix-task-promote <task_id> [patched|closed] [note]",
    },
    "fix-task-ack": {
        "description": "认领修复任务",
        "usage": "python3 main.py fix-task-ack <task_id> [owner] [note]",
    },
    "fix-task-note": {
        "description": "给修复任务追加备注",
        "usage": "python3 main.py fix-task-note <task_id> <note...>",
    },
    "fix-task-patched": {
        "description": "标记修复任务为已打补丁",
        "usage": "python3 main.py fix-task-patched <task_id> [note]",
    },
    "fix-task-close": {
        "description": "关闭修复任务",
        "usage": "python3 main.py fix-task-close <task_id> [note]",
    },
    "fix-task-reopen": {
        "description": "重新打开修复任务",
        "usage": "python3 main.py fix-task-reopen <task_id> [note]",
    },
    "backfill-packets": {
        "description": "将 market_snapshots 回填为 research/portfolio packets",
        "usage": "python3 main.py backfill-packets [--type daily_close|intraday] [--limit N] [--apply] [--force]",
    },
    "packet-maintain": {
        "description": "执行 daily_close+intraday 增量 packet 回填（默认 apply）",
        "usage": "python3 main.py packet-maintain [--limit N] [--daily-limit N] [--intraday-limit N] [--dry-run] [--force] [--no-write] [--out <path>]",
    },
    "daily-maintain": {
        "description": "日常维护总入口：packet-maintain + handoff-sync",
        "usage": "python3 main.py daily-maintain [--limit N] [--dry-run] [--force] [--no-write] [--skip-runtime-check] [--snapshot <path>] [--handoff <path>]",
    },
    "smoke-check": {
        "description": "执行主线 smoke 验收命令集合",
        "usage": "python3 main.py smoke-check [--strict]",
    },
    "feishu-query": {
        "description": "Feishu plugin 查询入口（示例：'国金今天持仓'）",
        "usage": "python3 main.py feishu-query \"国金今天持仓\"",
    },
    "feishu-bridge": {
        "description": "Feishu plugin 消息桥接（--query 或 stdin JSON）",
        "usage": "python3 main.py feishu-bridge --query \"国金今天持仓\"  # 或 echo '{\"event\":...}' | python3 main.py feishu-bridge",
    },
    "handoff-sync": {
        "description": "将 packet_maintenance_latest.json 同步写入 HANDOFF.md",
        "usage": "python3 main.py handoff-sync [--snapshot <path>] [--handoff <path>]",
    },
    "scheduled-briefing": {
        "description": "定时交易简报（0945东莞策略 / 1320或1420国金ETF）",
        "usage": "python3 main.py scheduled-briefing <0945|1320|1420> [YYYY-MM-DD|YYYYMMDD]",
    },
}


def _arg(index: int, default: str = "") -> str:
    return sys.argv[index] if len(sys.argv) > index else default


def _has_flag(flag: str) -> bool:
    return flag in sys.argv[2:]


def _flag_value(flag: str, default: str = "") -> str:
    args = sys.argv[2:]
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(f"{flag}="):
            return arg.split("=", 1)[1]
    return default


def is_new_command(command: str) -> bool:
    return command in NEW_COMMAND_SPECS


def get_new_command_description(command: str) -> str:
    return str((NEW_COMMAND_SPECS.get(command) or {}).get("description", ""))


def get_new_command_usage(command: str) -> str:
    return str((NEW_COMMAND_SPECS.get(command) or {}).get("usage", ""))


def get_new_command_help(command: str) -> str:
    if not is_new_command(command):
        return ""
    description = get_new_command_description(command)
    usage = get_new_command_usage(command)
    lines = [command]
    if description:
        lines.append(description)
    if usage:
        lines.append(f"usage: {usage}")
    return "\n".join(lines)


def _read_record_json() -> str:
    json_str = ""
    for i, arg in enumerate(sys.argv[2:], 2):
        if arg == "--json" and i + 1 < len(sys.argv):
            json_str = sys.argv[i + 1]
            break
        if arg.startswith("--json="):
            json_str = arg[len("--json="):]
            break
    if not json_str and not sys.stdin.isatty():
        json_str = sys.stdin.read().strip()
    return json_str


def _read_stdin_text() -> str:
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read().strip()


def _run_record_command() -> str:
    db.init_db()
    json_str = _read_record_json()
    if not json_str:
        raise ValueError(get_new_command_usage("record"))
    predictions = parse_predictions(json_str)
    if not predictions:
        return "❌ 未解析到有效预测"

    pred_ids = save_predictions(predictions, model="cron-agent")
    lines = []
    for p, pid in zip(predictions, pred_ids):
        lines.append(
            f"📝 [{p.get('name', p['code'])}] {p['direction']} "
            f"(置信度:{p['confidence']:.0%}, 预测涨跌:{p.get('predicted_change', 0):+.2f}%) → ID:{pid}"
        )
    lines.append(f"✅ 共记录 {len(predictions)} 条预测")
    return "\n".join(lines)


def run_command(command: str):
    if command == "record":
        return _run_record_command()
    if command == "monitor":
        return run_live_monitor(date=_arg(2, ""))
    if command == "monitor-trading":
        return run_trading_monitor(date=_arg(2, ""))
    if command == "runtime-check":
        return run_runtime_check(date=_arg(2, ""))
    if command == "today-candidates":
        return get_today_candidates(date=_arg(2, ""))
    if command == "today-buys":
        return get_today_buys(date=_arg(2, ""))
    if command == "today-account":
        return get_today_account(date=_arg(2, ""))
    if command == "today-summary":
        date = _arg(2, "")
        if date.startswith("--"):
            date = ""
        if _has_flag("--text"):
            return format_today_summary_text(date=date)
        return get_today_summary(date=date)
    if command == "fix-task-summary":
        return {"summary": summarize_fix_tasks()}
    if command == "fix-tasks":
        status = _arg(2, "open")
        limit = int(_arg(3, "20"))
        tasks = list_fix_tasks(status=status, limit=limit)
        return {"tasks": tasks, "count": len(tasks), "status": status, "limit": limit}
    if command == "fix-task-show":
        if len(sys.argv) < 3:
            raise ValueError(get_new_command_usage("fix-task-show"))
        task_id = int(sys.argv[2])
        task = get_fix_task(task_id)
        return {"task": task, "found": task is not None, "task_id": task_id}
    if command == "fix-task-context":
        if len(sys.argv) < 3:
            raise ValueError(get_new_command_usage("fix-task-context"))
        task_id = int(sys.argv[2])
        task = get_fix_task_context(task_id)
        return {"task": task, "found": task is not None, "task_id": task_id}
    if command == "fix-task-pack":
        if len(sys.argv) < 3:
            raise ValueError(get_new_command_usage("fix-task-pack"))
        task_id = int(sys.argv[2])
        task = get_fix_task_pack(task_id)
        return {"task": task, "found": task is not None, "task_id": task_id}
    if command == "fix-task-bundle":
        if len(sys.argv) < 3:
            raise ValueError(get_new_command_usage("fix-task-bundle"))
        task_id = int(sys.argv[2])
        task = get_fix_task_bundle(task_id)
        return {"task": task, "found": task is not None, "task_id": task_id}
    if command == "fix-task-export":
        if len(sys.argv) < 4:
            raise ValueError(get_new_command_usage("fix-task-export"))
        task_id = int(sys.argv[2])
        return export_fix_task_bundle(task_id, sys.argv[3])
    if command == "fix-task-run-validation":
        if len(sys.argv) < 3:
            raise ValueError(get_new_command_usage("fix-task-run-validation"))
        task_id = int(sys.argv[2])
        raw_limit = _arg(3, "")
        limit = int(raw_limit) if raw_limit else None
        return run_fix_task_validation(task_id, limit=limit)
    if command == "fix-task-validations":
        if len(sys.argv) < 3:
            raise ValueError(get_new_command_usage("fix-task-validations"))
        task_id = int(sys.argv[2])
        limit = int(_arg(3, "20"))
        runs = get_fix_task_validation_runs(task_id, limit=limit)
        return {"task_id": task_id, "count": len(runs), "runs": runs}
    if command == "fix-task-validation-groups":
        if len(sys.argv) < 3:
            raise ValueError(get_new_command_usage("fix-task-validation-groups"))
        task_id = int(sys.argv[2])
        limit = int(_arg(3, "10"))
        groups = get_fix_task_validation_groups(task_id, limit=limit)
        return {"task_id": task_id, "count": len(groups), "groups": groups}
    if command == "fix-task-promote":
        if len(sys.argv) < 3:
            raise ValueError(get_new_command_usage("fix-task-promote"))
        task_id = int(sys.argv[2])
        raw_status = _arg(3, "patched")
        target_status = raw_status if raw_status in {"patched", "closed"} else "patched"
        note_start = 4 if raw_status in {"patched", "closed"} else 3
        note = " ".join(sys.argv[note_start:]).strip()
        return promote_fix_task_from_validation(task_id, target_status=target_status, note=note)
    if command == "fix-task-patched":
        if len(sys.argv) < 3:
            raise ValueError(get_new_command_usage("fix-task-patched"))
        task_id = int(sys.argv[2])
        note = " ".join(sys.argv[3:]).strip()
        return {"updated": update_fix_task_status(task_id, "patched", note=note), "task_id": task_id, "status": "patched"}
    if command == "fix-task-ack":
        if len(sys.argv) < 3:
            raise ValueError(get_new_command_usage("fix-task-ack"))
        task_id = int(sys.argv[2])
        owner = _arg(3, "")
        note = " ".join(sys.argv[4:]).strip()
        return {
            "updated": update_fix_task_status(task_id, "acknowledged", note=note, owner=owner or None),
            "task_id": task_id,
            "status": "acknowledged",
            "owner": owner,
        }
    if command == "fix-task-close":
        if len(sys.argv) < 3:
            raise ValueError(get_new_command_usage("fix-task-close"))
        task_id = int(sys.argv[2])
        note = " ".join(sys.argv[3:]).strip()
        return {"updated": update_fix_task_status(task_id, "closed", note=note), "task_id": task_id, "status": "closed"}
    if command == "fix-task-reopen":
        if len(sys.argv) < 3:
            raise ValueError(get_new_command_usage("fix-task-reopen"))
        task_id = int(sys.argv[2])
        note = " ".join(sys.argv[3:]).strip()
        return {"updated": update_fix_task_status(task_id, "open", note=note), "task_id": task_id, "status": "open"}
    if command == "fix-task-note":
        if len(sys.argv) < 4:
            raise ValueError(get_new_command_usage("fix-task-note"))
        task_id = int(sys.argv[2])
        note = " ".join(sys.argv[3:]).strip()
        return {"updated": add_fix_task_note(task_id, note), "task_id": task_id}
    if command == "backfill-packets":
        raw_limit = _flag_value("--limit", "0")
        snapshot_type = _flag_value("--type", "")
        return backfill_packets(
            limit=max(0, int(raw_limit or 0)),
            snapshot_type=snapshot_type,
            dry_run=not _has_flag("--apply"),
            force=_has_flag("--force"),
        )
    if command == "packet-maintain":
        shared_limit = int(_flag_value("--limit", "200") or 200)
        daily_limit = int(_flag_value("--daily-limit", str(shared_limit)) or shared_limit)
        intraday_limit = int(_flag_value("--intraday-limit", str(shared_limit)) or shared_limit)
        output_path = _flag_value("--out", "")
        return run_packet_maintenance(
            daily_limit=daily_limit,
            intraday_limit=intraday_limit,
            dry_run=_has_flag("--dry-run"),
            force=_has_flag("--force"),
            write_snapshot=not _has_flag("--no-write"),
            output_path=output_path,
        )
    if command == "daily-maintain":
        limit = int(_flag_value("--limit", "200") or 200)
        snapshot_path = _flag_value("--snapshot", "") or "/root/.openclaw/workspace/investor/docs/packet_maintenance_latest.json"
        handoff_path = _flag_value("--handoff", "") or "/root/.openclaw/workspace/investor/HANDOFF.md"
        return run_daily_maintenance(
            limit=limit,
            dry_run=_has_flag("--dry-run"),
            force=_has_flag("--force"),
            no_write=_has_flag("--no-write"),
            skip_runtime_check=_has_flag("--skip-runtime-check"),
            snapshot_path=snapshot_path,
            handoff_path=handoff_path,
        )
    if command == "smoke-check":
        return run_smoke_checks(stop_on_fail=_has_flag("--strict"))
    if command == "feishu-query":
        query = " ".join(sys.argv[2:]).strip()
        if not query:
            raise ValueError(get_new_command_usage("feishu-query"))
        return {
            "query": query,
            "reply": handle_feishu_query(query),
            "channel": "feishu-plugin",
        }
    if command == "feishu-bridge":
        query = _flag_value("--query", "").strip()
        if query:
            return build_bridge_response({"query": query})
        raw = _read_stdin_text()
        if not raw:
            raise ValueError(get_new_command_usage("feishu-bridge"))
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                payload = {"query": str(payload)}
        except Exception:
            payload = {"query": raw}
        return build_bridge_response(payload)
    if command == "handoff-sync":
        snapshot_path = _flag_value("--snapshot", "")
        handoff_path = _flag_value("--handoff", "")
        return sync_handoff_snapshot(
            snapshot_path=snapshot_path or "/root/.openclaw/workspace/investor/docs/packet_maintenance_latest.json",
            handoff_path=handoff_path or "/root/.openclaw/workspace/investor/HANDOFF.md",
        )
    if command == "scheduled-briefing":
        slot = _arg(2, "")
        if not slot:
            raise ValueError(get_new_command_usage("scheduled-briefing"))
        date = _arg(3, "")
        return run_scheduled_briefing(slot=slot, date_text=date)
    if command == "help":
        target = _arg(2, "")
        if target and is_new_command(target):
            return get_new_command_help(target)
        lines = ["new-cli commands:"]
        for name, spec in NEW_COMMAND_SPECS.items():
            lines.append(f"- {name}: {spec['description']}")
        lines.append("usage: python3 main.py help <command>")
        return "\n".join(lines)
    raise ValueError(f"unsupported command: {command}")


def print_result(result):
    if result is None:
        return
    if isinstance(result, str):
        print(result)
        return
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

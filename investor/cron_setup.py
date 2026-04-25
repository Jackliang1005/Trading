#!/usr/bin/env python3
"""
OpenClaw Investor - OpenClaw Cron 任务生成器
生成可添加到 OpenClaw jobs.json 的 cron 任务配置
"""

import json
import uuid
from datetime import datetime

INVESTOR_DIR = "/root/.openclaw/workspace/investor"


def generate_cron_jobs():
    """生成所有 cron 任务的配置"""

    jobs = [
        # ────────── 每日数据采集 (07:30 AM) ──────────
        {
            "id": str(uuid.uuid4()),
            "agentId": "main",
            "name": "investor-daily-collect",
            "description": "每日07:30自动采集市场数据（行情、资金流向、新闻），存入知识库",
            "enabled": True,
            "createdAtMs": int(datetime.now().timestamp() * 1000),
            "updatedAtMs": int(datetime.now().timestamp() * 1000),
            "schedule": {
                "kind": "cron",
                "expr": "30 7 * * 1-5",
                "tz": "Asia/Shanghai",
            },
            "sessionTarget": "isolated",
            "wakeMode": "now",
            "payload": {
                "kind": "agentTurn",
                "message": f"""【投资助手-数据采集】任务时间：每个交易日 07:30

请运行以下命令采集今日市场数据：

```
cd {INVESTOR_DIR} && python3 main.py collect
```

将采集结果概括为一段简短摘要（3-5行），包含主要指数表现和关键新闻。
如果采集失败，报告错误信息。

⚠️ 无论成功失败，都必须回复内容（不要回复 HEARTBEAT_OK）。""",
                "model": "deepseek/deepseek-chat",
                "timeoutSeconds": 180,
            },
            "delivery": {
                "skipIf": "HEARTBEAT_OK",
                "mode": "announce",
                "channel": "feishu",
                "to": "ou_f7d5ef82efd4396dea7a604691c56f75",
            },
        },

        # ────────── 每日反思回测 (20:30 PM) ──────────
        {
            "id": str(uuid.uuid4()),
            "agentId": "main",
            "name": "investor-daily-reflect",
            "description": "每日20:30自动回测昨日预测，计算准确率并生成反思报告",
            "enabled": True,
            "createdAtMs": int(datetime.now().timestamp() * 1000),
            "updatedAtMs": int(datetime.now().timestamp() * 1000),
            "schedule": {
                "kind": "cron",
                "expr": "30 20 * * 1-5",
                "tz": "Asia/Shanghai",
            },
            "sessionTarget": "isolated",
            "wakeMode": "now",
            "payload": {
                "kind": "agentTurn",
                "message": f"""【投资助手-每日反思】任务时间：每个交易日 20:30

请运行以下命令执行每日反思任务：

```
cd {INVESTOR_DIR} && python3 main.py reflect
```

根据输出生成反思报告，格式：
📊 **每日反思** [日期]
- 回测了X条预测
- 正确X条，胜率X%
- 主要发现/问题
- 改进建议

如果今天是周日，还会运行周度归因分析。
如果今天是月初，还会运行月度策略审计。

⚠️ 必须回复完整报告内容。""",
                "model": "deepseek/deepseek-chat",
                "timeoutSeconds": 240,
            },
            "delivery": {
                "skipIf": "HEARTBEAT_OK",
                "mode": "announce",
                "channel": "feishu",
                "to": "ou_f7d5ef82efd4396dea7a604691c56f75",
            },
        },

        # ────────── 每周进化 (周日 21:00) ──────────
        {
            "id": str(uuid.uuid4()),
            "agentId": "main",
            "name": "investor-weekly-evolve",
            "description": "每周日21:00自动调整策略权重、更新规则库、管理案例库",
            "enabled": True,
            "createdAtMs": int(datetime.now().timestamp() * 1000),
            "updatedAtMs": int(datetime.now().timestamp() * 1000),
            "schedule": {
                "kind": "cron",
                "expr": "0 21 * * 0",
                "tz": "Asia/Shanghai",
            },
            "sessionTarget": "isolated",
            "wakeMode": "now",
            "payload": {
                "kind": "agentTurn",
                "message": f"""【投资助手-每周进化】任务时间：每周日 21:00

请运行以下命令执行进化任务：

```
cd {INVESTOR_DIR} && python3 main.py evolve
```

根据输出生成进化报告，格式：
🧬 **每周进化报告** [日期]

⚖️ 策略权重调整：
- [各策略权重变化]

📏 规则更新：
- 新增X条规则
- 禁用X条低效规则

📝 案例库更新：
- 新增X个案例
- 移除X个旧案例

📊 系统 Prompt 已更新

然后运行 `cd {INVESTOR_DIR} && python3 main.py dashboard` 获取状态看板，附在报告末尾。

⚠️ 必须回复完整报告内容。""",
                "model": "deepseek/deepseek-chat",
                "timeoutSeconds": 300,
            },
            "delivery": {
                "skipIf": "HEARTBEAT_OK",
                "mode": "announce",
                "channel": "feishu",
                "to": "ou_f7d5ef82efd4396dea7a604691c56f75",
            },
        },

        # ────────── 月度审计 (每月1日 22:00) ──────────
        {
            "id": str(uuid.uuid4()),
            "agentId": "main",
            "name": "investor-monthly-audit",
            "description": "每月1日22:00月度策略全面审计，深度调整权重和规则",
            "enabled": True,
            "createdAtMs": int(datetime.now().timestamp() * 1000),
            "updatedAtMs": int(datetime.now().timestamp() * 1000),
            "schedule": {
                "kind": "cron",
                "expr": "0 22 1 * *",
                "tz": "Asia/Shanghai",
            },
            "sessionTarget": "isolated",
            "wakeMode": "now",
            "payload": {
                "kind": "agentTurn",
                "message": f"""【投资助手-月度审计】任务时间：每月1日 22:00

请依次运行以下命令：

1. 月度审计：
```
cd {INVESTOR_DIR} && python3 main.py audit
```

2. 状态看板：
```
cd {INVESTOR_DIR} && python3 main.py dashboard
```

3. 当前 Prompt：
```
cd {INVESTOR_DIR} && python3 main.py prompt
```

根据所有输出生成完整的月度审计报告，包含：
📋 **月度审计报告** [月份]
- 本月整体表现（总预测数、正确数、胜率）
- 各策略表现对比
- 权重调整详情
- 规则变更记录
- 下月策略方向建议

⚠️ 必须回复完整报告内容。""",
                "model": "deepseek/deepseek-chat",
                "timeoutSeconds": 420,
            },
            "delivery": {
                "skipIf": "HEARTBEAT_OK",
                "mode": "announce",
                "channel": "feishu",
                "to": "ou_f7d5ef82efd4396dea7a604691c56f75",
            },
        },

        # ────────── 东莞 09:45 策略日志巡检 ──────────
        {
            "id": str(uuid.uuid4()),
            "agentId": "main",
            "name": "investor-dongguan-0945-briefing",
            "description": "每个交易日09:45获取东莞日志，汇报NH/MIX策略买入与监控股票",
            "enabled": True,
            "createdAtMs": int(datetime.now().timestamp() * 1000),
            "updatedAtMs": int(datetime.now().timestamp() * 1000),
            "schedule": {
                "kind": "cron",
                "expr": "45 9 * * 1-5",
                "tz": "Asia/Shanghai",
            },
            "sessionTarget": "isolated",
            "wakeMode": "now",
            "payload": {
                "kind": "agentTurn",
                "message": f"""【交易监控-东莞09:45】请运行：

```
cd {INVESTOR_DIR} && python3 main.py scheduled-briefing 0945
```

直接原样转发输出内容到飞书（不要改写）。""",
                "model": "deepseek/deepseek-chat",
                "timeoutSeconds": 120,
            },
            "delivery": {
                "skipIf": "HEARTBEAT_OK",
                "mode": "announce",
                "channel": "feishu",
                "to": "ou_f7d5ef82efd4396dea7a604691c56f75",
            },
        },

        # ────────── 国金 ETF 13:20 交易简报 ──────────
        {
            "id": str(uuid.uuid4()),
            "agentId": "main",
            "name": "investor-guojin-etf-1320-briefing",
            "description": "每个交易日13:20获取国金ETF交易情况",
            "enabled": True,
            "createdAtMs": int(datetime.now().timestamp() * 1000),
            "updatedAtMs": int(datetime.now().timestamp() * 1000),
            "schedule": {
                "kind": "cron",
                "expr": "20 13 * * 1-5",
                "tz": "Asia/Shanghai",
            },
            "sessionTarget": "isolated",
            "wakeMode": "now",
            "payload": {
                "kind": "agentTurn",
                "message": f"""【交易监控-国金ETF13:20】请运行：

```
cd {INVESTOR_DIR} && python3 main.py scheduled-briefing 1320
```

直接原样转发输出内容到飞书（不要改写）。""",
                "model": "deepseek/deepseek-chat",
                "timeoutSeconds": 120,
            },
            "delivery": {
                "skipIf": "HEARTBEAT_OK",
                "mode": "announce",
                "channel": "feishu",
                "to": "ou_f7d5ef82efd4396dea7a604691c56f75",
            },
        },

        # ────────── 国金 ETF 14:20 交易简报 ──────────
        {
            "id": str(uuid.uuid4()),
            "agentId": "main",
            "name": "investor-guojin-etf-1420-briefing",
            "description": "每个交易日14:20获取国金ETF交易情况",
            "enabled": True,
            "createdAtMs": int(datetime.now().timestamp() * 1000),
            "updatedAtMs": int(datetime.now().timestamp() * 1000),
            "schedule": {
                "kind": "cron",
                "expr": "20 14 * * 1-5",
                "tz": "Asia/Shanghai",
            },
            "sessionTarget": "isolated",
            "wakeMode": "now",
            "payload": {
                "kind": "agentTurn",
                "message": f"""【交易监控-国金ETF14:20】请运行：

```
cd {INVESTOR_DIR} && python3 main.py scheduled-briefing 1420
```

直接原样转发输出内容到飞书（不要改写）。""",
                "model": "deepseek/deepseek-chat",
                "timeoutSeconds": 120,
            },
            "delivery": {
                "skipIf": "HEARTBEAT_OK",
                "mode": "announce",
                "channel": "feishu",
                "to": "ou_f7d5ef82efd4396dea7a604691c56f75",
            },
        },
    ]

    return jobs


def merge_into_jobs_json(jobs_json_path: str = "/root/.openclaw/cron/jobs.json"):
    """将新任务合并到现有 jobs.json"""
    # 读取现有任务
    with open(jobs_json_path, "r") as f:
        existing = json.load(f)

    existing_names = {j["name"] for j in existing.get("jobs", [])}

    # 生成新任务
    new_jobs = generate_cron_jobs()

    # 添加不存在的任务
    added = 0
    for job in new_jobs:
        if job["name"] not in existing_names:
            existing["jobs"].append(job)
            added += 1
            print(f"  ✅ 添加任务: {job['name']} ({job['description'][:40]})")
        else:
            print(f"  ℹ️ 任务已存在: {job['name']}")

    if added > 0:
        # 备份
        import shutil
        backup_path = f"{jobs_json_path}.bak.investor"
        shutil.copy2(jobs_json_path, backup_path)
        print(f"  💾 备份: {backup_path}")

        # 写入
        with open(jobs_json_path, "w") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 已添加 {added} 个 cron 任务")
    else:
        print(f"\nℹ️ 无新任务需要添加")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "merge":
        merge_into_jobs_json()
    else:
        jobs = generate_cron_jobs()
        print(json.dumps(jobs, ensure_ascii=False, indent=2))

# Feishu Plugin Interface

更新时间：2026-04-25 (v3 — Webhook 直连方案，已验证通过)

## 架构

```
飞书事件订阅
    ↓ HTTP POST
:8788/feishu/trading
    ↓
feishu_webhook_server.py  （统一 webhook）
    ├── /持仓 /预测 /风险 等 → investor query service → openclaw message send → 飞书回复
    └── T 开头              → TradingCommandService（兼容原有交易指令）
```

**关键设计决策：** 不走 openclaw AI agent，webhook 直接处理并回复。原因：AI agent 在飞书上下文中只做文本回复，不会执行 bash 命令，skill 指令方案在此架构下不可行。

## 部署

```bash
systemctl status feishu-webhook   # 查看状态
systemctl restart feishu-webhook  # 重启
journalctl -u feishu-webhook -f   # 实时日志
```

- 端口: `8788`
- 路径: `/feishu/trading`（兼容原 trading webhook 的飞书事件订阅 URL）
- 开机自启: `enabled` (systemd)
- 日志: `/root/.openclaw/workspace/investor/logs/feishu_webhook.log`
- 系统服务: `/etc/systemd/system/feishu-webhook.service`

## 支持的命令

### 投资查询

| 命令 | 示例 | 数据来源 |
|------|------|---------|
| `/持仓` | `/持仓` `国金持仓` | qmt2http 实时 |
| `/账户` | `/账户` | qmt2http 实时 |
| `/成交` | `/成交` | qmt2http 实时 |
| `/委托` | `/委托` | qmt2http 实时 |
| `/预测` | `/预测` `/胜率` | 本地 SQLite |
| `/风险` | `/风险` `仓位集中度` | 本地 SQLite |
| `/策略` | `/策略` `策略权重` | 本地 SQLite |
| `/复盘` | `/复盘` | 本地文件 |
| `/帮助` | `/帮助` | — |

### 交易指令（兼容）

| 指令前缀 | 说明 |
|---------|------|
| `T 状态` | 查看交易计划 |
| `T 开启 300475` | 激活做T |
| `T 设置 300475 ...` | 设置做T参数 |

## 意图路由规则

```
消息内容
  ├── /持仓 /预测 /风险 /策略 /复盘 等 → investor (handle_feishu_query)
  ├── T 开头                             → trading (TradingCommandService)
  └── 其他                               → ignored (openclaw AI agent 处理)
```

## 回复机制

Webhook 通过 `openclaw message send --channel feishu --target <user_id>` 发送回复，与 trading 系统通知走同一通道。

## 注意事项

- 需要 qmt2http 在线且 `QMT2HTTP_API_TOKEN` 可用才能查询实盘数据
- 分析类查询（预测/风险/策略）不需要 token，纯本地数据
- 东莞账户若返回 `403`，表示服务端策略限制交易读口
- Webhook 包含签名校验和事件去重（与飞书安全规范一致）

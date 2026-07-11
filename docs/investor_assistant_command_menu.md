OpenClaw A股个人投资助理

【飞书/自然语言常用问法】
- /状态: 查看投资助理服务、定时器、最新报告和阻塞项 | python3 main.py assistant-status
- /健康: 检查 qmt2http、账户读口、日志与服务健康 | python3 main.py runtime-check
- /策略状态 国金 / /策略停止 国金 确认: 查看或控制 qmt2http 策略服务；启停需带确认 | python3 main.py strategy-control <status|start|stop|restart> <guojin|dongguan> [--confirm]
- /持仓 或 国金今天持仓: 查看国金/东莞账户、持仓、委托、成交 | python3 main.py today-account
- /早报: 盘前汇总全球事件、风险、长线组合和阻塞项 | python3 main.py morning-brief --save
- /监控: 查看候选、买入、账户、告警和长线摘要 | python3 main.py today-summary --text
- /风险: 查看持仓集中度、现金比例、浮盈亏和快照新鲜度 | python3 main.py risk-report --save
- /事件 或 今天有什么事件: 查看财经事件、主题、产业链和相关 A股 | python3 main.py event-today
- /全球: 扫描全球突发财经新闻并映射到 A股主题 | python3 main.py global-event-brief
- /影响: 按优先级输出全球突发新闻、持仓命中、A股传导和观察动作 | python3 main.py global-impact-brief
- /关注: 把最近事件主题映射成 A股关注清单 | python3 main.py watchlist-report
- /长线: 查看长线组合 NAV、现金、持仓和计划 | python3 main.py longterm-summary
- /主题 华为: 把主题映射到产业链和 A股标的 | python3 main.py theme-map 华为
- /收盘: 收盘后汇总事件、风险、长线组合和阻塞项 | python3 main.py closing-brief --save
- /复盘: 收盘后生成预测/交易反思 | python3 main.py reflect
- /审计: 查看投资助理能力审计与阻塞项 | python3 main.py capability-audit
- /周报: 生成一周事件、预测、长线组合和阻塞项报告 | python3 main.py weekly-report --save

【服务器 CLI 快捷命令】
- /root/.openclaw/workspace/scripts/investor_assistant_healthcheck.sh: 生成一键健康巡检报告 | /root/.openclaw/workspace/scripts/investor_assistant_healthcheck.sh
- /root/.openclaw/workspace/scripts/investor_assistant_audit.py: 生成投资助理能力覆盖审计 | /root/.openclaw/workspace/scripts/investor_assistant_audit.py
- /root/.openclaw/workspace/scripts/investor_health_alert.py --dry-run: Preview lightweight health alert probe | /root/.openclaw/workspace/scripts/investor_health_alert.py --dry-run
- /root/.openclaw/workspace/scripts/qmt2http_remote_recovery.py --server guojin --timeout 6: Read-only Guojin qmt2http recovery probe | /root/.openclaw/workspace/scripts/qmt2http_remote_recovery.py --server guojin --timeout 6
- systemctl list-timers --all | grep -Ei 'investor|trading|qmttrader-v2': 查看投资助理定时任务 | systemctl list-timers --all | grep -Ei 'investor|trading|qmttrader-v2'
- systemctl status investor-event-watch feishu-webhook trading-intraday: 查看三类常驻服务 | systemctl status investor-event-watch feishu-webhook trading-intraday
- cd /root/.openclaw/workspace/investor && python3 main.py smoke-check: 执行 investor 主线 smoke 检查 | cd /root/.openclaw/workspace/investor && python3 main.py smoke-check

【自动化时间表】
- 07:30: 采集每日市场与账户数据 | investor-collect.timer
- 08:55: 交易日盘前投资助理早报与飞书推送 | investor-morning-brief.timer
- 09:30: 生成每日预测 | investor-predict.timer
- 09:00-15:45: 交易时段 qmt2http、日志与服务健康告警 | investor-health-alert.timer
- 10:35/14:35: 盘中持仓风险报告与飞书推送 | investor-risk-report.timer
- 24/7 every 15m: 全球突发财经新闻扫描与飞书推送 | investor-global-event-scan.timer
- 09:35: 长线早盘复核 | trading-morning.timer
- 09:45: 东莞策略简报 | investor-briefing-0945.timer
- 13:20: 国金 ETF 午盘简报 | investor-briefing-1320.timer
- 14:20: 国金 ETF 尾盘简报 | investor-briefing-1420.timer
- 15:35: 长线收盘决策 | trading-evening.timer
- 16:05: 收盘后快速复盘简报与飞书推送 | investor-closing-brief.timer
- 18:05: 更新 qmttrader_v2 热点概念库 | qmttrader-v2-concepts.timer
- 18:30: packet 与 handoff 维护 | investor-daily-maintain.timer
- 20:30: 每日反思复盘 | investor-reflect.timer
- 21:00: 每日能力审计与阻塞项推送 | investor-capability-audit.timer
- Fri 20:45: 周度投资助理报告与飞书推送 | investor-weekly-report.timer

【当前外部依赖提醒】
- 国金 qmt2http: 当前健康/持仓读口超时或断连 | 下一步: 在国金 Windows 生产机排查 miniQMT 与 qmt2http
- qmttrader_v2 日志: OpenClaw 已切到 /api/qmttrader_v2/status 与 /logs | 下一步: 以 qmt2http 返回为生产状态依据
- 概念库: 已由 qmttrader-v2-concepts.timer 每个交易日晚间更新 /root/qmttrader_v2/concept_db/concepts.db | 下一步: 关注数据源失败告警

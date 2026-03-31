import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from .concept_agent import get_weekly_hot_concepts
from .execution import TradingExecutionBook
from .notifier import send_feishu
from .paths import BASE_DIR, DAILY_PLAN_PATH, DEFAULT_CONFIG, FOCUS_LIST_PATH, HOT_CONCEPTS_CACHE_PATH, NEWS_SNAPSHOTS_CACHE_PATH, UNIVERSE_STATE_PATH
from .qmt2http_client import Qmt2HttpClient
from .recorder import TradingRecorder
from .review_engine import ReviewEngine
from .storage import atomic_write_json, load_json


DEFAULT_TARGET = "ou_f7d5ef82efd4396dea7a604691c56f75"


class TradingCommandService:
    def __init__(self, base_dir: Path = BASE_DIR):
        self.base_dir = Path(base_dir)
        self.config_path = self.base_dir / DEFAULT_CONFIG.name
        self.daily_plan_path = self.base_dir / DAILY_PLAN_PATH.name

    def load_base_config(self) -> Dict:
        return load_json(self.config_path, {"stocks": []})

    def load_daily_plan(self) -> Dict:
        today = datetime.now().strftime("%Y-%m-%d")
        data = load_json(self.daily_plan_path, {"date": today, "updated_at": "", "source": "feishu-webhook", "stocks": []})
        if data.get("date") != today:
            return {"date": today, "updated_at": "", "source": "feishu-webhook", "stocks": []}
        return data

    def save_daily_plan(self, plan: Dict) -> None:
        plan["date"] = datetime.now().strftime("%Y-%m-%d")
        plan["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        atomic_write_json(self.daily_plan_path, plan)

    def get_stock_map(self) -> Dict[str, Dict]:
        return {str(item["code"]): deepcopy(item) for item in self.load_base_config().get("stocks", [])}

    def get_recorder(self):
        return TradingRecorder(base_dir=self.base_dir)

    def get_execution_book(self):
        return TradingExecutionBook(base_dir=self.base_dir)

    def get_review_engine(self):
        return ReviewEngine(base_dir=self.base_dir)

    def get_qmt2http_config(self) -> Dict:
        config = self.load_base_config()
        return config.get("qmt2http", {}) if isinstance(config, dict) else {}

    def load_focus_list(self) -> Dict:
        return load_json(FOCUS_LIST_PATH, {"updated_at": "", "focus": [], "watch": [], "avoid": []})

    def load_universe_state(self) -> Dict:
        return load_json(UNIVERSE_STATE_PATH, {"updated_at": "", "stocks": []})

    def get_qmt2http_client(self) -> Qmt2HttpClient:
        config = self.get_qmt2http_config()
        if config and not bool(config.get("enabled", True)):
            raise RuntimeError("qmt2http 交易通道已关闭，请在配置里打开 qmt2http.enabled")
        return Qmt2HttpClient(config)

    def apply_today_overrides(self, stock: Dict, plan: Dict) -> Dict:
        result = deepcopy(stock)
        override = {str(item["code"]): item for item in plan.get("stocks", [])}.get(str(stock["code"]))
        if override:
            result.update(override)
        return result

    def find_or_create_override(self, plan: Dict, stock_code: str, base_stock: Dict) -> Dict:
        for item in plan["stocks"]:
            if str(item["code"]) == stock_code:
                return item
        item = {"code": stock_code, "name": base_stock.get("name", stock_code)}
        plan["stocks"].append(item)
        return item

    @staticmethod
    def parse_range(text: str, label: str) -> Optional[Tuple[float, float]]:
        match = re.search(rf"{label}\s*([0-9]+(?:\.[0-9]+)?)\s*[-~到]\s*([0-9]+(?:\.[0-9]+)?)", text)
        return (float(match.group(1)), float(match.group(2))) if match else None

    @staticmethod
    def parse_float_field(text: str, label: str) -> Optional[float]:
        match = re.search(rf"{label}\s*([0-9]+(?:\.[0-9]+)?)", text)
        return float(match.group(1)) if match else None

    @staticmethod
    def parse_int_field(text: str, label: str) -> Optional[int]:
        match = re.search(rf"{label}\s*([0-9]+)", text)
        return int(match.group(1)) if match else None

    @staticmethod
    def parse_mode_price(text: str) -> Optional[float]:
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)", text)
        return float(match.group(1)) if match else None

    @staticmethod
    def clear_risk_overrides(override: Dict) -> None:
        for key in (
            "preopen_risk_mode",
            "avoid_reverse_t",
            "abandon_buy_below",
            "risk_notes",
            "buy_blocked",
            "buy_block_reason",
        ):
            override.pop(key, None)

    @staticmethod
    def format_risk_status(stock: Dict) -> str:
        return (
            f" 模式{stock.get('preopen_risk_mode', 'normal') or 'normal'}"
            f" 禁逆T{'是' if stock.get('avoid_reverse_t') else '否'}"
            f" 放弃低吸<={stock.get('abandon_buy_below', '-')}"
        )

    @staticmethod
    def format_command_help() -> str:
        lines = [
            "做T命令列表",
            "当前推荐命令：",
            "T 状态",
            "T 仓位",
            "T 初始持仓",
            "T 观察池",
            "T 今日计划",
            "T 热门题材",
            "T 选股日报",
            "T 信息状态",
            "T 通道状态",
            "T 指令簿",
            "T 成交日报",
            "T 决策日报",
            "T 模式复盘",
            "T 更新持仓 300475 200 158.6426 200 143.96 158.643",
            "T 重建持仓",
            "T 下达 买 300475 155.2 100 原因",
            "T 执行 任务ID",
            "T 撤单 任务ID",
            "T 直买 300475 155.2 100 原因",
            "T 直卖 300475 158.6 100 原因",
            "T 接单 任务ID",
            "T 撤销 任务ID",
            "兼容旧命令：",
            "T 开启 300475",
            "T 关闭 300475",
            "T 设置 300475 买154.8-155.6 卖158.2-160 止损153 数量100",
            "T 重置",
            "说明：以上旧命令仍可用，但当前主流程优先使用 持仓库 -> 观察池 -> 今日计划。",
            "帮助：",
            "T 命令",
            "T 帮助",
        ]
        return "\n".join(lines)

    @staticmethod
    def format_execution_summary(summary: Dict) -> str:
        lines = [
            f"执行回执 {summary['date']}",
            f"总记录{summary.get('total_trades', 0)} 笔",
            f"买入{summary.get('buy_count', 0)}",
            f"卖出{summary.get('sell_count', 0)}",
            f"放弃{summary.get('skip_count', 0)}",
            f"已实现盈亏{summary.get('total_profit', 0):.2f}元",
        ]
        for code, stats in summary.get("stock_stats", {}).items():
            lines.append(f"{code} {stats.get('name', '')} 记录{stats.get('trades', 0)} 卖出盈亏{stats.get('profit', 0):.2f}元")
        return "\n".join(lines)

    @staticmethod
    def format_stock_status(stock: Dict) -> str:
        buy_range = stock.get("buy_range", ["-", "-"])
        sell_range = stock.get("sell_range", ["-", "-"])
        return (
            f"{stock['code']} {stock.get('name', '')} "
            f"[{'开' if stock.get('enabled', False) else '关'}] "
            f"买{buy_range[0]}-{buy_range[1]} 卖{sell_range[0]}-{sell_range[1]} "
            f"止损{stock.get('stop_loss', '-')} 数量{stock.get('per_trade_shares', '-')}"
            f"{TradingCommandService.format_risk_status(stock)}"
        )

    @staticmethod
    def format_decision_review(summary: Dict) -> str:
        lines = [
            f"决策日报 {summary.get('date', '')}",
            f"总决策{summary.get('decision_count', 0)}",
            f"自动候选{summary.get('auto_trade_candidates', 0)}",
            f"禁买命中{summary.get('buy_blocked_candidates', 0)}",
        ]
        action_counts = summary.get("action_counts", {})
        lines.append(
            f"买入{action_counts.get('buy', 0)} 卖出{action_counts.get('sell', 0)} 观望{action_counts.get('wait', 0)}"
        )
        for name, count in summary.get("playbook_counts", {}).items():
            lines.append(f"{name} {count}")
        return "\n".join(lines)

    @staticmethod
    def format_mode_review(summary: Dict) -> str:
        lines = [
            f"模式复盘 {summary.get('date', '')}",
            f"总决策{summary.get('decision_count', 0)}",
        ]
        for mode, stats in summary.get("mode_stats", {}).items():
            lines.append(
                f"{mode} 决策{stats.get('count', 0)} 买{stats.get('buy', 0)} "
                f"卖{stats.get('sell', 0)} 观望{stats.get('wait', 0)} 自动候选{stats.get('auto', 0)} "
                f"已实现盈亏{stats.get('realized_profit', 0.0):.2f} 胜{stats.get('wins', 0)} 负{stats.get('losses', 0)}"
            )
        return "\n".join(lines)

    @staticmethod
    def format_focus_list(data: Dict) -> str:
        lines = [f"做T观察池 {data.get('updated_at', '')}"]
        for item in data.get("focus", []):
            lines.append(
                f"重点 {item.get('code')} {item.get('name')} 分数{item.get('score')} "
                f"模式{item.get('am_mode', '-')}/{item.get('pm_mode', '-')}"
                f" 买预算{item.get('buy_budget_amount', 0):.0f} 卖预算{item.get('sell_budget_amount', 0):.0f}"
                f" 仓位{item.get('suggested_t_ratio', 0):.0%} 买股数{item.get('suggested_buy_shares', 0)} 卖股数{item.get('suggested_sell_shares', 0)} "
                f"{item.get('reason', '')}"
            )
            if item.get("explanation"):
                lines.append(f"说明 {item.get('explanation')}")
            if item.get("buy_blocked"):
                lines.append(f"外盘风控 {item.get('buy_block_reason', '')}")
        for item in data.get("watch", [])[:5]:
            lines.append(
                f"观察 {item.get('code')} {item.get('name')} 分数{item.get('score')} "
                f"模式{item.get('am_mode', '-')}/{item.get('pm_mode', '-')}"
                f" 买预算{item.get('buy_budget_amount', 0):.0f} 卖预算{item.get('sell_budget_amount', 0):.0f}"
                f" 仓位{item.get('suggested_t_ratio', 0):.0%} 买股数{item.get('suggested_buy_shares', 0)} 卖股数{item.get('suggested_sell_shares', 0)} "
                f"{item.get('reason', '')}"
            )
            if item.get("explanation"):
                lines.append(f"说明 {item.get('explanation')}")
            if item.get("buy_blocked"):
                lines.append(f"外盘风控 {item.get('buy_block_reason', '')}")
        for item in data.get("avoid", [])[:3]:
            lines.append(
                f"回避 {item.get('code')} {item.get('name')} 分数{item.get('score')} "
                f"{item.get('reason', '')}"
            )
            if item.get("explanation"):
                lines.append(f"说明 {item.get('explanation')}")
            if item.get("buy_blocked"):
                lines.append(f"外盘风控 {item.get('buy_block_reason', '')}")
        return "\n".join(lines)

    @staticmethod
    def format_selection_plan(plan: Dict) -> str:
        lines = [f"当日做T计划 {plan.get('date', '')} 来源{plan.get('source', '')}"]
        for item in plan.get("stocks", []):
            lines.append(
                f"{item.get('code')} {item.get('name')} "
                f"[{'开' if item.get('enabled') else '关'}] "
                f"action={item.get('selection_action', '-')}"
                f" mode={item.get('selection_am_mode', '-')}/{item.get('selection_pm_mode', '-')}"
                f" score={item.get('selection_score', '-')}"
                f" buy_budget={item.get('selection_buy_budget_amount', 0):.0f}"
                f" sell_budget={item.get('selection_sell_budget_amount', 0):.0f}"
                f" ratio={item.get('selection_ratio', 0):.0%}"
                f" buy_shares={item.get('selection_buy_shares', 0)}"
                f" sell_shares={item.get('selection_sell_shares', 0)}"
                f" default_shares={item.get('per_trade_shares', 0)}"
            )
            if item.get("selection_buy_blocked") or item.get("buy_blocked"):
                lines.append(f"外盘风控 {item.get('selection_buy_block_reason') or item.get('buy_block_reason', '')}")
        return "\n".join(lines)

    @staticmethod
    def format_portfolio_status(state: Dict) -> str:
        lines = [f"当日仓位状态 {state['date']}"]
        for code in sorted(state.get("stocks", {})):
            item = state["stocks"][code]
            lines.append(
                f"{code} {item.get('name', '')} "
                f"底仓{item.get('carry_position', 0)} 当前{item.get('current_position', 0)} "
                f"今卖{item.get('intraday_sell', 0)} 今买{item.get('intraday_buy', 0)} "
                f"可卖{item.get('available_to_sell', 0)} 可回补{item.get('available_to_buy_back', 0)} "
                f"成本{item.get('cost_price', 0)}"
            )
        return "\n".join(lines)

    @staticmethod
    def format_initial_portfolio_db(data: Dict) -> str:
        lines = [f"初始持仓数据库 {data.get('updated_at', '')} 来源{data.get('source', '')}"]
        overview = data.get("account_overview", {})
        if overview:
            lines.append(
                f"可用{overview.get('available_cash', 0):.2f} "
                f"可取{overview.get('withdrawable_cash', 0):.2f} "
                f"冻结{overview.get('frozen_cash', 0):.2f} "
                f"市值{overview.get('stock_market_value', 0):.2f} "
                f"总资产{overview.get('total_assets', 0):.2f}"
            )
        for item in data.get("stocks", []):
            lines.append(
                f"{item.get('code')} {item.get('name')} 底仓{item.get('base_position', 0)} "
                f"可用{item.get('available_position', item.get('base_position', 0))} "
                f"成本{item.get('cost_price', 0)} 策略{item.get('strategy', '')}"
            )
        return "\n".join(lines)

    @staticmethod
    def format_hot_concepts(data: Dict[str, list]) -> str:
        lines = ["近7天热门题材"]
        for date_str, items in data.items():
            if not items:
                lines.append(f"{date_str} 无数据")
                continue
            top_names = [f"{str(item.get('name', '')).strip()}#{idx}" for idx, item in enumerate(items[:5], start=1) if str(item.get("name", "")).strip()]
            lines.append(f"{date_str} {' / '.join(top_names)}")
        return "\n".join(lines)

    @staticmethod
    def format_selection_review(data: Dict) -> str:
        lines = [f"选股日报 {data.get('updated_at', '')}"]
        if data.get("latest_hot_concepts_date"):
            top_names = [f"{str(item.get('name', '')).strip()}#{item.get('rank', '-')}" for item in data.get("latest_hot_concepts", [])[:5] if str(item.get("name", "")).strip()]
            lines.append(f"最新题材日 {data.get('latest_hot_concepts_date')} {' / '.join(top_names)}")
        for item in data.get("focus", [])[:5]:
            lines.append(
                f"重点 {item.get('code')} {item.get('name')} 分数{item.get('score')} "
                f"模式{item.get('am_mode', '-')}/{item.get('pm_mode', '-')}"
            )
            if item.get("explanation"):
                lines.append(f"说明 {item.get('explanation')}")
            if item.get("buy_blocked"):
                lines.append(f"外盘风控 {item.get('buy_block_reason', '')}")
        for item in data.get("peer_risk", [])[:5]:
            lines.append(
                f"外盘风控 {item.get('code')} {item.get('name')} "
                f"{item.get('buy_block_reason', '')}"
            )
        if data.get("avoid"):
            blocked = [item for item in data.get("avoid", []) if item.get("buy_blocked")]
            if blocked:
                lines.append(f"禁买合计 {len(blocked)}")
        return "\n".join(lines)

    @staticmethod
    def format_info_status(news_cache: Dict, concept_cache: Dict) -> str:
        lines = ["信息状态"]
        lines.append(
            f"新闻缓存 更新时间{news_cache.get('updated_at', '-') or '-'} "
            f"时段{news_cache.get('refresh_slot', '-') or '-'}"
        )
        refresh_slots = concept_cache.get("refresh_slots", {}) if isinstance(concept_cache.get("refresh_slots"), dict) else {}
        latest_day = next(iter(refresh_slots.keys()), "")
        latest_slot = refresh_slots.get(latest_day, "-") if latest_day else "-"
        lines.append(
            f"题材缓存 更新时间{concept_cache.get('updated_at', '-') or '-'} "
            f"最新题材日{latest_day or '-'} 时段{latest_slot}"
        )
        lines.append("新闻刷新规则 盘前1次 + 开盘/上午中段/上午尾段/下午前段/下午尾段 各1次，同一时段读缓存")
        lines.append("题材刷新规则 盘前不抢刷今日题材，09:30后按 open/mid_am/late_am/early_pm/late_pm/post_close 分段刷新")
        return "\n".join(lines)

    @staticmethod
    def format_qmt_status(status: Dict) -> str:
        lines = ["QMT通道状态"]
        lines.append(f"可达 {'是' if status.get('reachable') else '否'}")
        lines.append(f"行情 {'正常' if status.get('market_connected') else '异常'}")
        lines.append(f"交易 {'正常' if status.get('trade_connected') else '异常'}")
        lines.append(f"状态 {status.get('status', '-')}")
        if status.get("reason"):
            lines.append(f"原因 {status.get('reason')}")
        return "\n".join(lines)

    @staticmethod
    def format_command_brief(command: Dict) -> str:
        return (
            f"{command['id']} {command.get('stock_code', '')} {command.get('stock_name', '')} "
            f"{'买入' if command.get('action') == 'buy' else '卖出'} "
            f"{command.get('quantity', 0)}股 @{command.get('price', 0):.2f} "
            f"[{command.get('status', '')}] {command.get('reason', '')}".strip()
        )

    def execute_command_via_qmt2http(self, command_id: str) -> str:
        book = self.get_execution_book()
        command = book.find_command(command_id)
        if not command:
            return f"未找到指令 {command_id}"
        if command.get("status") in ("executed", "cancelled"):
            return f"指令已是 {command.get('status')} 状态：\n{self.format_command_brief(command)}"
        client = self.get_qmt2http_client()
        try:
            response = client.place_order(
                stock_code=command["stock_code"],
                side=command["action"],
                price=float(command["price"]),
                amount=int(command["quantity"]),
                strategy_name="trading_do_t",
                order_remark=f"command:{command['id']}",
            )
        except Exception as exc:
            book.mark_command_failed(command["id"], "qmt2http", str(exc))
            return f"qmt2http 下单失败：{exc}\n{self.format_command_brief(book.find_command(command['id']) or command)}"

        data = response.get("data", {}) if isinstance(response, dict) else {}
        broker_order_id = (
            data.get("order_id")
            or data.get("entrust_no")
            or data.get("order_no")
            or data.get("合同编号")
            or ""
        )
        submitted = book.mark_command_submitted(
            command["id"],
            "qmt2http",
            broker_order_id=str(broker_order_id),
            broker_response=response,
            note="已通过 qmt2http 提交",
        ) or command
        return (
            f"已通过 qmt2http 提交：\n{self.format_command_brief(submitted)}\n"
            f"券商单号：{submitted.get('broker_order_id') or '-'}"
        )

    def cancel_command_via_qmt2http(self, command_id: str) -> str:
        book = self.get_execution_book()
        command = book.find_command(command_id)
        if not command:
            return f"未找到指令 {command_id}"
        broker_order_id = str(command.get("broker_order_id") or "").strip()
        if not broker_order_id:
            return f"指令没有可用的券商单号，无法通过 qmt2http 撤单：\n{self.format_command_brief(command)}"
        client = self.get_qmt2http_client()
        try:
            response = client.cancel_order(broker_order_id)
        except Exception as exc:
            return f"qmt2http 撤单失败：{exc}\n{self.format_command_brief(command)}"
        cancelled = book.update_command_status(
            command["id"],
            "cancelled",
            f"已通过 qmt2http 撤单，券商单号 {broker_order_id}",
        ) or command
        return (
            f"已通过 qmt2http 撤单：\n{self.format_command_brief(cancelled)}\n"
            f"券商单号：{broker_order_id}\n"
            f"返回：{response}"
        )

    def create_and_execute_command(
        self,
        stock_code: str,
        action: str,
        price: float,
        quantity: int,
        reason: str,
        source: str,
    ) -> str:
        validation_error = self.validate_trade_params(stock_code, action, price, quantity)
        if validation_error:
            return validation_error
        command = self.get_execution_book().create_command(
            stock_code=stock_code,
            action=action,
            price=price,
            quantity=quantity,
            reason=reason,
            source=source,
        )
        return self.execute_command_via_qmt2http(command["id"])

    def validate_trade_params(self, stock_code: str, action: str, price: float, quantity: int) -> str:
        code = str(stock_code or "").strip()
        if not code:
            return "证券代码不能为空"
        try:
            price_val = float(price)
        except (TypeError, ValueError):
            return "价格格式错误"
        try:
            quantity_val = int(quantity)
        except (TypeError, ValueError):
            return "数量格式错误"
        if price_val <= 0:
            return "价格必须大于 0"
        if quantity_val <= 0:
            return "数量必须大于 0"
        if quantity_val % 100 != 0:
            return "A股数量必须是 100 股整数倍"
        if quantity_val > 1000000:
            return "单笔数量过大，请拆单后再下达"
        if action == "sell":
            portfolio = self.get_execution_book().load_portfolio_state()
            available = int(portfolio.get("stocks", {}).get(code, {}).get("available_to_sell", 0) or 0)
            if quantity_val > available:
                return f"可卖数量不足：当前可卖 {available} 股"
        return ""

    def handle_command(self, message: str) -> str:
        text = re.sub(r"\s+", " ", message.strip())
        if not text.startswith("T"):
            raise ValueError("不是交易指令")
        stock_map = self.get_stock_map()
        plan = self.load_daily_plan()
        if text in ("T 命令", "T 帮助", "T help", "T commands-help"):
            return self.format_command_help()
        if text in ("T 状态", "T 列表", "T status", "T list"):
            lines = [f"今日交易计划 {plan['date']}"]
            for _, base in stock_map.items():
                merged = self.apply_today_overrides(base, plan)
                action = merged.get("selection_action", "-")
                lines.append(f"{self.format_stock_status(merged)} action={action}")
            return "\n".join(lines) if len(lines) > 1 else f"今日交易计划 {plan['date']}\n当前没有配置监控票"
        if text in ("T 仓位", "T 持仓", "T portfolio"):
            return self.format_portfolio_status(self.get_execution_book().load_portfolio_state())
        if text in ("T 初始持仓", "T 持仓库", "T initial-portfolio"):
            return self.format_initial_portfolio_db(self.get_execution_book().ensure_initial_portfolio_db())
        if text in ("T 热门题材", "T 热题材", "T hot-concepts"):
            return self.format_hot_concepts(get_weekly_hot_concepts())
        if text in ("T 选股日报", "T 观察池日报", "T selection-review"):
            today = datetime.now().strftime("%Y-%m-%d")
            data = load_json(self.base_dir / "trading_data" / f"selection_review_{today}.json", {"updated_at": "", "focus": []})
            return self.format_selection_review(data)
        if text in ("T 信息状态", "T 数据状态", "T info-status"):
            news_cache = load_json(NEWS_SNAPSHOTS_CACHE_PATH, {"updated_at": "", "refresh_slot": ""})
            concept_cache = load_json(HOT_CONCEPTS_CACHE_PATH, {"updated_at": "", "refresh_slots": {}})
            return self.format_info_status(news_cache, concept_cache)
        if text in ("T 通道状态", "T qmt状态", "T qmt-status"):
            return self.format_qmt_status(self.get_qmt2http_client().probe_status())
        matched = re.match(
            r"T\s*(更新持仓|更新底仓)\s*(\d{6})\s*([0-9]+)\s*([0-9]+(?:\.[0-9]+)?)(?:\s*([0-9]+))?(?:\s*([0-9]+(?:\.[0-9]+)?))?(?:\s*([0-9]+(?:\.[0-9]+)?))?",
            text,
            re.IGNORECASE,
        )
        if matched:
            code = matched.group(2)
            base_position = int(matched.group(3))
            cost_price = float(matched.group(4))
            available_position = int(matched.group(5)) if matched.group(5) else base_position
            last_price = float(matched.group(6)) if matched.group(6) else None
            break_even_price = float(matched.group(7)) if matched.group(7) else None
            entry = self.get_execution_book().update_initial_portfolio_entry(
                stock_code=code,
                base_position=base_position,
                cost_price=cost_price,
                available_position=available_position,
                last_price=last_price,
                break_even_price=break_even_price,
            )
            return (
                f"已更新初始持仓：{entry.get('code')} {entry.get('name')} "
                f"底仓{entry.get('base_position')} 可用{entry.get('available_position')} "
                f"成本{entry.get('cost_price')}"
            )
        if text in ("T 重建持仓", "T 重建仓位", "T rebuild-portfolio"):
            state = self.get_execution_book().rebuild_portfolio_state_from_initial()
            return self.format_portfolio_status(state)
        if text in ("T 指令簿", "T 指令", "T 任务", "T commands"):
            commands = self.get_execution_book().list_commands(["pending", "acknowledged", "executed"])
            if not commands:
                return f"指令簿 {plan['date']}\n当前没有记录。"
            lines = [f"指令簿 {plan['date']}"]
            for item in commands[:10]:
                lines.append(self.format_command_brief(item))
            return "\n".join(lines)
        if text in ("T 成交日报", "T 执行日报", "T 回执", "T report"):
            return self.format_execution_summary(self.get_recorder().get_daily_summary())
        if text in ("T 决策日报", "T 复盘", "T decision-review"):
            return self.format_decision_review(self.get_review_engine().build_daily_review())
        if text in ("T 模式复盘", "T mode-review"):
            return self.format_mode_review(self.get_review_engine().build_mode_review())
        if text in ("T 观察池", "T 重点票", "T focus"):
            return self.format_focus_list(self.load_focus_list())
        if text in ("T 今日计划", "T 选股计划", "T selection-plan"):
            return self.format_selection_plan(self.load_daily_plan())
        if text in ("T 重置", "T reset"):
            plan["stocks"] = []
            self.save_daily_plan(plan)
            return f"已重置 {plan['date']} 的临时交易计划，监控恢复为基础配置。"
        matched = re.match(r"T\s*(开启|关闭|on|off)\s*(\d{6})", text, re.IGNORECASE)
        if matched:
            code = matched.group(2)
            base = stock_map.get(code)
            if not base:
                return f"未找到股票代码 {code}"
            override = self.find_or_create_override(plan, code, base)
            override["enabled"] = matched.group(1).lower() in ("开启", "on")
            self.save_daily_plan(plan)
            return f"已{'开启' if override['enabled'] else '关闭'} {code} {base.get('name', '')} 的今日监控。"
        matched = re.match(r"T\s*(下达|指令|派单)\s*(买|卖|buy|sell)\s*(\d{6})\s*([0-9]+(?:\.[0-9]+)?)\s*([0-9]+)(?:\s*(.*))?", text, re.IGNORECASE)
        if matched:
            action = matched.group(2).lower()
            code = matched.group(3)
            base = stock_map.get(code)
            if not base:
                return f"未找到股票代码 {code}"
            validation_error = self.validate_trade_params(
                code,
                "buy" if action in ("买", "buy") else "sell",
                float(matched.group(4)),
                int(matched.group(5)),
            )
            if validation_error:
                return validation_error
            command = self.get_execution_book().create_command(
                stock_code=code,
                action="buy" if action in ("买", "buy") else "sell",
                price=float(matched.group(4)),
                quantity=int(matched.group(5)),
                reason=(matched.group(6) or "").strip(),
                source="feishu-manual",
            )
            return f"已下达执行指令：\n{self.format_command_brief(command)}"
        matched = re.match(r"T\s*(执行|直连执行)\s*([0-9a-fA-F]{8})", text, re.IGNORECASE)
        if matched:
            return self.execute_command_via_qmt2http(matched.group(2).lower())
        matched = re.match(r"T\s*(撤单|直连撤单)\s*([0-9a-fA-F]{8})", text, re.IGNORECASE)
        if matched:
            return self.cancel_command_via_qmt2http(matched.group(2).lower())
        matched = re.match(r"T\s*(直买|直卖|直接买|直接卖)\s*(\d{6})\s*([0-9]+(?:\.[0-9]+)?)\s*([0-9]+)(?:\s*(.*))?", text, re.IGNORECASE)
        if matched:
            keyword = matched.group(1)
            action = "buy" if "买" in keyword else "sell"
            return self.create_and_execute_command(
                stock_code=matched.group(2),
                action=action,
                price=float(matched.group(3)),
                quantity=int(matched.group(4)),
                reason=(matched.group(5) or "").strip() or "飞书直接交易",
                source="feishu-direct",
            )
        matched = re.match(r"T\s*(接单|确认)\s*([0-9a-fA-F]{8})", text, re.IGNORECASE)
        if matched:
            command = self.get_execution_book().update_command_status(matched.group(2).lower(), "acknowledged")
            if not command:
                return f"未找到指令 {matched.group(2)}"
            return f"已确认接单：\n{self.format_command_brief(command)}"
        matched = re.match(r"T\s*(撤销|取消)\s*([0-9a-fA-F]{8})(?:\s*(.*))?", text, re.IGNORECASE)
        if matched:
            command = self.get_execution_book().update_command_status(matched.group(2).lower(), "cancelled", (matched.group(3) or "").strip())
            if not command:
                return f"未找到指令 {matched.group(2)}"
            return f"已撤销指令：\n{self.format_command_brief(command)}"
        matched = re.match(r"T\s*(设置|set)\s*(\d{6})\s*(.*)", text, re.IGNORECASE)
        if matched:
            code = matched.group(2)
            body = matched.group(3)
            base = stock_map.get(code)
            if not base:
                return f"未找到股票代码 {code}"
            override = self.find_or_create_override(plan, code, base)
            override["enabled"] = True
            buy_range = self.parse_range(body, "买")
            sell_range = self.parse_range(body, "卖")
            stop_loss = self.parse_float_field(body, "止损")
            shares = self.parse_int_field(body, "数量")
            if not any([buy_range, sell_range, stop_loss is not None, shares is not None]):
                return "未识别到有效参数，示例：T 设置 300475 买154.8-155.6 卖158.2-160 止损153 数量100"
            if buy_range:
                override["buy_range"] = [buy_range[0], buy_range[1]]
            if sell_range:
                override["sell_range"] = [sell_range[0], sell_range[1]]
            if stop_loss is not None:
                override["stop_loss"] = stop_loss
            if shares is not None:
                override["per_trade_shares"] = shares
            self.save_daily_plan(plan)
            return f"已更新今日计划：\n{self.format_stock_status(self.apply_today_overrides(base, plan))}"
        matched = re.match(r"T\s*(防守|谨慎|解除防守|恢复|normal)\s*(\d{6})(?:\s*(.*))?", text, re.IGNORECASE)
        if matched:
            action = matched.group(1).lower()
            code = matched.group(2)
            body = matched.group(3) or ""
            base = stock_map.get(code)
            if not base:
                return f"未找到股票代码 {code}"
            override = self.find_or_create_override(plan, code, base)
            override["enabled"] = True
            if action == "防守":
                override["preopen_risk_mode"] = "defensive"
                override["avoid_reverse_t"] = True
                price = self.parse_mode_price(body)
                if price is not None:
                    override["abandon_buy_below"] = price
            elif action == "谨慎":
                override["preopen_risk_mode"] = "cautious"
                override["avoid_reverse_t"] = False
            else:
                self.clear_risk_overrides(override)
            self.save_daily_plan(plan)
            return f"已更新风控模式：\n{self.format_stock_status(self.apply_today_overrides(base, plan))}"
        matched = re.match(r"T\s*(放弃低吸)\s*(\d{6})\s*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
        if matched:
            code = matched.group(2)
            base = stock_map.get(code)
            if not base:
                return f"未找到股票代码 {code}"
            override = self.find_or_create_override(plan, code, base)
            override["enabled"] = True
            override["abandon_buy_below"] = float(matched.group(3))
            self.save_daily_plan(plan)
            return f"已更新：\n{self.format_stock_status(self.apply_today_overrides(base, plan))}"
        matched = re.match(r"T\s*(解除放弃)\s*(\d{6})", text, re.IGNORECASE)
        if matched:
            code = matched.group(2)
            base = stock_map.get(code)
            if not base:
                return f"未找到股票代码 {code}"
            override = self.find_or_create_override(plan, code, base)
            override.pop("abandon_buy_below", None)
            self.save_daily_plan(plan)
            return f"已更新：\n{self.format_stock_status(self.apply_today_overrides(base, plan))}"
        matched = re.match(r"T\s*(外盘风险|同行风险|禁买)\s*(\d{6})(?:\s*(.*))?", text, re.IGNORECASE)
        if matched:
            code = matched.group(2)
            reason = (matched.group(3) or "").strip() or "外盘同行走弱，禁止主动买入"
            base = stock_map.get(code)
            if not base:
                return f"未找到股票代码 {code}"
            override = self.find_or_create_override(plan, code, base)
            override["enabled"] = True
            override["buy_blocked"] = True
            override["buy_block_reason"] = reason
            override["avoid_reverse_t"] = True
            self.save_daily_plan(plan)
            return f"已设置外盘风险禁买：\n{self.format_stock_status(self.apply_today_overrides(base, plan))}"
        matched = re.match(r"T\s*(解除外盘风险|解除同行风险|解除禁买)\s*(\d{6})", text, re.IGNORECASE)
        if matched:
            code = matched.group(2)
            base = stock_map.get(code)
            if not base:
                return f"未找到股票代码 {code}"
            override = self.find_or_create_override(plan, code, base)
            override.pop("buy_blocked", None)
            override.pop("buy_block_reason", None)
            self.save_daily_plan(plan)
            return f"已解除外盘风险禁买：\n{self.format_stock_status(self.apply_today_overrides(base, plan))}"
        matched = re.match(r"T\s*(已买|买入成交)\s*(\d{6})\s*([0-9]+(?:\.[0-9]+)?)\s*([0-9]+)(?:\s*(.*))?", text, re.IGNORECASE)
        if matched:
            note = (matched.group(5) or "").strip() or "飞书回执买入"
            trade = self.get_recorder().record_trade(matched.group(2), "buy", float(matched.group(3)), int(matched.group(4)), note)
            portfolio = self.get_execution_book().record_execution(matched.group(2), "buy", float(matched.group(3)), int(matched.group(4)), note)
            summary = self.get_recorder().get_daily_summary()
            return (
                f"已记录买入成交：{trade['stock_name']} {trade['quantity']}股 @ {trade['price']:.2f}\n"
                f"仓位：当前{portfolio.get('current_position', 0)} 可卖{portfolio.get('available_to_sell', 0)} 可回补{portfolio.get('available_to_buy_back', 0)}\n"
                f"今日累计：买入{summary.get('buy_count', 0)} 卖出{summary.get('sell_count', 0)} 放弃{summary.get('skip_count', 0)} 已实现盈亏{summary.get('total_profit', 0):.2f}元"
            )
        matched = re.match(r"T\s*(已卖|卖出成交)\s*(\d{6})\s*([0-9]+(?:\.[0-9]+)?)\s*([0-9]+)(?:\s*(.*))?", text, re.IGNORECASE)
        if matched:
            note = (matched.group(5) or "").strip() or "飞书回执卖出"
            trade = self.get_recorder().record_trade(matched.group(2), "sell", float(matched.group(3)), int(matched.group(4)), note)
            portfolio = self.get_execution_book().record_execution(matched.group(2), "sell", float(matched.group(3)), int(matched.group(4)), note)
            summary = self.get_recorder().get_daily_summary()
            profit_note = f"，本笔盈亏 {trade.get('profit', 0):.2f}元" if trade.get("profit") else ""
            return (
                f"已记录卖出成交：{trade['stock_name']} {trade['quantity']}股 @ {trade['price']:.2f}{profit_note}\n"
                f"仓位：当前{portfolio.get('current_position', 0)} 可卖{portfolio.get('available_to_sell', 0)} 可回补{portfolio.get('available_to_buy_back', 0)}\n"
                f"今日累计：买入{summary.get('buy_count', 0)} 卖出{summary.get('sell_count', 0)} 放弃{summary.get('skip_count', 0)} 已实现盈亏{summary.get('total_profit', 0):.2f}元"
            )
        matched = re.match(r"T\s*(放弃)\s*(\d{6})(?:\s*(.*))?", text, re.IGNORECASE)
        if matched:
            event = self.get_recorder().record_skip(matched.group(2), reason=(matched.group(3) or "").strip() or "盘口不成立，放弃执行")
            summary = self.get_recorder().get_daily_summary()
            return f"已记录放弃执行：{event['stock_name']}，原因：{event['reason']}\n今日累计：买入{summary.get('buy_count', 0)} 卖出{summary.get('sell_count', 0)} 放弃{summary.get('skip_count', 0)} 已实现盈亏{summary.get('total_profit', 0):.2f}元"
        return "未识别指令。可用格式：T 状态 / T 仓位 / T 指令簿 / T 下达 卖 300475 158.6 100 / T 执行 1a2b3c4d / T 撤单 1a2b3c4d / T 直买 300475 155.2 100 / T 直卖 300475 158.6 100 / T 接单 1a2b3c4d / T 撤销 1a2b3c4d / T 成交日报 / T 开启 300475 / T 关闭 688316 / T 设置 300475 买154.8-155.6 卖158.2-160 止损153 数量100 / T 防守 300475 151.8 / T 放弃低吸 300475 151.8 / T 外盘风险 300475 美光海力士隔夜大跌 / T 解除外盘风险 300475 / T 已买 300475 155.2 100 / T 已卖 300475 158.6 100 / T 放弃 300475 / T 重置"

    @staticmethod
    def send_reply(target: str, text: str) -> bool:
        return send_feishu(target, text, silent=True)

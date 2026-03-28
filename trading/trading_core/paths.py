from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
STATE_PATH = BASE_DIR / "trading_data" / "monitor_state.json"
DEFAULT_CONFIG = BASE_DIR / "做T监控配置.json"
DAILY_PLAN_PATH = BASE_DIR / "今日交易计划.json"
PORTFOLIO_STATE_PATH = BASE_DIR / "trading_data" / "portfolio_state.json"
COMMAND_BOOK_PATH = BASE_DIR / "trading_data" / "command_book.json"

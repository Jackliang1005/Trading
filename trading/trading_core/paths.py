from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
STATE_PATH = BASE_DIR / "trading_data" / "monitor_state.json"
DEFAULT_CONFIG = BASE_DIR / "做T监控配置.json"
DAILY_PLAN_PATH = BASE_DIR / "今日交易计划.json"
PORTFOLIO_STATE_PATH = BASE_DIR / "trading_data" / "portfolio_state.json"
COMMAND_BOOK_PATH = BASE_DIR / "trading_data" / "command_book.json"
FOCUS_LIST_PATH = BASE_DIR / "trading_data" / "focus_list.json"
UNIVERSE_STATE_PATH = BASE_DIR / "trading_data" / "universe_state.json"
INITIAL_PORTFOLIO_DB_PATH = BASE_DIR / "初始持仓数据库.json"
LOCAL_SECRETS_PATH = BASE_DIR / "local_secrets.json"
HOT_CONCEPTS_CACHE_PATH = BASE_DIR / "trading_data" / "hot_concepts_cache.json"
NEWS_SNAPSHOTS_CACHE_PATH = BASE_DIR / "trading_data" / "news_snapshots_cache.json"

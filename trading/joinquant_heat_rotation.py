"""
Formal baseline: GEM-only small-cap offensive version.

Validated baseline parameters:
- base_stock_sum = 3
- rank_start = 10
- final_pool_size = 20
- revenue_yoy_threshold = 0.15
- market_cap_max = 50.0
- stop_loss_rate = 0.10
- stop_profit_rate = 1.20
"""

from jqdata import *
import pandas as pd
import numpy as np
import math
import pickle
import base64


def set_backtest():
    set_benchmark("399006.XSHE")
    set_option("avoid_future_data", True)
    set_option("use_real_price", True)
    log.set_level("order", "error")
    set_slippage(FixedSlippage(0.002), type="stock")
    set_order_cost(
        OrderCost(
            open_tax=0,
            close_tax=0.0005,
            open_commission=0.85 / 10000,
            close_commission=0.85 / 10000,
            close_today_commission=0,
            min_commission=5,
        ),
        type="stock",
    )


def set_params(context):
    # Core experiment knobs for the GEM-only version.
    g.base_stock_sum = 3
    g.rank_start = 10
    g.final_pool_size = 20
    g.max_per_stock = 200000

    g.stop_loss_rate = 0.10
    g.stop_profit_rate = 1.20
    g.cooling_period_days = 1
    g.cooling_period_stocks = {}
    g.yesterday_limit_up_stocks = []

    g.small_cap_divergence_config = ("399101.XSHE", 6)
    g.small_cap_divergence_history = []
    g.enable_macd_divergence_check = False

    g.pass_months = []
    g.enable_turnover_check = True
    g.enable_market_breadth_check = False
    g.defense_signal = None
    g.defense_cache = {}
    g.trading_signal = True

    g.enable_roe_filter = False
    g.enable_roa_filter = False
    g.enable_revenue_yoy_filter = True
    g.revenue_yoy_threshold = 0.15
    g.enable_pb_filter = False
    g.pb_threshold = 3.0
    g.enable_gross_profit_margin_filter = False
    g.gross_profit_margin_threshold = 40.0
    g.enable_netprofit_yoy_filter = False
    g.netprofit_yoy_threshold = -1.0
    g.enable_grossmargin_yoy_filter = False
    g.grossmargin_yoy_threshold = 0.50
    g.enable_price_filter = False
    g.max_price = 20.0

    # Default to GEM-only because it showed better excess return than 30+68.
    g.use_board_universe = True
    g.board_prefixes = ("30",)
    g.market_cap_min = 5.0
    g.market_cap_max = 50.0
    g.universe_limit = 1000
    g.min_listing_days = 375

    g.strategy_holdings = []
    g.stock_revenue_yoy = {}
    g.stock_gross_profit_margin = {}

    load_cache()


def initialize(context):
    set_backtest()
    set_params(context)
    log.set_level("order", "error")

    run_daily(check_divergence, "09:31")
    run_daily(small_cap_risk_control, "09:32")
    run_daily(small_cap_check_morning, "10:35")
    run_daily(small_cap_check_afternoon, "14:00")
    run_daily(small_cap_check_market_breadth, "14:50")
    run_weekly(small_cap_sell, 1, "09:42")
    run_weekly(small_cap_buy, 1, "09:45")


def is_index_below_ma(index_code="399303.XSHE", ma_period=20):
    return False


def small_cap_filter(context):
    pool_codes = _get_small_cap_pool(context)
    if not pool_codes:
        return []

    stocks = filter_basic_stock(context, pool_codes)
    stocks = [s for s in stocks if s not in g.cooling_period_stocks]
    if not stocks:
        return []

    stocks_df = _query_small_cap_candidates(stocks)
    if stocks_df.empty:
        return []

    stocks_df = stocks_df.sort_values("market_cap", ascending=True)
    stocks_df = stocks_df.drop_duplicates(subset=["code"]).head(g.final_pool_size)
    result = stocks_df["code"].tolist()
    del stocks_df

    if not result:
        return []

    if g.enable_price_filter:
        last_prices = history(1, "1d", "close", result, df=False)
        current_holdings = set(g.strategy_holdings)
        filtered = []
        for stock in result:
            if stock in current_holdings:
                filtered.append(stock)
                continue
            if stock in last_prices and last_prices[stock][0] <= g.max_price:
                filtered.append(stock)
        result = filtered
        del last_prices, current_holdings

    _cache_buy_metrics(result)
    result = _apply_grossmargin_yoy_filter(result, context)
    return result


def _get_small_cap_pool(context):
    if g.use_board_universe:
        all_stocks = get_all_securities(types=["stock"], date=context.previous_date)
        if all_stocks is None or all_stocks.empty:
            return []
        pool = []
        for code in all_stocks.index.tolist():
            raw = code.split(".")[0]
            if raw.startswith(g.board_prefixes):
                pool.append(code)
        return pool

    q_pool = query(valuation.code, valuation.market_cap).order_by(valuation.market_cap.asc()).limit(g.universe_limit)
    pool_df = get_fundamentals(q_pool)
    if pool_df is None or pool_df.empty:
        return []
    result = pool_df["code"].tolist()
    del pool_df
    return result


def _query_small_cap_candidates(stocks):
    filters = [
        valuation.code.in_(stocks),
        valuation.market_cap.between(g.market_cap_min, g.market_cap_max),
    ]

    if g.enable_roe_filter:
        filters.append(indicator.roe > 15)
    if g.enable_roa_filter:
        filters.append(indicator.roa > 10)
    if g.enable_pb_filter:
        filters.append(valuation.pb_ratio < g.pb_threshold)
    if g.enable_revenue_yoy_filter:
        filters.append(indicator.inc_revenue_year_on_year >= g.revenue_yoy_threshold * 100)
    if g.enable_gross_profit_margin_filter:
        filters.append(indicator.gross_profit_margin > g.gross_profit_margin_threshold)
    if g.enable_netprofit_yoy_filter:
        filters.append(indicator.inc_net_profit_year_on_year >= g.netprofit_yoy_threshold * 100)

    growth_board = [s for s in stocks if s.startswith(g.board_prefixes)]
    regular = [s for s in stocks if not s.startswith(g.board_prefixes)]

    frames = []
    if regular:
        frames.append(_query_by_revenue(filters, regular, 1e8))
    if growth_board:
        frames.append(_query_by_revenue(filters, growth_board, 3e5))

    if not frames:
        return pd.DataFrame(columns=["code", "market_cap"])

    result = pd.concat(frames, ignore_index=True)
    if result is None or result.empty:
        return pd.DataFrame(columns=["code", "market_cap"])
    return result[["code", "market_cap"]]


def _query_by_revenue(base_filters, candidate_codes, revenue_threshold):
    scoped_filters = list(base_filters)
    scoped_filters[0] = valuation.code.in_(candidate_codes)
    scoped_filters.append(income.operating_revenue > revenue_threshold)
    df = get_fundamentals(
        query(valuation.code, valuation.market_cap)
        .filter(*scoped_filters)
        .order_by(valuation.market_cap.asc())
    )
    if df is None or df.empty:
        return pd.DataFrame(columns=["code", "market_cap"])
    return df


def small_cap_sell(context):
    if not g.trading_signal:
        return

    total_value = context.portfolio.total_value
    max_stocks = max(g.base_stock_sum, int(math.ceil(total_value / float(g.max_per_stock))))
    all_candidates = small_cap_filter(context)
    current_holdings = [s for s in context.portfolio.positions if s in g.strategy_holdings]
    target_stocks = all_candidates[g.rank_start:g.rank_start + max_stocks]

    current_data = get_current_data()
    for stock in current_holdings:
        if stock in target_stocks or stock in g.yesterday_limit_up_stocks:
            continue
        cd = current_data[stock]
        if not cd.paused and cd.low_limit < cd.last_price < cd.high_limit:
            close_position(context, stock, "调仓")
    del current_data


def small_cap_buy(context):
    if not g.trading_signal:
        return

    total_value = context.portfolio.total_value
    max_stocks = max(g.base_stock_sum, int(math.ceil(total_value / float(g.max_per_stock))))
    current_holdings = set([s for s in context.portfolio.positions if s in g.strategy_holdings])
    available_slots = max_stocks - len(current_holdings)
    if available_slots <= 0:
        return

    candidates = small_cap_filter(context)
    target_slice = candidates[g.rank_start:g.rank_start + max_stocks]
    to_buy = [s for s in target_slice if s not in current_holdings][:available_slots]
    if not to_buy:
        return

    valid_holdings = [s for s in current_holdings if context.portfolio.positions[s].closeable_amount > 0]
    current_holdings_value = 0.0
    for stock in valid_holdings:
        current_holdings_value += context.portfolio.positions[stock].value
    available_cash = min(context.portfolio.available_cash, total_value - current_holdings_value)
    value_per_stock = min(available_cash / len(to_buy), float(g.max_per_stock))

    for stock in to_buy:
        open_position(context, stock, value_per_stock, "调仓" if current_holdings else "开仓")


def small_cap_risk_control(context):
    g.trading_signal = True
    g.yesterday_limit_up_stocks = []

    if is_index_below_ma(index_code="399303.XSHE"):
        print("风控触发|大盘跌破均线，小市值策略清仓停止交易")
        _close_all_tradeable(context, "大盘跌破均线")
        g.trading_signal = False
        return

    holdings = g.strategy_holdings[:]
    if holdings:
        df = get_price(
            holdings,
            end_date=context.previous_date,
            frequency="daily",
            fields=["close", "high_limit"],
            count=1,
            panel=False,
        )
        if df is not None and not df.empty:
            g.yesterday_limit_up_stocks = df[df["close"] >= df["high_limit"] * 0.997].code.drop_duplicates().tolist()
        del df

    symbol, recovery_days = g.small_cap_divergence_config
    if symbol and True in g.small_cap_divergence_history[-recovery_days:]:
        recent = g.small_cap_divergence_history[-recovery_days:]
        last_true_index = len(recent) - 1 - recent[::-1].index(True)
        days_remaining = recovery_days - (len(recent) - 1 - last_true_index)
        print("%s背离信号恢复中，小市值策略将在%d天后恢复持仓" % (symbol, days_remaining))
        _close_all_tradeable(context, "背离恢复")
        g.trading_signal = False
        return

    if context.current_dt.month in g.pass_months:
        print("空仓月份，小市值策略保持空仓")
        _close_all_tradeable(context, "空仓月份")
        g.trading_signal = False
        return

    if g.enable_market_breadth_check and (g.defense_signal is None or g.defense_signal):
        if g.defense_signal is None:
            print("首次运行，市场宽度信号未检测，小市值策略暂不交易")
        else:
            print("处于中小组防御状态，小市值策略保持空仓")
            _close_all_tradeable(context, "宽度防御")
        g.trading_signal = False
        return


def small_cap_check_morning(context):
    check_profit_loss(context)
    check_turnover(context)


def small_cap_check_afternoon(context):
    check_profit_loss(context)
    check_turnover(context)
    check_limit_up(context)


def small_cap_check_market_breadth(context):
    check_market_breadth(context)


def place_order(context, security, value):
    current_data = get_current_data()

    if value == 0 and security in context.portfolio.positions:
        if context.portfolio.positions[security].closeable_amount == 0:
            return False

    if value > 0:
        price = current_data[security].last_price
        min_shares = 200 if security[:2] == "68" else 100
        min_value = price * min_shares
        is_reduce = security in context.portfolio.positions and context.portfolio.positions[security].value > value
        if not is_reduce and (value < min_value or context.portfolio.available_cash < min_value or int(value / price) < min_shares):
            return False

    need_limit_order = security[:2] == "68" or security[:3] == "300"
    if need_limit_order:
        order_obj = order_target_value(security, value, style=LimitOrderStyle(current_data[security].last_price))
    else:
        order_obj = order_target_value(security, value)

    if order_obj and order_obj.filled > 0:
        if value > 0:
            if security not in g.strategy_holdings:
                g.strategy_holdings.append(security)
        elif security in g.strategy_holdings:
            g.strategy_holdings.remove(security)
        return True
    return False


def open_position(context, security, value, reason=""):
    if place_order(context, security, value):
        yoy = g.stock_revenue_yoy.get(security)
        gpm = g.stock_gross_profit_margin.get(security)
        yoy_str = "%.1f%%" % (yoy * 100) if yoy is not None else "N/A"
        gpm_str = "%.2f%%" % gpm if gpm is not None else "N/A"
        print("买入：%s(%s) - %s | 营收同比增长率:%s | 销售毛利率:%s" %
              (get_security_info(security).display_name, security, reason, yoy_str, gpm_str))
        return True
    return False


def close_position(context, security, reason=""):
    if security in g.strategy_holdings:
        if place_order(context, security, 0):
            print("卖出：%s(%s) - %s" % (get_security_info(security).display_name, security, reason))
            return True
    return False


def filter_basic_stock(context, stock_list):
    current_data = get_current_data()
    filtered = []
    for stock in stock_list:
        cd = current_data[stock]
        if cd.paused or cd.is_st or "\u9000" in cd.name:
            continue
        if (context.previous_date - get_security_info(stock).start_date).days < g.min_listing_days:
            continue
        if not (cd.low_limit < cd.last_price < cd.high_limit):
            continue
        raw = stock.split(".")[0]
        if not raw.startswith(g.board_prefixes):
            continue
        filtered.append(stock)
    del current_data
    return filtered


def check_divergence(context):
    if not g.enable_macd_divergence_check:
        return

    def calculate_macd(close, fast=12, slow=26, signal=9):
        close_array = np.asarray(close)

        def calculate_ema(array, span):
            alpha = 2.0 / (span + 1)
            ema = np.zeros_like(array)
            ema[span - 1] = np.mean(array[:span])
            for i in range(span, len(array)):
                ema[i] = alpha * array[i] + (1 - alpha) * ema[i - 1]
            return ema

        dif = calculate_ema(close_array, fast) - calculate_ema(close_array, slow)
        dea = calculate_ema(dif, signal)
        return dif, dea, (dif - dea) * 2

    def detect_divergence_for_date(symbol, end_date):
        if not symbol:
            return False

        rows = (12 + 26 + 9) * 5
        current_date = context.current_dt.date() if hasattr(context.current_dt, "date") else context.current_dt
        check_date = end_date.date() if hasattr(end_date, "date") else end_date
        if check_date == current_date:
            end_date = context.previous_date

        price_data = get_price(symbol, end_date=end_date, count=rows, frequency="daily", fields=["close"]).dropna()
        if len(price_data) < rows:
            del price_data
            return False

        close_prices = price_data["close"].values
        del price_data
        dif, dea, macd = calculate_macd(close_prices, 12, 26, 9)

        mask = (macd < 0) & (np.roll(macd, 1) >= 0)
        mask_indices = np.where(mask)[0]
        if len(mask_indices) < 2:
            return False

        key2_idx, key1_idx = mask_indices[-2], mask_indices[-1]
        price_cond = close_prices[key2_idx] < close_prices[key1_idx]
        dif_cond = dif[key2_idx] > dif[key1_idx] > 0
        macd_cond = macd[-2] > 0 > macd[-1]
        trend_cond = dif[-10:].mean() < dif[-20:-10].mean() if len(dif) > 20 else False
        return bool(price_cond and dif_cond and macd_cond and trend_cond)

    symbol, recovery_days = g.small_cap_divergence_config
    if not g.small_cap_divergence_history:
        try:
            trade_days = get_trade_days(end_date=context.previous_date, count=recovery_days)
            g.small_cap_divergence_history = [detect_divergence_for_date(symbol, date) for date in trade_days]
            del trade_days
        except Exception as e:
            log.warning("重构%s背离历史失败: %s" % (symbol, e))
            g.small_cap_divergence_history = []
    else:
        g.small_cap_divergence_history = g.small_cap_divergence_history[-(recovery_days + 10):]

    divergence_detected = detect_divergence_for_date(symbol, context.current_dt)
    g.small_cap_divergence_history.append(divergence_detected)
    g.small_cap_divergence_history = g.small_cap_divergence_history[-(recovery_days + 10):]

    if divergence_detected:
        print("检测到%s顶背离信号，小市值策略清仓" % symbol)
        _close_all_tradeable(context, "MACD顶背离")


def check_market_breadth(context):
    if not g.enable_market_breadth_check:
        return

    date_str = context.current_dt.date().strftime("%Y-%m-%d")
    if date_str in g.defense_cache:
        cached_signal = g.defense_cache[date_str]
        if cached_signal != g.defense_signal:
            log.info("检测到中小组防御信号，小市值策略清仓" if cached_signal else "中小组防御信号消失，小市值策略恢复持仓")
            g.defense_signal = cached_signal
        if g.defense_signal and g.strategy_holdings:
            _close_all_tradeable(context, "宽度防御")
        return

    if not g.defense_signal:
        is_high = _check_trend_quick(context)
        if not is_high:
            g.defense_signal = False
            _update_cache(date_str)
            return

    sorted_ma_data, up_ratio_avg = _get_market_breadth_optimized(context, ma_days=20)

    if g.defense_signal:
        g.defense_signal = "组20" in sorted_ma_data.index[:3]
        log.info("检测到中小组防御信号，小市值策略保持空仓" if g.defense_signal else "中小组防御信号消失，小市值策略恢复持仓")
    else:
        defense_in_top = "组20" in sorted_ma_data.index[:2]
        other_groups = [x for x in sorted_ma_data.index if x != "组20"]
        avg_score = sorted_ma_data[other_groups].mean() if other_groups else 100
        g.defense_signal = bool(defense_in_top and avg_score < 60 and up_ratio_avg < 0.5)
        if g.defense_signal:
            log.info("检测到中小组防御信号，小市值策略清仓")

    _update_cache(date_str)
    if g.defense_signal and g.strategy_holdings:
        _close_all_tradeable(context, "成交额宽度防御清仓")


def _check_trend_quick(context, index_symbol="399101.XSHE"):
    data = get_bars(
        index_symbol,
        end_dt=context.current_dt.replace(hour=14, minute=49),
        count=62,
        unit="1d",
        fields=["close", "high"],
        include_now=True,
        df=True,
    )
    if len(data) < 60:
        return False

    close_arr = data["close"].values
    high_arr = data["high"].values
    for offset in [-2, -1, 0]:
        idx = len(close_arr) if offset == 0 else offset
        window_high = high_arr[max(0, idx - 60):idx].max() if idx > 0 else high_arr[-60:].max()
        current_close = close_arr[idx - 1] if idx != 0 else close_arr[-1]
        if current_close >= window_high * 0.95:
            return True
    return False


def _get_market_breadth_optimized(context, ma_days=20):
    end_date = context.current_dt.replace(hour=14, minute=49)
    all_stocks = get_index_stocks("000852.XSHG", date=end_date)
    if not all_stocks:
        return pd.Series({"组20": 0}), 0.5

    data = get_bars(
        all_stocks,
        end_dt=end_date,
        count=ma_days + 3,
        unit="1d",
        fields=["date", "close", "money"],
        include_now=True,
        df=True,
    )
    if data.empty:
        return pd.Series({"组20": 0}), 0.5

    data_reset = data.reset_index()
    close_pivot = data_reset.pivot(index="level_1", columns="level_0", values="close")
    ma20 = close_pivot.rolling(window=ma_days).mean()
    above_ma = (close_pivot > ma20).iloc[-3:]

    money_pivot = data_reset.pivot(index="level_1", columns="level_0", values="money")
    avg_money = money_pivot.iloc[-20:].mean()
    try:
        money_groups = pd.qcut(avg_money, 20, labels=["组%d" % (i + 1) for i in range(20)], duplicates="drop")
    except Exception:
        return pd.Series({"组20": 0}), 0.5

    group_scores = {}
    for group_name in money_groups.unique():
        group_stocks = money_groups[money_groups == group_name].index.tolist()
        valid_stocks = [s for s in group_stocks if s in above_ma.columns]
        if valid_stocks:
            group_scores[group_name] = 100 * above_ma[valid_stocks].mean().mean()

    sorted_ma_data = pd.Series(group_scores).sort_values(ascending=False)
    pct_change = close_pivot.pct_change()
    up_ratio_avg = (pct_change.iloc[-3:] > 0).mean().mean()
    return sorted_ma_data, up_ratio_avg


def _update_cache(date_str):
    g.defense_cache[date_str] = g.defense_signal
    if len(g.defense_cache) > 90:
        all_dates = sorted(g.defense_cache.keys())
        for old_date in all_dates[:-90]:
            del g.defense_cache[old_date]
    save_cache()


def check_turnover(context):
    if not g.enable_turnover_check or context.current_dt.month in g.pass_months:
        return

    current_data = get_current_data()
    shrink, expand = 0.003, 0.1

    for stock in g.strategy_holdings[:]:
        if stock not in context.portfolio.positions:
            g.strategy_holdings.remove(stock)
            continue

        cd = current_data[stock]
        if cd.paused or cd.last_price >= cd.high_limit * 0.97 or context.portfolio.positions[stock].closeable_amount == 0:
            continue

        df_cap = get_valuation(stock, end_date=context.previous_date, fields=["circulating_cap"], count=1)
        circulating_cap = df_cap["circulating_cap"].iloc[0] if not df_cap.empty else 0
        del df_cap
        if circulating_cap == 0:
            continue

        df_vol = get_price(
            stock,
            start_date=context.current_dt.date(),
            end_date=context.current_dt,
            frequency="1m",
            fields=["volume"],
            skip_paused=False,
            fq="pre",
            panel=True,
            fill_paused=False,
        )
        rt = df_vol["volume"].sum() / (circulating_cap * 10000)
        del df_vol

        df_volume = get_price(stock, end_date=context.previous_date, frequency="daily", fields=["volume"], count=20)
        avg = (df_volume["volume"] / (circulating_cap * 10000)).mean()
        del df_volume

        if avg < shrink:
            close_position(context, stock, "缩量")
            g.cooling_period_stocks[stock] = 0
        elif rt > expand and rt / avg > 2:
            close_position(context, stock, "放量")
            g.cooling_period_stocks[stock] = 0

    del current_data


def check_profit_loss(context):
    g.cooling_period_stocks = {s: d + 1 for s, d in g.cooling_period_stocks.items() if d + 1 <= g.cooling_period_days}

    current_data = get_current_data()
    for stock in g.strategy_holdings[:]:
        if stock not in context.portfolio.positions:
            g.strategy_holdings.remove(stock)
            continue

        cd = current_data[stock]
        if cd.paused:
            continue

        position = context.portfolio.positions[stock]
        if position.avg_cost == 0:
            continue

        profit_ratio = (cd.last_price - position.avg_cost) / position.avg_cost
        if profit_ratio >= g.stop_profit_rate:
            if close_position(context, stock, "止盈(%.2f%%)" % (profit_ratio * 100)):
                g.cooling_period_stocks[stock] = 0
        elif profit_ratio <= -g.stop_loss_rate:
            if close_position(context, stock, "止损(%.2f%%)" % (profit_ratio * 100)):
                g.cooling_period_stocks[stock] = 0
    del current_data


def check_limit_up(context):
    g.yesterday_limit_up_stocks = []
    holdings = g.strategy_holdings[:]
    if not holdings:
        return

    df = get_price(holdings, end_date=context.previous_date, frequency="daily", fields=["close", "high_limit"], count=1, panel=False)
    if df is None or df.empty:
        return

    g.yesterday_limit_up_stocks = df[df["close"] >= df["high_limit"] * 0.997].code.drop_duplicates().tolist()
    if g.yesterday_limit_up_stocks:
        current_data = get_current_data()
        for stock in g.yesterday_limit_up_stocks:
            if current_data[stock].last_price < current_data[stock].high_limit * 0.99:
                if close_position(context, stock, "涨停开板"):
                    g.cooling_period_stocks[stock] = 0
        del current_data


def _cache_buy_metrics(result):
    try:
        if not result:
            return
        df_metrics = get_fundamentals(
            query(
                valuation.code,
                indicator.inc_revenue_year_on_year,
                indicator.gross_profit_margin,
            ).filter(valuation.code.in_(result))
        ).set_index("code")

        for stock in result:
            yoy_val = df_metrics.at[stock, "inc_revenue_year_on_year"] if stock in df_metrics.index else None
            gpm_val = df_metrics.at[stock, "gross_profit_margin"] if stock in df_metrics.index else None
            g.stock_revenue_yoy[stock] = yoy_val / 100 if yoy_val is not None and not pd.isna(yoy_val) else None
            g.stock_gross_profit_margin[stock] = gpm_val if gpm_val is not None and not pd.isna(gpm_val) else None
        del df_metrics
    except Exception as e:
        log.warning("买入日志指标计算失败：%s" % e)


def _apply_grossmargin_yoy_filter(result, context):
    if not g.enable_grossmargin_yoy_filter or not result:
        return result

    from datetime import timedelta

    cur_date = context.current_dt.date() if hasattr(context.current_dt, "date") else context.current_dt
    last_year_date = cur_date - timedelta(days=365)
    try:
        df_cur = get_fundamentals(
            query(valuation.code, indicator.gross_profit_margin).filter(valuation.code.in_(result))
        ).set_index("code")
        df_prev = get_fundamentals(
            query(valuation.code, indicator.gross_profit_margin).filter(valuation.code.in_(result)),
            date=last_year_date
        ).set_index("code")
    except Exception as e:
        log.warning("同比指标筛选失败，已跳过：%s" % e)
        return result

    filtered = []
    for stock in result:
        cur_val = df_cur.at[stock, "gross_profit_margin"] if stock in df_cur.index else None
        prev_val = df_prev.at[stock, "gross_profit_margin"] if stock in df_prev.index else None
        if cur_val is None or prev_val is None or pd.isna(cur_val) or pd.isna(prev_val):
            continue
        if prev_val == 0:
            if cur_val > 0:
                filtered.append(stock)
            continue
        growth_rate = (cur_val - prev_val) / abs(prev_val)
        if growth_rate >= g.grossmargin_yoy_threshold:
            filtered.append(stock)
    return filtered


def _close_all_tradeable(context, reason):
    current_data = get_current_data()
    for stock in g.strategy_holdings[:]:
        cd = current_data[stock]
        if stock in g.yesterday_limit_up_stocks:
            continue
        if not cd.paused and cd.low_limit < cd.last_price < cd.high_limit:
            close_position(context, stock, reason)
    del current_data


def load_cache():
    if not g.defense_cache:
        try:
            content = read_file("market_breadth_defense.pkl")
            g.defense_cache.update(pickle.loads(base64.b64decode(content)))
            log.info("加载成交量宽度防御缓存: %d 条记录" % len(g.defense_cache))
        except Exception:
            log.info("成交量宽度防御缓存文件不存在，创建新缓存")


def save_cache():
    try:
        write_file("market_breadth_defense.pkl", base64.b64encode(pickle.dumps(g.defense_cache)).decode("utf-8"))
    except Exception:
        pass

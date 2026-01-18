import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from binance.client import Client
from multiprocessing import Pool, cpu_count
from itertools import product
import time

# === ВСТАВЬ СВОИ КЛЮЧИ BINANCE ===
api_key = ''
api_secret = ''
client = Client(api_key, api_secret)

# === ФУНКЦИЯ ЗАГРУЗКИ ДАННЫХ ===
def fetch_klines_paged(symbol, interval, limit=5000):
    klines = []
    end_time = None

    while len(klines) < limit:
        batch = client.get_klines(
            symbol=symbol,
            interval=interval,
            limit=min(1000, limit - len(klines)),
            endTime=end_time
        )
        if not batch:
            break
        klines = batch + klines
        end_time = batch[0][0] - 1

    df = pd.DataFrame(klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tbbav", "tbqav", "ignore"
    ])
    df = df[["open_time", "open", "high", "low", "close"]].astype(float)
    df["open_time"] = df["open_time"].astype(int)
    return df.reset_index(drop=True)


# === ОСНОВНАЯ ФУНКЦИЯ БЭКТЕСТА ===
def run_backtest(params):
    symbol, tp_pct, sl_pct, be_trig_pct, cci_length, adx_long_min = params

    df = fetch_klines_paged(symbol, Client.KLINE_INTERVAL_1HOUR, limit=5000)
    if df.empty or len(df) < 1000:
        return None

    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')

    # === Параметры ===
    conversionPeriods = 10
    basePeriods = 26
    laggingSpan2Periods = 24
    displacement = 26
    ma_length = 200
    cci_long_thr = 98.0
    cci_short_thr = -98.0
    use_ichi_cloud = True
    use_ichi_lines = True
    use_ma_dir = True
    wait_flag_reset_till_flat = True
    use_adx_filter = True
    adx_len = 10
    adx_long_max = 55.0
    adx_short_min = 14.0
    adx_short_max = 41.0
    use_rsi_filter = True
    rsi_len = 18
    rsi_long_min = 55.0
    rsi_long_max = 69.0
    rsi_short_min = 30.0
    rsi_short_max = 50.0
    use_natr_filter = True
    natr_len = 13
    natr_long_min = 0.85
    natr_long_max = 3.5
    natr_short_min = 0.6
    natr_short_max = 3.3
    use_bbw_filter = True
    bbw_len = 29
    bbw_mult = 2.1
    bbw_min_trend = 2.0
    be_enabled = True
    be_offset_pct = 0.0
    commission_rate = 0.00035
    initial_capital = 10000.0
    risk_per_trade = 0.01  # 1% от текущего капитала на каждую сделку

    # === Индикаторы (без изменений) ===
    hlc3 = (df['high'] + df['low'] + df['close']) / 3

    def donchian_high(s, length): return s.rolling(length).max()
    def donchian_low(s, length): return s.rolling(length).min()

    conversionLine = (donchian_high(df['high'], conversionPeriods) + donchian_low(df['low'], conversionPeriods)) / 2
    baseLine = (donchian_high(df['high'], basePeriods) + donchian_low(df['low'], basePeriods)) / 2
    leadLine1 = (conversionLine + baseLine) / 2
    leadLine2 = (donchian_high(df['high'], laggingSpan2Periods) + donchian_low(df['low'], laggingSpan2Periods)) / 2

    spanA = leadLine1.shift(displacement - 1)
    spanB = leadLine2.shift(displacement - 1)
    df['kumoTop'] = np.maximum(spanA, spanB)
    df['kumoBottom'] = np.minimum(spanA, spanB)
    df['hasKumo'] = df['kumoTop'].notna() & df['kumoBottom'].notna()
    df['is_above_kumo'] = df['hasKumo'] & (df['close'] > df['kumoTop'])
    df['is_below_kumo'] = df['hasKumo'] & (df['close'] < df['kumoBottom'])
    df['is_in_kumo'] = df['hasKumo'] & (df['close'] <= df['kumoTop']) & (df['close'] >= df['kumoBottom'])

    df['long_lines_pass'] = df['hasKumo'] & (conversionLine > df['kumoTop']) & (baseLine > df['kumoTop'])
    df['short_lines_pass'] = df['hasKumo'] & (conversionLine < df['kumoBottom']) & (baseLine < df['kumoBottom'])

    df['ma_val'] = df['close'].ewm(span=ma_length, adjust=False).mean()
    df['long_ma_pass'] = df['close'] > df['ma_val']
    df['short_ma_pass'] = df['close'] < df['ma_val']

    cci_ma = hlc3.rolling(cci_length).mean()
    cci_dev = (hlc3 - cci_ma).abs().rolling(cci_length).mean()
    df['cci_val'] = (hlc3 - cci_ma) / (0.015 * cci_dev + 1e-10)

    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - df['close'].shift(1)).abs()
    tr3 = (df['low'] - df['close'].shift(1)).abs()
    df['tr'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    up = df['high'] - df['high'].shift(1)
    down = df['low'].shift(1) - df['low']
    plus_dm = np.where((up > down) & (up > 0), up, 0)
    minus_dm = np.where((down > up) & (down > 0), down, 0)
    df['plus_dm'] = plus_dm
    df['minus_dm'] = minus_dm

    alpha = 1 / adx_len
    tr_smooth = df['tr'].ewm(alpha=alpha, adjust=False).mean()
    plus_smooth = pd.Series(plus_dm).ewm(alpha=alpha, adjust=False).mean()
    minus_smooth = pd.Series(minus_dm).ewm(alpha=alpha, adjust=False).mean()

    plus_di = 100 * plus_smooth / (tr_smooth + 1e-10)
    minus_di = 100 * minus_smooth / (tr_smooth + 1e-10)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    df['adx'] = dx.ewm(alpha=alpha, adjust=False).mean()

    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/rsi_len, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/rsi_len, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    df['rsi'] = 100 - 100 / (1 + rs)

    df['atr'] = df['tr'].ewm(alpha=1/natr_len, adjust=False).mean()
    df['natr'] = 100 * df['atr'] / df['close']

    bb_mid = df['close'].rolling(bbw_len).mean()
    bb_std = df['close'].rolling(bbw_len).std()
    df['bb_w'] = 100 * (2 * bbw_mult * bb_std) / (bb_mid + 1e-10)

    # Сигналы
    df['long_entry'] = (
        (df['cci_val'].shift(1) < cci_long_thr) & (df['cci_val'] > cci_long_thr) &
        (~use_ichi_cloud | df['is_above_kumo']) &
        (~use_ichi_lines | df['long_lines_pass']) &
        (~use_ma_dir | df['long_ma_pass']) &
        (~use_adx_filter | (df['adx'] >= adx_long_min) & (df['adx'] <= adx_long_max)) &
        (~use_rsi_filter | (df['rsi'] >= rsi_long_min) & (df['rsi'] <= rsi_long_max)) &
        (~use_natr_filter | (df['natr'] >= natr_long_min) & (df['natr'] <= natr_long_max)) &
        (~use_bbw_filter | (df['bb_w'] >= bbw_min_trend))
    )

    df['short_entry'] = (
        (df['cci_val'].shift(1) > cci_short_thr) & (df['cci_val'] < cci_short_thr) &
        (~use_ichi_cloud | df['is_below_kumo']) &
        (~use_ichi_lines | df['short_lines_pass']) &
        (~use_ma_dir | df['short_ma_pass']) &
        (~use_adx_filter | (df['adx'] >= adx_short_min) & (df['adx'] <= adx_short_max)) &
        (~use_rsi_filter | (df['rsi'] >= rsi_short_min) & (df['rsi'] <= rsi_short_max)) &
        (~use_natr_filter | (df['natr'] >= natr_short_min) & (df['natr'] <= natr_short_max)) &
        (~use_bbw_filter | (df['bb_w'] >= bbw_min_trend))
    )

    # === БЭКТЕСТ С ФИКСИРОВАННЫМ 1% РИСКА НА СДЕЛКУ ===
    max_lookback = 600
    capital = initial_capital
    position = 0.0  # количество монет
    entry_price = 0.0
    long_sl = short_sl = long_tp = short_tp = 0.0
    long_be_triggered = short_be_triggered = False
    allow_long = allow_short = True
    equity_curve = []
    dates = []

    for i in range(max_lookback, len(df)):
        row = df.iloc[i]
        price = row['close']

        current_equity = capital + position * price
        equity_curve.append(current_equity)
        dates.append(row['open_time'])

        # Сброс флагов
        if row['hasKumo'] and (not wait_flag_reset_till_flat or position == 0):
            if row['is_in_kumo'] or (row['low'] <= row['kumoTop'] and row['low'] >= row['kumoBottom']):
                allow_long = True
            if row['is_in_kumo'] or (row['high'] >= row['kumoBottom'] and row['high'] <= row['kumoTop']):
                allow_short = True

        # Выходы
        exit_price = None
        if position > 0:
            if be_enabled and not long_be_triggered and price >= entry_price * (1 + be_trig_pct / 100):
                long_be_triggered = True
                long_sl = max(long_sl, entry_price * (1 + be_offset_pct / 100))
            if row['low'] <= long_sl:
                exit_price = long_sl
            elif row['high'] >= long_tp:
                exit_price = long_tp
        elif position < 0:
            if be_enabled and not short_be_triggered and price <= entry_price * (1 - be_trig_pct / 100):
                short_be_triggered = True
                short_sl = min(short_sl, entry_price * (1 - be_offset_pct / 100))
            if row['high'] >= short_sl:
                exit_price = short_sl
            elif row['low'] <= short_tp:
                exit_price = short_tp

        if exit_price is not None:
            net_proceeds = abs(position) * exit_price * (1 - commission_rate)
            capital += net_proceeds
            position = 0.0
            long_be_triggered = short_be_triggered = False

        # Входы — только если нет открытой позиции
        current_risk_amount = capital * risk_per_trade  # 1% от текущего капитала

        if df['long_entry'].iloc[i] and position == 0 and allow_long:
            entry_price = price
            qty = current_risk_amount / price
            cost = qty * price
            commission = cost * commission_rate
            capital -= (cost + commission)
            position = qty
            long_sl = price * (1 - sl_pct / 100)
            long_tp = price * (1 + tp_pct / 100)
            allow_long = False
            allow_short = True

        elif df['short_entry'].iloc[i] and position == 0 and allow_short:
            entry_price = price
            qty = current_risk_amount / price
            proceeds = qty * price
            commission = proceeds * commission_rate
            capital += (proceeds - commission)
            position = -qty
            short_sl = price * (1 + sl_pct / 100)
            short_tp = price * (1 - tp_pct / 100)
            allow_short = False
            allow_long = True

    final_equity = capital + position * df.iloc[-1]['close']
    total_profit = final_equity - initial_capital

    equity_series = pd.Series(equity_curve, index=dates)

    return {
        'symbol': symbol,
        'tp_pct': tp_pct,
        'sl_pct': sl_pct,
        'be_trig_pct': be_trig_pct,
        'cci_length': cci_length,
        'adx_long_min': adx_long_min,
        'total_profit': total_profit,
        'final_equity': final_equity,
        'equity_curve': equity_series
    }


# === ГЛАВНЫЙ БЛОК ===
if __name__ == '__main__':
    symbols = ['ETHUSDT', 'DOGEUSDT', 'SOLUSDT']
    tp_pcts = [5.0, 6.0, 6.9, 8.0]
    sl_pcts = [1.0, 1.2, 1.5]
    be_trig_pcts = [1.5, 1.7, 2.0]
    cci_lengths = [25, 29, 33]
    adx_long_mins = [16, 18, 20]

    configs = list(product(symbols, tp_pcts, sl_pcts, be_trig_pcts, cci_lengths, adx_long_mins))

    print(f"Всего комбинаций: {len(configs)}")
    print("Запуск оптимизации...")

    start_time = time.time()
    with Pool(cpu_count()) as pool:
        results = pool.map(run_backtest, configs)

    results = [r for r in results if r is not None]
    print(f"Готово за {time.time() - start_time:.1f} сек.\n")

    top5 = sorted(results, key=lambda x: x['total_profit'], reverse=True)[:5]

    print("=" * 70)
    print("ТОП-5 КОНФИГУРАЦИЙ (1% риск на сделку)")
    print("=" * 70)
    for i, res in enumerate(top5, 1):
        print(f"{i}. {res['symbol']}")
        print(f"   TP: {res['tp_pct']}% | SL: {res['sl_pct']}% | BE: {res['be_trig_pct']}%")
        print(f"   CCI: {res['cci_length']} | ADX min: {res['adx_long_min']}")
        print(f"   Прибыль: ${res['total_profit']:,.0f} | Итог: ${res['final_equity']:,.0f}")
        print("   ---")

    best = top5[0]
    plt.figure(figsize=(14, 7))
    best['equity_curve'].plot()
    plt.title(f"Equity Curve — Лучшая стратегия (1% риск)\n{best['symbol']} | Прибыль ${best['total_profit']:,.0f}")
    plt.ylabel("Капитал ($)")
    plt.xlabel("Дата")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig("best_equity_curve_1percent_risk.png")
    plt.show()

    print("\nГрафик сохранён: best_equity_curve_1percent_risk.png")
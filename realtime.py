import pandas as pd
import numpy as np
import time
import logging
from binance.client import Client as BinanceClient
from binance import ThreadedWebsocketManager
from binance.enums import *
from bingx_client import BingxClient  # ← твой файл с классом

# === НАСТРОЙКИ ЛОГИРОВАНИЯ ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

# === КЛЮЧИ (ВСТАВЬ СВОИ) ===
BINANCE_API_KEY = ''      # Только для чтения свечей (можно даже без ключа)
BINANCE_API_SECRET = ''

BINGX_API_KEY = ''
BINGX_API_SECRET = ''

# === ПАРАМЕТРЫ ТОРГОВЛИ ===
SYMBOL = 'SOLUSDT'
INTERVAL = '1h'
RISK_PER_TRADE = 0.01  # 1% от капитала на сделку

# Лучшие параметры из оптимизации
tp_pct = 5.0
sl_pct = 1.0
be_trig_pct = 2.0
cci_length = 25
adx_long_min = 16

# Фиксированные параметры стратегии
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

# === КЛИЕНТЫ ===
binance_client = BinanceClient(BINANCE_API_KEY, BINANCE_API_SECRET)
bingx_client = BingxClient(BINGX_API_KEY, BINGX_API_SECRET, symbol=SYMBOL)

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ СОСТОЯНИЯ ===
df = pd.DataFrame()
position = 0.0  # количество контрактов (положительное — long, отрицательное — short)
entry_price = 0.0
long_sl = short_sl = long_tp = short_tp = 0.0
long_be_triggered = short_be_triggered = False
allow_long = allow_short = True
last_capital_update = time.time()

# === ПОЛУЧЕНИЕ ТЕКУЩЕГО БАЛАНСА USDT НА BINGX ===
def get_usdt_balance():
    try:
        # BingX не имеет прямого метода баланса в твоём клиенте — добавим простой запрос
        path = "/openApi/swap/v2/user/balance"
        resp = bingx_client._request("GET", path)
        if resp and resp.get('code') == 0:
            for asset in resp['data']:
                if asset['asset'] == 'USDT':
                    return float(asset['availableBalance'])
        return 10000.0  # fallback
    except Exception as e:
        logger.error(f"Ошибка получения баланса: {e}")
        return 10000.0

capital = get_usdt_balance()
logger.info(f"Стартовый капитал на BingX: {capital:.2f} USDT")

# === ОБРАБОТКА НОВОЙ ЗАКРЫТОЙ СВЕЧИ ===
def process_candle(msg):
    global df, position, entry_price, long_sl, long_tp, short_sl, short_tp
    global long_be_triggered, short_be_triggered, allow_long, allow_short, capital

    if msg['e'] == 'kline' and msg['k']['x']:  # Закрытая свеча
        k = msg['k']
        new_row = {
            'open_time': pd.to_datetime(int(k['t']), unit='ms'),
            'open': float(k['o']),
            'high': float(k['h']),
            'low': float(k['l']),
            'close': float(k['c'])
        }
        global df
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        df = df.tail(700)  # Хватит для всех индикаторов

        logger.info(f"Новая свеча: {new_row['close']}")

        # === РАСЧЁТ ИНДИКАТОРОВ ===
        hlc3 = (df['high'] + df['low'] + df['close']) / 3

        def donchian_high(s, l): return s.rolling(l).max()
        def donchian_low(s, l): return s.rolling(l).min()

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

        # === СИГНАЛЫ ===
        last = len(df) - 1
        long_signal = (
            (df['cci_val'].iloc[last-1] < cci_long_thr) and (df['cci_val'].iloc[last] > cci_long_thr) and
            (~use_ichi_cloud or df['is_above_kumo'].iloc[last]) and
            (~use_ichi_lines or df['long_lines_pass'].iloc[last]) and
            (~use_ma_dir or df['long_ma_pass'].iloc[last]) and
            (~use_adx_filter or (df['adx'].iloc[last] >= adx_long_min and df['adx'].iloc[last] <= adx_long_max)) and
            (~use_rsi_filter or (df['rsi'].iloc[last] >= rsi_long_min and df['rsi'].iloc[last] <= rsi_long_max)) and
            (~use_natr_filter or (df['natr'].iloc[last] >= natr_long_min and df['natr'].iloc[last] <= natr_long_max)) and
            (~use_bbw_filter or df['bb_w'].iloc[last] >= bbw_min_trend)
        )

        short_signal = (
            (df['cci_val'].iloc[last-1] > cci_short_thr) and (df['cci_val'].iloc[last] < cci_short_thr) and
            (~use_ichi_cloud or df['is_below_kumo'].iloc[last]) and
            (~use_ichi_lines or df['short_lines_pass'].iloc[last]) and
            (~use_ma_dir or df['short_ma_pass'].iloc[last]) and
            (~use_adx_filter or (df['adx'].iloc[last] >= adx_short_min and df['adx'].iloc[last] <= adx_short_max)) and
            (~use_rsi_filter or (df['rsi'].iloc[last] >= rsi_short_min and df['rsi'].iloc[last] <= rsi_short_max)) and
            (~use_natr_filter or (df['natr'].iloc[last] >= natr_short_min and df['natr'].iloc[last] <= natr_short_max)) and
            (~use_bbw_filter or df['bb_w'].iloc[last] >= bbw_min_trend)
        )

        price = df['close'].iloc[-1]

        # Обновляем капитал раз в 30 сек
        global last_capital_update
        if time.time() - last_capital_update > 30:
            capital = get_usdt_balance()
            last_capital_update = time.time()

        risk_amount = capital * RISK_PER_TRADE

        # === ВХОДЫ ===
        if long_signal and position == 0 and allow_long:
            qty = risk_amount / price
            logger.info(f"ОТКРЫВАЕМ LONG: {qty:.6f} {SYMBOL} по {price}")
            resp = bingx_client.place_market_order("long", qty, stop=round(price * (1 - sl_pct / 100), 1), tp=round(price * (1 + tp_pct / 100), 1))
            logger.info(f"Ответ BingX: {resp}")

            if resp and resp.get('code') == 0:
                entry_price = price
                position = qty
                long_sl = price * (1 - sl_pct / 100)
                long_tp = price * (1 + tp_pct / 100)
                allow_long = False
                allow_short = True

        elif short_signal and position == 0 and allow_short:
            qty = risk_amount / price
            logger.info(f"ОТКРЫВАЕМ SHORT: {qty:.6f} {SYMBOL} по {price}")
            resp = bingx_client.place_market_order("short", qty, stop=round(price * (1 + sl_pct / 100), 1), tp=round(price * (1 - tp_pct / 100), 1))
            logger.info(f"Ответ BingX: {resp}")

            if resp and resp.get('code') == 0:
                entry_price = price
                position = -qty
                short_sl = price * (1 + sl_pct / 100)
                short_tp = price * (1 - tp_pct / 100)
                allow_short = False
                allow_long = True

        # === СБРОС ФЛАГОВ ===
        row = df.iloc[-1]
        if row['hasKumo'] and (not wait_flag_reset_till_flat or position == 0):
            if row['is_in_kumo'] or (row['low'] <= row['kumoTop'] and row['low'] >= row['kumoBottom']):
                allow_long = True
            if row['is_in_kumo'] or (row['high'] >= row['kumoBottom'] and row['high'] <= row['kumoTop']):
                allow_short = True


# === ЗАПУСК БОТА ===
def main():
    logger.info("Запуск бота Third Eye на BingX + Binance candles")

    # Загрузка истории
    global df
    klines = binance_client.get_klines(symbol=SYMBOL, interval=INTERVAL, limit=700)
    df = pd.DataFrame(klines, columns=['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'trades', 'tbbav', 'tbqav', 'ignore'])
    df = df[['open_time', 'open', 'high', 'low', 'close']].astype(float)
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')

    # WebSocket Binance
    twm = ThreadedWebsocketManager()
    twm.start()
    twm.start_kline_socket(callback=process_candle, symbol=SYMBOL, interval=INTERVAL)

    logger.info("Бот запущен. Ожидание сигналов...")
    twm.join()


if __name__ == '__main__':
    main()
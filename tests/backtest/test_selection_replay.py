import numpy as np
import pandas as pd

from gridtrade.backtest.cache import ParquetCache
from gridtrade.exchanges.base import CANDLE_COLS


def _bars(symbol, n=300, seed=0, start='2024-01-01'):
    rng = np.random.RandomState(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = np.concatenate([[100.0], close[:-1]])
    t = pd.date_range(start, periods=n, freq='1H')
    return pd.DataFrame({
        'symbol': symbol, 'candle_begin_time': t,
        'open': open_, 'high': np.maximum(open_, close) * 1.001,
        'low': np.minimum(open_, close) * 0.999, 'close': close,
        'vol': rng.uniform(1e3, 1e4, n), 'volCcy': rng.uniform(1e3, 1e4, n),
        'quote_volume': rng.uniform(1e6, 1e7, n),
    })[CANDLE_COLS]


def _seed_cache(tmp_path, symbols):
    cache = ParquetCache(str(tmp_path))
    for i, s in enumerate(symbols):
        df = _bars(s, seed=i + 1)
        for day, g in df.groupby(df['candle_begin_time'].dt.strftime('%Y-%m-%d')):
            cache.write('1h', s, day, g.reset_index(drop=True))
    return cache


STRAT = {'period': '12H', 'weight_list': [1, 1, 1], 'choose_symbols': 1,
         'max_candle_num': 160}
FACTORS = {'Reg_v2_5': True, 'Sgcz_5': True, 'Er_2': True}


def test_load_full_series(tmp_path):
    from gridtrade.backtest.selection_replay import load_full_series
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    series = load_full_series(cache, syms, timeframe='1h')
    assert set(series) == set(syms)
    assert list(series['AAA/USDT:USDT'].columns) == CANDLE_COLS
    assert series['AAA/USDT:USDT']['candle_begin_time'].is_monotonic_increasing


def test_replay_selection_emits_picks(tmp_path):
    from gridtrade.backtest.selection_replay import replay_selection
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    run_times = [pd.Timestamp('2024-01-10 00:00:00'), pd.Timestamp('2024-01-10 12:00:00')]
    picks = []
    n = replay_selection(cache, syms, run_times, STRAT, FACTORS, 8,
                         lambda rt, off, row: picks.append((rt, off, row['symbol'])),
                         timeframe='1h')
    assert n == 2
    assert len(picks) >= 1                     # 至少选出一个币
    # 每个 pick 的 row 含布网所需列
    assert all(isinstance(p[2], str) for p in picks)

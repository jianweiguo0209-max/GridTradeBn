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
    n = replay_selection(cache, syms, run_times, STRAT, FACTORS,
                         lambda rt, off, row: picks.append((rt, off, row['symbol'])),
                         timeframe='1h')
    assert n == 2
    assert len(picks) >= 1                     # 至少选出一个币
    # 每个 pick 的 row 含布网所需列
    assert all(isinstance(p[2], str) for p in picks)


def _series_with_vol(symbol, n=60, vol_per_bar=1.0, start='2024-01-01'):
    import pandas as pd, numpy as np
    t = pd.date_range(start, periods=n, freq='1H')
    close = np.full(n, 100.0)
    return pd.DataFrame({
        'symbol': symbol, 'candle_begin_time': t,
        'open': close, 'high': close * 1.001, 'low': close * 0.999, 'close': close,
        'vol': 1.0, 'volCcy': 1.0, 'quote_volume': float(vol_per_bar),
    })


def test_build_pit_candidates_floor_and_blacklist_and_pit():
    import pandas as pd
    from gridtrade.backtest.selection_replay import build_pit_candidates
    # HIGH: 每根 100 → 前置24根和=2400；LOW: 每根 10 → 24根和=240
    series = {'HIGH/USDC:USDC': _series_with_vol('HIGH/USDC:USDC', vol_per_bar=100.0),
              'LOW/USDC:USDC':  _series_with_vol('LOW/USDC:USDC',  vol_per_bar=10.0),
              'BAN/USDC:USDC':  _series_with_vol('BAN/USDC:USDC',  vol_per_bar=100.0)}
    rt = pd.Timestamp('2024-01-03 00:00:00')   # 有 >24 根 < rt
    # 门槛 1000：HIGH(2400)过、LOW(240)剔；BAN 被黑名单剔
    out = build_pit_candidates(series, rt, max_candle_num=160,
                               min_quote_volume=1000.0, blacklist=('BAN/USDC:USDC',))
    assert set(out) == {'HIGH/USDC:USDC'}
    # 门槛 0=停用：HIGH+LOW 都在（BAN 仍被黑名单剔）
    out0 = build_pit_candidates(series, rt, max_candle_num=160,
                                min_quote_volume=0.0, blacklist=('BAN/USDC:USDC',))
    assert set(out0) == {'HIGH/USDC:USDC', 'LOW/USDC:USDC'}


def test_build_pit_candidates_no_lookahead():
    import pandas as pd
    from gridtrade.backtest.selection_replay import build_pit_candidates
    # 前 30 根量=10（和=240<1000），第 30 根后量飙到 1000。run_time 卡在飙升前 → 仍按低量剔。
    import numpy as np
    t = pd.date_range('2024-01-01', periods=60, freq='1H')
    qv = np.concatenate([np.full(30, 10.0), np.full(30, 1000.0)])
    df = pd.DataFrame({'symbol': 'X/USDC:USDC', 'candle_begin_time': t,
                       'open': 100.0, 'high': 100.1, 'low': 99.9, 'close': 100.0,
                       'vol': 1.0, 'volCcy': 1.0, 'quote_volume': qv})
    rt = t[28]   # 只看得到前 28 根（都是 10）
    out = build_pit_candidates({'X/USDC:USDC': df}, rt, max_candle_num=160, min_quote_volume=1000.0)
    assert out == {}          # 未来的高量不算进来（无未来函数）

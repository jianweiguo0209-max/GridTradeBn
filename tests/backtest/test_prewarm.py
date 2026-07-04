import pandas as pd

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument, CANDLE_COLS

SYMS = ['AAA/USDT:USDT', 'BBB/USDT:USDT']


def _bars(symbol, start_ms, n):
    ts = [start_ms + i * 3600_000 for i in range(n)]
    return pd.DataFrame({'symbol': symbol, 'candle_begin_time': pd.to_datetime(ts, unit='ms'),
                         'open': [1.0] * n, 'high': [2.0] * n, 'low': [0.5] * n,
                         'close': [1.5] * n, 'vol': [9.0] * n, 'volCcy': [9.0] * n,
                         'quote_volume': [13.0] * n})[CANDLE_COLS]


def _ds(tmp_path, ex):
    from gridtrade.backtest.cache import ParquetCache
    from gridtrade.backtest.datasource import DataSource
    return DataSource(ex, ParquetCache(str(tmp_path)), timeframe='1h')


def test_prewarm_ohlcv_populates_cache_then_offline(tmp_path):
    from gridtrade.backtest.prewarm import prewarm_ohlcv
    start = 1_704_067_200_000
    ex = FakeExchange(instruments=[Instrument(s, 0.1, 0.001, 0.001, 'live', 0) for s in SYMS])
    for s in SYMS:
        ex.seed_ohlcv(s, _bars(s, start, 48))
    ds = _ds(tmp_path, ex)
    stat = prewarm_ohlcv(ds, SYMS, start, start + 47 * 3600_000)
    assert stat['symbols'] == 2 and stat['rows'] == 96

    # 预热后离线：用会报错的交易所，仅靠缓存仍取得
    class Offline(FakeExchange):
        def fetch_ohlcv(self, *a, **k):
            raise AssertionError('should be offline after prewarm')
    off = Offline(instruments=[Instrument(s, 0.1, 0.001, 0.001, 'live', 0) for s in SYMS])
    ds2 = _ds(tmp_path, off)
    df = ds2.fetch_ohlcv_range('AAA/USDT:USDT', start, start + 47 * 3600_000)
    assert len(df) == 48


def test_prewarm_ohlcv_skips_bad_coin(tmp_path):
    # 全市场含个别不可拉取币（ccxt BadSymbol）；坏币跳过、不中断全池、好币照常缓存。
    from gridtrade.backtest.prewarm import prewarm_ohlcv
    start = 1_704_067_200_000
    syms = ['GOOD/USDT:USDT', 'BAD/USDT:USDT']

    class _BadOne(FakeExchange):
        def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
            if symbol == 'BAD/USDT:USDT':
                raise ValueError('hyperliquid does not have market symbol BAD')
            return super().fetch_ohlcv(symbol, timeframe, start_ms, end_ms)

    ex = _BadOne(instruments=[Instrument(s, 0.1, 0.001, 0.001, 'live', 0) for s in syms])
    ex.seed_ohlcv('GOOD/USDT:USDT', _bars('GOOD/USDT:USDT', start, 48))
    ds = _ds(tmp_path, ex)
    stat = prewarm_ohlcv(ds, syms, start, start + 47 * 3600_000)
    assert stat['skipped'] == 1                          # BAD 被跳过
    assert stat['symbols'] == 1 and stat['rows'] == 48   # GOOD 正常缓存、未受影响


def test_resolve_universe_filters(tmp_path):
    from gridtrade.backtest.prewarm import resolve_universe
    insts = [Instrument('AAA/USDT:USDT', 0.1, 0.001, 0.001, 'live', 0),
             Instrument('BBB/USDT:USDT', 0.1, 0.001, 0.001, 'expired', 0),
             Instrument('CCC/USDT:USDT', 0.1, 0.001, 0.001, 'live', 0)]
    ex = FakeExchange(instruments=insts)
    ds = _ds(tmp_path, ex)
    uni = resolve_universe(ds)
    assert 'AAA/USDT:USDT' in uni and 'CCC/USDT:USDT' in uni
    assert 'BBB/USDT:USDT' not in uni            # 非 live 过滤掉
    assert len(resolve_universe(ds, limit=1)) == 1

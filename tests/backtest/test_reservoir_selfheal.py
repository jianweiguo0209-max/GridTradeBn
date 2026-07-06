# tests/backtest/test_reservoir_selfheal.py
"""warm 自愈：坏 1m 格触发重下、好格跳过。"""
import pandas as pd
from gridtrade.backtest import reservoir as RV


class _StubCache:
    def __init__(self, data):
        self.data = dict(data)
        self.writes = []
    def exists(self, ns, sym, day):
        return (ns, sym, day) in self.data
    def read(self, ns, sym, day):
        return self.data.get((ns, sym, day))
    def write(self, ns, sym, day, df):
        self.data[(ns, sym, day)] = df; self.writes.append((ns, sym, day))
    def write_empty(self, ns, sym, day, cols):
        self.data[(ns, sym, day)] = pd.DataFrame(columns=cols)
        self.writes.append((ns, sym, day))


def _h(closes):
    idx = pd.date_range('2026-03-15', periods=len(closes), freq='1H')
    return pd.DataFrame({'candle_begin_time': idx, 'open': closes,
                         'high': [c * 1.001 for c in closes], 'low': [c * 0.999 for c in closes],
                         'close': closes, 'vol': 1.0, 'volCcy': 1.0, 'quote_volume': 1.0})


def _good_1m():
    idx = pd.date_range('2026-03-15', periods=24 * 6, freq='10min')
    return pd.DataFrame({'candle_begin_time': idx, 'open': 4.0, 'high': 4.004,
                         'low': 3.996, 'close': 4.0, 'vol': 1.0, 'volCcy': 1.0,
                         'quote_volume': 1.0})


SYM = 'BTC/USDC:USDC'
DAY = '2026-03-15'


def test_day_1m_all_valid_detects_bad_and_good():
    good_h = _h([4.0] * 24)
    bad = _StubCache({('1h', SYM, DAY): good_h,
                      ('1m', SYM, DAY): good_h.iloc[:0].copy()})   # 1m 空 → 坏
    assert RV._day_1m_all_valid(bad, [SYM], DAY) is False
    good = _StubCache({('1h', SYM, DAY): good_h, ('1m', SYM, DAY): _good_1m()})
    assert RV._day_1m_all_valid(good, [SYM], DAY) is True


def test_warm_refetches_bad_cell(monkeypatch, tmp_path):
    c = _StubCache({('1h', SYM, DAY): _h([4.0] * 24),
                    ('1m', SYM, DAY): _h([4.0] * 24).iloc[:0].copy()})   # 坏：1m 空
    monkeypatch.setattr(RV, '_s3_cp', lambda day, dest, log=print: (open(dest, 'w').close() or True))
    monkeypatch.setattr(RV.pd, 'read_parquet', lambda p: pd.DataFrame({'coin': ['BTC']}))
    monkeypatch.setattr(RV, 'candles_1s_resample',
                        lambda raw, smap, rule: {SYM: _good_1m()} if rule == '1min'
                        else {SYM: _h([4.0] * 24)})
    start = int(pd.Timestamp(DAY).value // 1_000_000)
    end = start + 86_400_000 - 1
    stat = RV.warm_reservoir_ohlcv(c, [SYM], start, end, workdir=str(tmp_path))
    assert ('1m', SYM, DAY) in c.writes          # 坏格被重写
    assert stat['skipped_cached'] == 0           # 未跳过（校验不过）


def test_warm_skips_valid_cell(monkeypatch, tmp_path):
    c = _StubCache({('1h', SYM, DAY): _h([4.0] * 24), ('1m', SYM, DAY): _good_1m()})
    called = {'s3': 0}
    monkeypatch.setattr(RV, '_s3_cp', lambda day, dest, log=print: called.__setitem__('s3', called['s3'] + 1) or True)
    start = int(pd.Timestamp(DAY).value // 1_000_000)
    end = start + 86_400_000 - 1
    stat = RV.warm_reservoir_ohlcv(c, [SYM], start, end, workdir=str(tmp_path))
    assert stat['skipped_cached'] == 1 and called['s3'] == 0   # 好格跳过、不下载

# tests/runtime/test_dbadmin_validate1m.py
"""validate-1m 清库：扫描分类 + 坏格聚合成天重取 + dry-run + 幂等。"""
import pandas as pd
from gridtrade.runtime.dbadmin import validate_1m_cache


class _Cache:
    def __init__(self):
        self.data = {}
        self.days = {}
    def put(self, ns, sym, day, df):
        self.data[(ns, sym, day)] = df
        self.days.setdefault((ns, sym), []).append(day)
    def read(self, ns, sym, day): return self.data.get((ns, sym, day))
    def list_days(self, ns, sym): return sorted(self.days.get((ns, sym), []))
    def list_symbols(self, ns):
        return sorted({sym for (n, sym) in self.days if n == ns})


def _h(closes):
    idx = pd.date_range('2026-03-15', periods=len(closes), freq='1H')
    return pd.DataFrame({'candle_begin_time': idx, 'open': closes,
                         'high': [c * 1.001 for c in closes], 'low': [c * 0.999 for c in closes],
                         'close': closes})


def _m(n, freq='1min'):
    idx = pd.date_range('2026-03-15', periods=n, freq=freq)
    return pd.DataFrame({'candle_begin_time': idx, 'open': 4.0,
                         'high': 4.004, 'low': 3.996, 'close': 4.0})


def _seed():
    c = _Cache()
    c.put('1h', 'GOOD/USDC:USDC', '2026-03-15', _h([4.0] * 24))
    c.put('1m', 'GOOD/USDC:USDC', '2026-03-15', _m(24 * 6, '10min'))   # 每小时都有 → ok
    c.put('1h', 'BAD/USDC:USDC', '2026-03-15', _h([4.0] * 24))
    c.put('1m', 'BAD/USDC:USDC', '2026-03-15', _m(180))               # 只前 3h → hour_gap
    c.put('1h', 'EMPTY/USDC:USDC', '2026-03-15', _h([])[:0])
    c.put('1m', 'EMPTY/USDC:USDC', '2026-03-15', _m(0))               # 1h 空 → no_1h_ref
    return c


def test_dry_run_classifies_no_refetch():
    c = _seed()
    calls = []
    rep = validate_1m_cache(c, dry_run=True,
                            warm_fn=lambda *a, **k: calls.append(a), log=lambda *a: None)
    assert rep['scanned'] == 3 and rep['ok'] == 1
    assert rep['hour_gap'] == 1 and rep['no_1h_ref'] == 1
    assert rep['refetched_days'] == 0 and calls == []


def test_refetch_bad_day_and_fix():
    c = _seed()
    def _warm(cache, syms, s_ms, e_ms, **k):
        cache.data[('1m', 'BAD/USDC:USDC', '2026-03-15')] = _m(24 * 6, '10min')
    rep = validate_1m_cache(c, dry_run=False, warm_fn=_warm, log=lambda *a: None)
    assert rep['hour_gap'] == 1 and rep['refetched_days'] == 1
    assert rep['still_bad'] == 0


def test_idempotent_second_run_noop():
    c = _seed()
    def _warm(cache, syms, s_ms, e_ms, **k):
        cache.data[('1m', 'BAD/USDC:USDC', '2026-03-15')] = _m(24 * 6, '10min')
    validate_1m_cache(c, dry_run=False, warm_fn=_warm, log=lambda *a: None)
    calls = []
    rep2 = validate_1m_cache(c, dry_run=False,
                             warm_fn=lambda *a, **k: calls.append(a), log=lambda *a: None)
    assert rep2['refetched_days'] == 0 and calls == []   # 已修好，第二遍不重取

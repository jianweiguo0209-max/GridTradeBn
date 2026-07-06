# tests/backtest/test_validate_1m.py
"""1m 缓存完整性判据（spec 2026-07-07-1m-cache-integrity）。"""
import pandas as pd
from gridtrade.backtest.reservoir import validate_1m_cell


def _bars(begin, closes, tf='1min'):
    idx = pd.date_range(begin, periods=len(closes), freq=tf)
    return pd.DataFrame({'candle_begin_time': idx,
                         'open': closes, 'high': [c * 1.001 for c in closes],
                         'low': [c * 0.999 for c in closes], 'close': closes})


def test_no_1h_ref_is_ok():
    m = _bars('2026-03-15', [4.0] * 100)
    assert validate_1m_cell(m, None) == (True, 'no_1h_ref')
    assert validate_1m_cell(m, pd.DataFrame(columns=['candle_begin_time', 'high',
                                                     'low', 'close'])) == (True, 'no_1h_ref')


def test_empty_1m_with_no_1h_is_ok():
    empty = pd.DataFrame(columns=['candle_begin_time', 'high', 'low', 'close'])
    assert validate_1m_cell(empty, None)[0] is True


def test_range_mismatch_flagged():
    # TRUMP 型：1h 平静(4.0±0.1%)，1m 假崩到 2.0 → 振幅差远超 5%
    h = _bars('2026-03-15', [4.0] * 24, tf='1H')
    m = _bars('2026-03-15', [4.0] * 30 + [2.0] * 30)
    ok, reason = validate_1m_cell(m, h)
    assert ok is False and reason == 'range_mismatch'


def test_hour_gap_flagged():
    # GMX 型：1h 满 24 根，1m 只覆盖前 3 小时(180根) → 后续小时零 bar
    h = _bars('2026-03-15', [4.0] * 24, tf='1H')
    m = _bars('2026-03-15', [4.0] * 180)
    ok, reason = validate_1m_cell(m, h)
    assert ok is False and reason == 'hour_gap'


def test_empty_1m_with_full_1h_is_bad():
    h = _bars('2026-03-15', [4.0] * 24, tf='1H')
    empty = pd.DataFrame(columns=['candle_begin_time', 'open', 'high', 'low', 'close'])
    ok, reason = validate_1m_cell(empty, h)
    assert ok is False and reason == 'hour_gap'


def test_legit_sparse_is_ok():
    # 合法稀疏：每个 1h 小时里都有 1m bar，只是分钟级有缺（不是整小时空洞）
    h = _bars('2026-03-15', [4.0] * 24, tf='1H')
    idx = pd.date_range('2026-03-15', periods=24 * 6, freq='10min')
    m = pd.DataFrame({'candle_begin_time': idx, 'open': 4.0,
                      'high': 4.004, 'low': 3.996, 'close': 4.0})
    assert validate_1m_cell(m, h) == (True, 'ok')

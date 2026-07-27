"""window_labels_batch 与逐窗 window_label 的**逐位 parity**(同 cal_factor/cal_factor_batch 套路)。

批量变体只为回测吞吐存在(~1400 轮 × ~280 币,逐窗调会 O(n) 重算掩码);
数学必须与逐窗版逐位一致,否则回测与实盘就分叉了。
"""
import numpy as np
import pandas as pd

from gridtrade.core.p12_labels import (MIN_WINDOW_BARS, window_label,
                                       window_labels_batch)


def _bars(n=3000, seed=7, start='2026-07-01'):
    rng = np.random.default_rng(seed)
    t = pd.date_range(start, periods=n, freq='1min')
    c = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.0015, n)))
    return pd.DataFrame({'candle_begin_time': t, 'close': c,
                         'high': c * (1 + rng.uniform(0, 0.004, n)),
                         'low': c * (1 - rng.uniform(0, 0.004, n))})


def test_batch_matches_window_label_bitwise_across_many_starts():
    bars = _bars()
    starts = [bars['candle_begin_time'].iloc[0] + pd.Timedelta(minutes=k)
              for k in range(0, 2200, 37)]          # 60 个窗口起点,含窗尾越界的
    got = window_labels_batch(bars, starts)
    assert len(got) == len(starts)
    n_valid = 0
    for w0, g in zip(starts, got):
        ref = window_label(bars, w0, w0 + pd.Timedelta(hours=12))
        if ref is None:
            assert g is None, w0
            continue
        n_valid += 1
        assert g is not None, w0
        assert g[0] == ref[0], ('cross1', w0)        # 逐位,不是 approx
        assert g[1] == ref[1], ('mae', w0)
    assert n_valid >= 20, '有效窗太少(%d),parity 没咬到' % n_valid


def test_batch_returns_none_for_short_windows():
    bars = _bars(n=MIN_WINDOW_BARS - 1)
    got = window_labels_batch(bars, [bars['candle_begin_time'].iloc[0]])
    assert got == [None]


def test_batch_empty_inputs():
    assert window_labels_batch(_bars(n=100), []) == []
    empty = pd.DataFrame({'candle_begin_time': pd.to_datetime([]), 'close': [],
                          'high': [], 'low': []})
    assert window_labels_batch(empty, [pd.Timestamp('2026-07-01')]) == [None]


def test_batch_gap_window_uses_same_predecessor_as_single():
    """窗内有断档(缺 bar)时两版必须仍然一致——dstep 是位置差分,断档处两版看同一前驱。"""
    bars = _bars(n=2000)
    bars = pd.concat([bars.iloc[:800], bars.iloc[900:]], ignore_index=True)
    w0 = bars['candle_begin_time'].iloc[0]
    ref = window_label(bars, w0, w0 + pd.Timedelta(hours=12))
    got = window_labels_batch(bars, [w0])[0]
    assert got == ref

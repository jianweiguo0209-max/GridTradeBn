import numpy as np
import pandas as pd
import pytest

from gridtrade.core.p12_labels import (MIN_WINDOW_BARS, ladder_dstep, p12_eff,
                                       window_label)


def _bars(closes, start='2026-07-01', highs=None, lows=None):
    n = len(closes)
    t = pd.date_range(start, periods=n, freq='1min')
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({'candle_begin_time': t, 'close': c,
                         'high': np.asarray(highs, float) if highs is not None else c,
                         'low': np.asarray(lows, float) if lows is not None else c})


def test_ladder_dstep_counts_level_crossings():
    # 100 → 101.1 跨过 100×1.01=101 一级;首根 prepend ⇒ dstep[0]=0
    d = ladder_dstep(np.array([100.0, 100.5, 101.1, 100.5]))
    assert d[0] == 0 and d.sum() == 1          # 上穿一次(462→463,101.1仍在463)


def test_window_label_includes_boundary_transition():
    # 窗前最后一根 100.0 → 窗首根 101.1:过渡发生在窗首根,必须计入(同 stage_L 整段 diff 再切窗)
    n = 720
    closes = [100.0] * 61 + [101.1] * (n)      # 61 根窗前(其中最后一根前也全平)
    bars = _bars(closes)
    w0 = bars['candle_begin_time'].iloc[61]
    r = window_label(bars, w0, w0 + pd.Timedelta(hours=12))
    assert r is not None
    cross1, mae = r
    assert cross1 == 1.0                        # 唯一过渡在窗首根
    assert mae == 0.0                           # o=窗首收盘 101.1,窗内无偏离


def test_window_label_o_is_first_close_and_mae_uses_high_low():
    n = 720
    closes = [100.0] * n
    highs = list(closes); lows = list(closes)
    highs[300] = 108.0                          # 窗内最大上偏 8%
    lows[500] = 95.0                            # 窗内最大下偏 5%
    bars = _bars(closes, highs=highs, lows=lows)
    w0 = bars['candle_begin_time'].iloc[0]
    cross1, mae = window_label(bars, w0, w0 + pd.Timedelta(hours=12))
    assert mae == pytest.approx(0.08)


def test_window_label_returns_none_below_600_bars():
    bars = _bars([100.0] * 599)
    w0 = bars['candle_begin_time'].iloc[0]
    assert window_label(bars, w0, w0 + pd.Timedelta(hours=12)) is None
    assert MIN_WINDOW_BARS == 600


def test_p12_eff_formula():
    assert p12_eff(10.0, 0.05) == pytest.approx(10.0 / (1.0 + 100.0 * 0.05))

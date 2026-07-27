import pandas as pd

from gridtrade.config import DEFAULT_STRATEGY_CONFIG
from gridtrade.core.tick_fit import filter_tick_fit


def _df():
    # close=100/Atr_5=0.05/middle_5=100 ⇒ band3:range=15%,spacing_ratio=0.025,
    # calc_grid_params_v2: spacing=(30)/grid_count;grid_count=round(30/2.5)=12→cmin16 ⇒ 16 格
    # spacing = 30/16 = 1.875
    rows = [dict(symbol='FINE/USDT', close=100.0, Atr_5=0.05, middle_5=100.0),
            dict(symbol='COARSE/USDT', close=100.0, Atr_5=0.05, middle_5=100.0),
            dict(symbol='NOTICK/USDT', close=100.0, Atr_5=0.05, middle_5=100.0)]
    return pd.DataFrame(rows)


def test_coarse_tick_dropped_fine_kept_missing_failopen():
    ticks = {'FINE/USDT': 0.01, 'COARSE/USDT': 1.0}   # 1.875/1.0 < 3 ⇒ 剔;NOTICK 缺 ⇒ 留
    out, dropped = filter_tick_fit(_df(), ticks, DEFAULT_STRATEGY_CONFIG, 3.0)
    assert list(out['symbol']) == ['FINE/USDT', 'NOTICK/USDT']
    assert dropped == ['COARSE/USDT']


def test_min_ticks_zero_disables():
    out, dropped = filter_tick_fit(_df(), {'COARSE/USDT': 1.0}, DEFAULT_STRATEGY_CONFIG, 0.0)
    assert len(out) == 3 and dropped == []

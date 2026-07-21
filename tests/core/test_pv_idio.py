"""pv 截面条件化(pv_idio)退出语义:
- 两参缺一 = 关,行为与旧版逐位相同(宽松 pv_pnl_thr 单轨)
- idio_tight=1 的尖峰时刻按紧阈值 pv_idio_thr 开火;=0/缺行(NaN)只走宽松阈值
"""
import numpy as np
import pandas as pd

from gridtrade.core.grid_engine import _apply_exit

CAP = 1000.0
C_RATE = 0.0005
STOP = {'stop_loss': 9.0}          # 中和固定止损,只留 pv 层


def _mk(net_values, pv, tight=None):
    n = len(net_values)
    t = pd.date_range('2024-03-01', periods=n, freq='1min')
    df = pd.DataFrame({'candle_begin_time': t,
                       'net_value': np.asarray(net_values, dtype='float64'),
                       'hold_num': np.ones(n), 'close': np.full(n, 100.0)})
    pv_df = pd.DataFrame({'candle_begin_time': t, 'pv_spike': np.asarray(pv, dtype='int64')})
    idio_df = None
    if tight is not None:
        idio_df = pd.DataFrame({'candle_begin_time': t,
                                'idio_tight': np.asarray(tight, dtype='int8')})
    return df, pv_df, idio_df


def test_off_by_default_loose_only():
    # 亏 0.4%(浅于宽松阈值 1%)+尖峰:未传 idio 参数 → 不触发
    df, pv_df, _ = _mk([1.0, 0.996, 0.996], pv=[0, 1, 0])
    _, reason, _ = _apply_exit(df, CAP, C_RATE, STOP, 0.05, pv_df, pv_pnl_thr=-0.01)
    assert reason is None


def test_tight_fires_only_when_idio():
    # 同样浅亏+尖峰,idio_tight=1 → 紧阈值(+0.005:pr<0.5%即认)开火
    df, pv_df, idio = _mk([1.0, 0.996, 0.996], pv=[0, 1, 0], tight=[0, 1, 0])
    tr, reason, _ = _apply_exit(df, CAP, C_RATE, STOP, 0.05, pv_df,
                                pv_pnl_thr=-0.01, pv_idio_df=idio, pv_idio_thr=0.005)
    assert reason == 'pv主动止损'
    assert len(tr) == 2                      # 在第2根(尖峰+idio)截断
    # idio_tight=0 → 不开火
    df, pv_df, idio = _mk([1.0, 0.996, 0.996], pv=[0, 1, 0], tight=[0, 0, 0])
    _, reason, _ = _apply_exit(df, CAP, C_RATE, STOP, 0.05, pv_df,
                               pv_pnl_thr=-0.01, pv_idio_df=idio, pv_idio_thr=0.005)
    assert reason is None


def test_loose_track_unaffected():
    # 深亏(1.2%>宽松阈值)+尖峰:无论 idio 与否宽松轨照常开火,且触发 bar 一致
    for tight in ([0, 0, 0], [0, 1, 0]):
        df, pv_df, idio = _mk([1.0, 0.988, 0.988], pv=[0, 1, 0], tight=tight)
        tr, reason, _ = _apply_exit(df, CAP, C_RATE, STOP, 0.05, pv_df,
                                    pv_pnl_thr=-0.01, pv_idio_df=idio, pv_idio_thr=0.005)
        assert reason == 'pv主动止损' and len(tr) == 2


def test_idio_missing_rows_are_conservative():
    # idio_df 缺行(merge 后 NaN)→ 视为不紧,不开火
    df, pv_df, idio = _mk([1.0, 0.996, 0.996], pv=[0, 1, 0], tight=[1, 1, 1])
    idio = idio.iloc[:1]                     # 只留第1行,尖峰行缺失
    _, reason, _ = _apply_exit(df, CAP, C_RATE, STOP, 0.05, pv_df,
                               pv_pnl_thr=-0.01, pv_idio_df=idio, pv_idio_thr=0.005)
    assert reason is None

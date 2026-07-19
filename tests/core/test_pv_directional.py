"""方向性 PV 止损(spec 2026-07-19-pv-directional-design)。

pv 尖峰按同窗价格方向(pv_dir=sign(close_t−close_{t−period}))分涨/跌,由当前净仓符号门控:
净多只触发跌尖峰、净空只触发涨尖峰、零仓不触发;funding 同加零仓门控。flag 默认 False=现状零漂移。
"""
import numpy as np
import pandas as pd
import pytest

from gridtrade.core.grid_engine import calc_pv_spike, simulate_grid_engine

_GP = {'low_price': 100.0, 'high_price': 200.0, 'grid_count': 10,
       'stop_low_price': 90.0, 'stop_high_price': 210.0}


def _stop(directional):
    return {'stop_loss': 0.5, 'trailing_k': None, 'trailing_floor': None,
            'fundingRate_stop_loss': 0.0015, 'pv_pnl_thr': 0.005, 'pv_mult': 3,
            'pv_period': '15min', 'pv_n': 100, 'pv_directional': directional}


def _bars(path, n=240, spike_at=None):
    """path='up' 先上穿最近线(151.57)建净空;'down' 先下穿(141.42)建净多;'flat' 不触网。
    之后横盘;spike_at 处放量能尖峰,并让该尖峰方向与 spike_dir 一致(通过前 15 根价格走向控制)。"""
    t0 = pd.Timestamp('2026-01-01 00:00')
    rows = []
    if path == 'up':
        anchor = 153.0     # 上穿 151.57 → 净空
        rows.append({'candle_begin_time': t0, 'open': 150.0, 'high': 153.2, 'low': 149.9,
                     'close': anchor, 'quote_volume': 1e5})
    elif path == 'down':
        anchor = 140.0     # 下穿 141.42 → 净多
        rows.append({'candle_begin_time': t0, 'open': 150.0, 'high': 150.1, 'low': 139.8,
                     'close': anchor, 'quote_volume': 1e5})
    else:
        anchor = 150.0
        rows.append({'candle_begin_time': t0, 'open': 150.0, 'high': 150.3, 'low': 149.7,
                     'close': anchor, 'quote_volume': 1e5})
    for i in range(1, n):
        px = anchor
        rows.append({'candle_begin_time': t0 + pd.Timedelta(minutes=i), 'open': px,
                     'high': px * 1.0005, 'low': px * 0.9995, 'close': px, 'quote_volume': 1e5})
    df = pd.DataFrame(rows)
    if spike_at is not None:
        j, drift = spike_at
        # 尖峰前 15 根缓步 drift(±0.5%),之后**保持在漂移终点**(否则弹回会在粘滞窗内翻方向);
        # 幅度小不触第二条线。pv_dir=sign(drift)。
        for k in range(15):
            f = 1.0 + drift * (k + 1) / 15.0
            for col in ('open', 'high', 'low', 'close'):
                df.loc[j - 14 + k, col] = df.loc[j - 14 + k, col] * f
        for col in ('open', 'high', 'low', 'close'):
            df.loc[j + 1:, col] = df.loc[j + 1:, col] * (1.0 + drift)
        df.loc[j, 'quote_volume'] = 1e10
    return df


def _run(path, directional, spike_at=None):
    return simulate_grid_engine(_bars(path, spike_at=spike_at), _GP, cap=1000.0, leverage=5.0,
                                stop_cfg=_stop(directional), neutral_init=False,
                                active_stop_mode='pv', pv_pnl_thr=0.005, pv_mult=3,
                                pv_period='15min', pv_n=100)


# ---------- calc_pv_spike 的 pv_dir 列 ----------

def test_calc_pv_spike_emits_direction_column():
    t = pd.date_range('2026-01-01', periods=60, freq='1min')
    close = np.full(60, 100.0)
    close[40:] = 102.0                       # 40 起上台阶 → 同窗涨
    qv = np.full(60, 100.0); qv[45] = 1e9    # 45 放量(窗口 [31..45] 内价升)
    df = pd.DataFrame({'candle_begin_time': t, 'close': close, 'quote_volume': qv})
    out = calc_pv_spike(df, active_period='15min', mult=3, n=10)
    assert 'pv_dir' in out.columns
    assert int(out['pv_spike'].iloc[45]) == 1
    assert int(out['pv_dir'].iloc[45]) == 1              # 涨尖峰
    close2 = np.full(60, 100.0); close2[40:] = 98.0      # 40 起下台阶(落在窗 [31..45] 内)
    down = df.copy(); down['close'] = close2
    o2 = calc_pv_spike(down, active_period='15min', mult=3, n=10)
    assert int(o2['pv_dir'].iloc[45]) == -1


def test_calc_pv_spike_dir_zero_without_close():
    t = pd.date_range('2026-01-01', periods=30, freq='1min')
    df = pd.DataFrame({'candle_begin_time': t, 'quote_volume': np.full(30, 100.0)})
    out = calc_pv_spike(df, active_period='15min', mult=3, n=10)
    assert (out['pv_dir'] == 0).all()                    # 缺 close → fail-soft 恒 0


# ---------- 引擎方向门控矩阵 ----------

def test_net_short_up_spike_fires():
    """净空(上穿建仓) + 涨尖峰(逆向) → pv主动止损。"""
    r = _run('up', True, spike_at=(60, +0.005))
    assert r['exit_reason'] == 'pv主动止损'


def test_net_short_down_spike_does_not_fire():
    """净空 + 跌尖峰(顺向,回本中) → 不触发(骑到窗口结束)。"""
    r = _run('up', True, spike_at=(60, -0.005))
    assert r['exit_reason'] != 'pv主动止损'


def test_net_long_down_spike_fires():
    r = _run('down', True, spike_at=(60, -0.005))
    assert r['exit_reason'] == 'pv主动止损'


def test_net_long_up_spike_does_not_fire():
    r = _run('down', True, spike_at=(60, +0.005))
    assert r['exit_reason'] != 'pv主动止损'


def test_zero_position_spike_does_not_fire():
    """零仓(未触网)有尖峰也不触发 → '未触网'(取代 284fe1d 的零仓 pv 关格,两侧同改)。"""
    r = _run('flat', True, spike_at=(60, -0.005))
    assert r['n_trades'] == 0
    assert r['exit_reason'] == '未触网'


def test_zero_position_funding_gated_too():
    """flag 开:零仓 + 资金费率超阈也不触发(零仓关格无经济意义)。"""
    fd = pd.DataFrame([{'ts': int(pd.Timestamp('2026-01-01 01:30').value // 1_000_000),
                        'fundingRate': 0.002}])
    r = simulate_grid_engine(_bars('flat'), _GP, cap=1000.0, leverage=5.0,
                             stop_cfg=_stop(True), funding_df=fd, neutral_init=False,
                             active_stop_mode='pv', pv_pnl_thr=0.005, pv_mult=3,
                             pv_period='15min', pv_n=100)
    assert r['exit_reason'] == '未触网'


def test_flag_off_keeps_current_behavior():
    """flag=False 回归:顺向尖峰照触发(现状)、零仓 pv 也触发(284fe1d 现状)。"""
    r1 = _run('down', False, spike_at=(60, +0.005))      # 净多+涨尖峰,现状触发
    assert r1['exit_reason'] == 'pv主动止损'
    r2 = _run('flat', False, spike_at=(60, -0.005))      # 零仓,284fe1d 现状触发
    assert r2['exit_reason'] == 'pv主动止损'


# ---------- 标量(实盘) ↔ 向量(回测) 等价 ----------

def test_scalar_evaluate_exit_directional_matrix():
    from gridtrade.core.stop_rules import evaluate_exit
    cfg = _stop(True)
    kw = dict(net_value=1.0, stop_cfg=cfg, margin_rate=0.05, funding_rate=0.0)
    assert evaluate_exit(0.0, 0.0, pv_spike=1, pv_dir=-1, net_position=+5.0, **kw) == 'pv主动止损'
    assert evaluate_exit(0.0, 0.0, pv_spike=1, pv_dir=+1, net_position=+5.0, **kw) is None
    assert evaluate_exit(0.0, 0.0, pv_spike=1, pv_dir=+1, net_position=-5.0, **kw) == 'pv主动止损'
    assert evaluate_exit(0.0, 0.0, pv_spike=1, pv_dir=-1, net_position=-5.0, **kw) is None
    assert evaluate_exit(0.0, 0.0, pv_spike=1, pv_dir=-1, net_position=0.0, **kw) is None
    # funding 零仓门控
    assert evaluate_exit(0.0, 0.0, funding_rate=0.002, net_position=0.0, pv_spike=0, pv_dir=0,
                         net_value=1.0, stop_cfg=cfg, margin_rate=0.05) is None
    assert evaluate_exit(0.0, 0.0, funding_rate=0.002, net_position=1.0, pv_spike=0, pv_dir=0,
                         net_value=1.0, stop_cfg=cfg, margin_rate=0.05) == '资金费率止损'
    # net_position=None = 接线缺失 → fail-open 回旧行为
    assert evaluate_exit(0.0, 0.0, pv_spike=1, pv_dir=+1, net_position=None, **kw) == 'pv主动止损'
    # flag off → 方向/零仓全不管(现状)
    cfg0 = _stop(False)
    assert evaluate_exit(0.0, 0.0, pv_spike=1, pv_dir=+1, net_position=+5.0, net_value=1.0,
                         stop_cfg=cfg0, margin_rate=0.05, funding_rate=0.0) == 'pv主动止损'

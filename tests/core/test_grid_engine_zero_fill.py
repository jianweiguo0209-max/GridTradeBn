"""零成交(未触网)格也评估 pv/funding 退出信号,归因对齐实盘(2026-07-19,0G 实证)。

分歧:实盘 monitor 对**零成交**的活跃格仍每轮评估 pv/funding——pv 尖峰 + pnl(0)<pv_thr(0.005)
恒真 → pv主动止损关格(mainnet 0G/USDT 07-19 00:12 实证:零成交、pv 止损、回测 pv 尖峰 @00:07 对齐)。
回测旧实现在 trade_df.empty 时直接 return '未触网',跳过 pv/funding 评估 → exit_reason 分布与实盘背离
(实盘算 pv/资金费止损,回测一律"未触网")。pnl 恒 0(零成交无盈亏),仅退出归因差异。

修复:零成交(非破网)构造零仓净值序列(net_value≡1、pnl≡0)走 _apply_exit,pv/funding 尖峰即触发;
无信号仍 '未触网'(保留回测语义:确实没触网到期)。破网零成交仍 '破网'(不变)。
"""
import numpy as np
import pandas as pd

from gridtrade.core.grid_engine import simulate_grid_engine

_GP = {'low_price': 100.0, 'high_price': 200.0, 'grid_count': 10,
       'stop_low_price': 90.0, 'stop_high_price': 210.0}
_STOP = {'stop_loss': 0.045, 'trailing_k': 0.3, 'trailing_floor': 0.00618,
         'fundingRate_stop_loss': 0.0015, 'pv_pnl_thr': 0.005, 'pv_mult': 3,
         'pv_period': '15min', 'pv_n': 100}


def _no_touch_bars(n=180, spike_at=None, spike_qv=1e10, base_qv=1e5):
    """价格全程 149.6~150.4——夹在最近线(下 141.42 / 上 151.57)之间,不触任何线=零成交。"""
    t0 = pd.Timestamp('2026-01-01 00:00')
    rows = []
    for i in range(n):
        qv = spike_qv if (spike_at is not None and i == spike_at) else base_qv
        rows.append({'candle_begin_time': t0 + pd.Timedelta(minutes=i),
                     'open': 150.0, 'high': 150.4, 'low': 149.6, 'close': 150.0, 'quote_volume': qv})
    return pd.DataFrame(rows)


def _run(bars, funding_df=None, mode='pv'):
    return simulate_grid_engine(bars, _GP, cap=1000.0, leverage=5.0, stop_cfg=_STOP,
                                funding_df=funding_df, neutral_init=False, active_stop_mode=mode,
                                pv_pnl_thr=_STOP['pv_pnl_thr'], pv_mult=_STOP['pv_mult'],
                                pv_period=_STOP['pv_period'], pv_n=_STOP['pv_n'])


def test_zero_fill_is_actually_zero_fill():
    """前提:该构造确实零成交(否则测的不是零成交路径)。"""
    r = _run(_no_touch_bars())
    assert r['n_trades'] == 0 and r['pnl_ratio'] == 0.0 and r['broke'] is False


def test_zero_fill_pv_spike_exits_pv_not_untouched():
    """零成交 + pv 尖峰 → 'pv主动止损'(对齐实盘 0G),非'未触网'。pnl 恒 0。"""
    r = _run(_no_touch_bars(spike_at=90))
    assert r['n_trades'] == 0 and r['pnl_ratio'] == 0.0
    assert r['exit_reason'] == 'pv主动止损'


def test_zero_fill_no_signal_stays_untouched():
    """零成交 + 无信号 → 仍 '未触网'(保留回测语义)。"""
    r = _run(_no_touch_bars())
    assert r['exit_reason'] == '未触网'


def test_zero_fill_funding_spike_exits_funding():
    """零成交 + 资金费率超阈 → '资金费率止损'。"""
    fund_ts = pd.Timestamp('2026-01-01 01:30')      # 窗内结算,费率 0.002>0.0015
    fd = pd.DataFrame([{'ts': int(fund_ts.value // 1_000_000), 'fundingRate': 0.002}])
    r = _run(_no_touch_bars(n=180), funding_df=fd, mode='pv')
    assert r['n_trades'] == 0
    assert r['exit_reason'] == '资金费率止损'


def test_zero_fill_pv_off_no_false_exit():
    """active_stop_mode='none' 时零成交不因 pv 尖峰误触发(仍'未触网')。"""
    r = _run(_no_touch_bars(spike_at=90), mode='none')
    assert r['exit_reason'] == '未触网'

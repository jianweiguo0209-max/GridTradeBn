"""破网估值:按**被击穿的终止价**,而非破网 bar 的收盘(2026-07-18)。

分歧:
- **实盘**:灾难保险丝是 stop 价触发的 reduce-only **市价单**(grid_executor 开格时即挂),
  价格触到终止价即触发、在触发价附近成交 —— 不会等到那根 bar 收盘。
- **回测(旧)**:`trans_candle_to_tick` 触及终止价即截断 tick 序列,但末行仍按 `row['close']`
  估值。破网 bar 的 close 通常**已从极值回撤** → 涨破(持净空)按更低价估、跌破(持净多)按更高价
  估 —— **两个方向都偏向回测**。

当前四窗回测破网 = 0 笔,故此项独立影响≈0;但 pv 前视修复后(见 test_pv_spike_no_lookahead)
实盘 pv 只响约三成,窄带+长持仓会让破网真的发生 —— 那条路径此前回测一个样本都没有,
故这条不能单独判为无害。
"""
import pandas as pd
import pytest

from gridtrade.core.grid_engine import simulate_grid_engine

_GP = {'low_price': 100.0, 'high_price': 200.0, 'grid_count': 10,
       'stop_low_price': 90.0, 'stop_high_price': 210.0}
_T0 = pd.Timestamp('2026-01-01 10:00:00')


def _run(break_bar_close, high=215.0):
    """entry=150 → 上穿建净空 → 次根冲高 215 击穿 stop_high=210 → 收在 break_bar_close。

    击穿发生在 p3(=high)这一跳,tick 序列在此截断,故 p4(=close)被切掉、**不产生成交**;
    两个 close 取值下的成交序列完全相同,差别只在末行估值 —— 正好隔离出本测试要测的东西。
    """
    bars = pd.DataFrame([
        {'candle_begin_time': _T0, 'open': 150.0, 'high': 152.0, 'low': 149.9, 'close': 152.0},
        {'candle_begin_time': _T0 + pd.Timedelta(minutes=1),
         'open': 152.0, 'high': high, 'low': 152.0, 'close': break_bar_close},
    ])
    return simulate_grid_engine(bars, _GP, cap=1000.0, leverage=5.0, stop_cfg=None,
                                neutral_init=False, active_stop_mode='none')


def test_it_actually_breaks():
    """前提:该构造确实破网(否则后面几条测的是空气)。"""
    r = _run(205.0)
    assert r['broke'] is True and r['exit_reason'] == '破网'


def test_break_valuation_independent_of_break_bar_close():
    """核心不变量:实盘丝在**触发价**成交,故破网盈亏不该取决于那根 bar 最后收在哪里。
    旧口径按 bar 收盘估值 → 同一次破网,收盘回撤越多、回测越好看(此处 205 比 214 好看)。"""
    a = _run(205.0)          # 冲到 215 后大幅回撤收 205
    b = _run(214.0)          # 冲到 215 后几乎没回撤收 214
    assert a['exit_reason'] == b['exit_reason'] == '破网'
    assert a['pnl_ratio'] == pytest.approx(b['pnl_ratio'], rel=1e-9), \
        '破网盈亏随 bar 收盘漂移 → 仍在按收盘估值'


def test_break_valuation_is_worse_than_retraced_close_for_short():
    """方向性:涨破时持净空,按终止价(210)估必然比按回撤后的收盘(205)估**更亏**。
    旧口径正是在这里系统性美化回测。"""
    r = _run(205.0)
    # 按终止价 210 估的净空亏损 > 按 205 估 → pnl 必须低于「收盘估值」那个更好看的数
    # (用 close=210 的同一构造做参照:此时收盘恰等于终止价,新旧口径同值)
    ref_at_stop_px = _run(210.0)
    assert r['pnl_ratio'] == pytest.approx(ref_at_stop_px['pnl_ratio'], rel=1e-9)


def test_downward_break_uses_low_stop_price():
    """跌破用终止最低价估值(对称,别只修一个方向)。"""
    bars = pd.DataFrame([
        {'candle_begin_time': _T0, 'open': 150.0, 'high': 150.1, 'low': 148.0, 'close': 148.0},
        {'candle_begin_time': _T0 + pd.Timedelta(minutes=1),
         'open': 148.0, 'high': 148.0, 'low': 85.0, 'close': 95.0},      # 击穿 stop_low=90
    ])
    bars2 = bars.copy()
    bars2.loc[1, 'close'] = 89.0                                          # 同一次击穿,收盘不同
    kw = dict(cap=1000.0, leverage=5.0, stop_cfg=None, neutral_init=False,
              active_stop_mode='none')
    a = simulate_grid_engine(bars, _GP, **kw)
    b = simulate_grid_engine(bars2, _GP, **kw)
    assert a['exit_reason'] == b['exit_reason'] == '破网'
    assert a['pnl_ratio'] == pytest.approx(b['pnl_ratio'], rel=1e-9)

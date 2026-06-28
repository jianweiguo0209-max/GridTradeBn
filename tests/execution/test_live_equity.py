import numpy as np
import pandas as pd

from gridtrade.core.grid_engine import cal_equity_curve


CAP = 1000.0
FEE = 0.0002
CRATE = 0.0005


def _le(entry=100.0):
    from gridtrade.execution.live_equity import LiveEquity
    return LiveEquity(CAP, fee=FEE, c_rate_taker=CRATE, entry_price=entry)


# 一组确定性成交（分钟, 价, 方向）；价为网格线
FILLS = [(1, 99.0, 'buy'), (2, 98.0, 'buy'), (3, 99.0, 'sell'), (4, 100.0, 'sell')]


def _truth_net_value(fills, entry, final_price):
    """真值：把同一组成交 + 完整逐 bar 路径喂 cal_equity_curve，取末行 net_value。"""
    rows = []
    last = entry
    for ts, p, side in fills:
        rows.append({'candle_begin_time': pd.to_datetime(ts * 60_000, unit='ms'),
                     'last_touch': float(last), 'touch': float(p),
                     'order_dir': 1.0 if side == 'buy' else -1.0, 'order_num': 0.5})
        last = p
    trade_df = pd.DataFrame(rows)
    # 注意：本真值用固定 order_num=0.5，测试里 record_fill 也用 size=0.5
    n = fills[-1][0] + 2
    tbars = pd.date_range(pd.to_datetime(0, unit='ms'), periods=n, freq='1min')
    closes = []
    fmap = {ts: p for ts, p, _ in fills}
    cur = entry
    for i in range(n):
        cur = fmap.get(i, cur)
        closes.append(cur)
    closes[-1] = final_price
    candle = pd.DataFrame({'candle_begin_time': tbars, 'open': closes, 'high': closes,
                           'low': closes, 'close': closes, 'symbol': 'X'})
    eq = cal_equity_curve(candle, trade_df.copy(), FEE, CAP, CRATE, funding_df=None)
    return float(eq['net_value'].iloc[-1])


def test_empty_snapshot_is_unit():
    snap = _le().snapshot(100.0)
    assert snap['net_value'] == 1.0 and snap['pnl_ratio'] == 0.0
    assert snap['net_position'] == 0.0 and snap['realized_pnl'] == 0.0


def test_snapshot_matches_full_path_engine():
    le = _le(entry=100.0)
    for ts, p, side in FILLS:
        le.record_fill(p, side, 0.5, ts * 60_000)
    final_price = 100.5
    snap = le.snapshot(final_price)
    truth = _truth_net_value(FILLS, 100.0, final_price)
    assert abs(snap['net_value'] - truth) < 1e-9, f"{snap['net_value']} vs {truth}"
    # 全平后净持仓应为 0，已实现 = 两个格子收益 = 2 × gap(1.0) × 0.5 = 1.0
    assert abs(snap['net_position']) < 1e-9
    assert abs(snap['realized_pnl'] - 1.0) < 1e-9


def test_open_position_marks_to_mark_price():
    le = _le(entry=100.0)
    le.record_fill(99.0, 'buy', 0.5, 60_000)   # 持多 0.5 @ 99
    snap = le.snapshot(101.0)                   # mark 101
    assert abs(snap['net_position'] - 0.5) < 1e-9
    assert abs(snap['avg_price'] - 99.0) < 1e-9
    truth = _truth_net_value([(1, 99.0, 'buy')], 100.0, 101.0)
    assert abs(snap['net_value'] - truth) < 1e-9


def test_neutral_init_base_inventory():
    # 复刻 OKX 中性网格开网底仓：entry 上方若干格在 entry 价预置多头；随后上涨卖出兑现
    le = _le(entry=100.0)
    for i in range(3):                          # 3 笔底仓买入 @ entry
        le.record_fill(100.0, 'buy', 0.5, (i + 1) * 60_000)
    le.record_fill(101.0, 'sell', 0.5, 5 * 60_000)   # 上方格卖出
    snap = le.snapshot(101.0)
    assert abs(snap['net_position'] - 1.0) < 1e-9   # 1.5 买 - 0.5 卖 = 1.0
    fills = [(1, 100.0, 'buy'), (2, 100.0, 'buy'), (3, 100.0, 'buy'), (5, 101.0, 'sell')]
    truth = _truth_net_value(fills, 100.0, 101.0)
    assert abs(snap['net_value'] - truth) < 1e-9


def test_bad_side_raises():
    import pytest
    with pytest.raises(ValueError):
        _le().record_fill(100.0, 'long', 0.5, 60_000)

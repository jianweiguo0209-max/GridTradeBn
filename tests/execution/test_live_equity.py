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


def test_bad_side_raises():
    import pytest
    with pytest.raises(ValueError):
        _le().record_fill(100.0, 'long', 0.5, 60_000)


def test_add_funding_reduces_net_value():
    le = _le(entry=100.0)
    le.record_fill(99.0, 'buy', 0.5, 60_000)
    before = le.snapshot(101.0)['net_value']
    le.add_funding(5.0)                       # 支付 5 USDT 资金费
    after = le.snapshot(101.0)
    assert abs((before - after['net_value']) - 5.0 / CAP) < 1e-12
    assert after['funding_paid'] == 5.0


def test_replay_matches_incremental():
    fills = [(99.0, 'buy', 0.5, 60_000), (98.0, 'buy', 0.5, 120_000),
             (99.0, 'sell', 0.5, 180_000)]
    inc = _le(entry=100.0)
    for price, side, size, ts in fills:
        inc.record_fill(price, side, size, ts)
    rep = _le(entry=100.0).replay(fills)
    a, b = inc.snapshot(100.0), rep.snapshot(100.0)
    assert abs(a['net_value'] - b['net_value']) < 1e-12
    assert abs(a['net_position'] - b['net_position']) < 1e-12
    assert abs(a['realized_pnl'] - b['realized_pnl']) < 1e-12


def test_snapshot_flip_long_to_short_matches_full_path():
    # net_dir crosses zero (long -> flat -> short); the net_dir-keyed avg-price path
    # is the highest-risk reconstruction case. Must still match the full-path engine.
    le = _le(entry=100.0)
    le.record_fill(100.0, 'buy', 0.5, 60_000)    # net +0.5
    le.record_fill(101.0, 'sell', 0.5, 120_000)  # net 0
    le.record_fill(102.0, 'sell', 0.5, 180_000)  # net -0.5 (flipped short)
    snap = le.snapshot(103.0)
    assert abs(snap['net_position'] - (-0.5)) < 1e-9
    fills = [(1, 100.0, 'buy'), (2, 101.0, 'sell'), (3, 102.0, 'sell')]
    truth = _truth_net_value(fills, 100.0, 103.0)
    assert abs(snap['net_value'] - truth) < 1e-9


def test_snapshot_fee_paid_is_real_sum():
    le = _le(entry=100.0)
    le.record_fill(99.0, 'buy', 0.5, 60_000, fee=0.7)
    le.record_fill(99.0, 'sell', 0.5, 120_000, fee=0.9)
    snap = le.snapshot(100.0)
    assert abs(snap['fee_paid'] - 1.6) < 1e-12      # 0.7 + 0.9


def test_net_value_corrected_to_real_fee():
    fills_geom = [(99.0, 'buy', 0.5, 60_000), (98.0, 'buy', 0.5, 120_000)]
    est = _le(entry=100.0)
    for p, s, sz, ts in fills_geom:
        est.record_fill(p, s, sz, ts)               # fee=None → 估算费率
    est_snap = est.snapshot(100.0)

    real = _le(entry=100.0)
    for p, s, sz, ts in fills_geom:
        real.record_fill(p, s, sz, ts, fee=3.0)     # 每笔真实费 3.0，共 6.0
    real_snap = real.snapshot(100.0)

    assert real_snap['fee_paid'] == 6.0
    # net_value 用真实费替换估算费：real = est + (est_fee - real_fee)/cap
    expected = est_snap['net_value'] + (est_snap['fee_paid'] - 6.0) / CAP
    assert abs(real_snap['net_value'] - expected) < 1e-12
    assert abs(real_snap['pnl_ratio'] - (real_snap['net_value'] - 1.0)) < 1e-12


def test_replay_accepts_fee_tuples():
    fills = [(99.0, 'buy', 0.5, 60_000, 0.4), (98.0, 'buy', 0.5, 120_000, 0.6)]
    rep = _le(entry=100.0).replay(fills)
    assert abs(rep.snapshot(100.0)['fee_paid'] - 1.0) < 1e-12

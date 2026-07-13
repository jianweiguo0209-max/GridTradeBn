# tests/dashboard/test_realized_by_fill.py — 已实现曲线逐笔口径(2026-07-13 用户定)
"""曲线含激活格、按成交时刻入账；已关格在 closed_at 记尾差(funding/收尾费)——
终值与旧'关格时点 total_pnl'口径逐分一致。盈亏数学复用 LiveEquity 直算(单源)。"""
from gridtrade.dashboard.analytics import (realized_curve, realized_curve_by_fill,
                                           realized_fill_events)
from gridtrade.state.fills import FillRepository
from gridtrade.state.records import RecordRepository
from gridtrade.state.models import Fill, Record


def _fill(store, gid, tid, ts, side, price, size, fee=0.0):
    FillRepository(store).add_if_new(Fill(trade_id=tid, grid_id=gid, line_index=1,
                                          side=side, price=price, size=size,
                                          fee=fee, ts=ts))


def test_active_grid_fills_enter_curve_at_fill_time(store):
    # 激活格(无 record):买 1@100 → 卖 1@101,各收费 0.01
    _fill(store, 'gA', 't1', 1_000, 'buy', 100.0, 1.0, fee=0.01)
    _fill(store, 'gA', 't2', 2_000, 'sell', 101.0, 1.0, fee=0.01)
    ev = dict(realized_fill_events(store))
    assert abs(ev[1_000] - (-0.01)) < 1e-9            # 开仓笔:无实现,只扣费
    assert abs(ev[2_000] - (1.0 - 0.01)) < 1e-9       # 平仓笔:+1 价差 − 费
    curve = realized_curve_by_fill(store)
    assert curve[-1][0] == 2_000                       # 按成交时刻,非关格时刻
    assert abs(curve[-1][1] - 0.98) < 1e-9
    assert realized_curve(store) == []                 # 旧口径:激活格不可见(对照)


def test_closed_grid_terminal_matches_records(store):
    # 已关格:逐笔 0.98,record total_pnl=0.90(差 −0.08=funding/收尾)→ 尾差记在 closed_at
    _fill(store, 'gB', 't3', 1_000, 'buy', 100.0, 1.0, fee=0.01)
    _fill(store, 'gB', 't4', 2_000, 'sell', 101.0, 1.0, fee=0.01)
    RecordRepository(store).add(Record(id='rB', exchange='x', symbol='BTC', tag='gt0',
                                       grid_id='gB', total_pnl=0.90, closed_at=5_000))
    curve = realized_curve_by_fill(store)
    assert curve[-1][0] == 5_000
    assert abs(curve[-1][1] - 0.90) < 1e-9             # 终值=旧口径 total_pnl ✓
    old = realized_curve(store)
    assert abs(old[-1][1] - curve[-1][1]) < 1e-9        # 两口径终值一致


def test_window_filter_cum_from_zero(store):
    _fill(store, 'gC', 't5', 1_000, 'buy', 100.0, 1.0)
    _fill(store, 'gC', 't6', 2_000, 'sell', 101.0, 1.0)
    _fill(store, 'gC', 't7', 9_000, 'buy', 100.0, 1.0)
    _fill(store, 'gC', 't8', 9_500, 'sell', 102.0, 1.0)
    w = realized_curve_by_fill(store, start_ms=8_000)
    assert w[0][0] == 9_000 and abs(w[-1][1] - 2.0) < 1e-9   # 窗口内从 0 起累加

# tests/execution/test_fuse_ledger.py
"""丝触发经账本结算(spec 2026-07-08-position-ledger 冲突②):
丝成交按 fuse oid 摄入触发格账本(真实 fee → 计入 record pnl,根治
snapshot-fuse-blind-window 余项);reduce-only clamp 吃掉的只是净仓,
残余份额经 close_share 标准转仓给兄弟 → 全体 Σclaims == 交易所净仓。"""
from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def test_fuse_fire_hedged_settles_through_ledger(store):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0, stop_orders_enabled=True)
    ga = gx.open('fake', BTC, dict(GP), tag='tA')
    gb = gx.open('fake', BTC, dict(GP), tag='tB')
    # 清掉交易所侧网格限价单(只留丝):隔离测试,避免价格穿越时买线成交干扰仓位
    ex._open.clear()
    # 对冲态:A +5 / B −3,交易所净 +2
    gx.live[ga].record_fill(100.0, 'buy', 5.0, 1000)
    gx.live[gb].record_fill(100.0, 'sell', 3.0, 1000)
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, 2.0, 100.0)

    ex.set_price(BTC, 96.5)              # 穿破 stop_low → A 的 sell 丝触发,clamp 到净仓 2
    ga_row = gx.grids.get(ga)
    rec = Reconciler(gx)
    out = rec.reconcile_fuses(ga, BTC)
    assert out['fired'] is True
    assert gx.grids.get(ga).status == 'CLOSED'

    # 丝成交入 A 账本:真实 trade_id(非 ledger:)、line_index=-1、size=2(clamp 后)
    fuse_fills = [f for f in gx.fills.list_by_grid(ga)
                  if f.line_index == -1 and not f.trade_id.startswith('ledger:')]
    assert len(fuse_fills) == 1
    assert abs(fuse_fills[0].size - 2.0) < 1e-9
    assert fuse_fills[0].side == 'sell'

    # 残余 3 转给 B → 双方账本归零 == 交易所净 0(不变量恢复)
    assert abs(ex.fetch_positions(BTC).net_size) < 1e-9
    assert abs(gx.live[ga].net_position) < 1e-9
    assert abs(gx.live[gb].net_position) < 1e-9
    rows_b = [f for f in gx.fills.list_by_grid(gb) if f.trade_id.startswith('ledger:')]
    assert len(rows_b) == 1 and abs(rows_b[0].size - 3.0) < 1e-9

    # record 落库且真因保留;丝成交(96.5×2 的亏损)已计入账本 → pnl 非零
    recs = gx.records.list_by_grid(ga)
    assert len(recs) == 1 and recs[0].exit_reason == '保险丝触发'
    assert recs[0].pnl_ratio != 0.0

    # 兄弟丝原封在挂(v23 语义保留)
    on_book_stops = {s.id for s in ex._stops.get(BTC, [])}
    gb_row = gx.grids.get(gb)
    assert gb_row.fuse_low_oid in on_book_stops
    assert ga_row.fuse_high_oid not in on_book_stops     # 自己的另一张丝已撤

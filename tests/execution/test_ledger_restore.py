# tests/execution/test_ledger_restore.py
"""restore/游标集成(spec 2026-07-08-position-ledger):
合成行(ledger:)持久在 grid_fills → 重启后 restore 重放恢复 claims;
但 max_ts 游标不被合成行推进(否则漏摄入真实成交)。"""
from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.state.models import Fill

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _fake():
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    return ex


def test_restore_replays_ledger_rows_and_cursor_ignores_them(store):
    ex = _fake()
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    ga = gx.open('fake', BTC, dict(GP), tag='tA')
    gb = gx.open('fake', BTC, dict(GP), tag='tB')
    # 对冲态(真实成交落库,与账本一致——restore 的重放源是 grid_fills)
    for gid, side in ((ga, 'buy'), (gb, 'sell')):
        gx.fills.add_if_new(Fill(trade_id='t-%s' % gid, grid_id=gid, line_index=3,
                                 side=side, price=100.0, size=5.0, fee=0.1, ts=1000))
        gx.live[gid].record_fill(100.0, side, 5.0, 1000, 0.1)
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, 0.0, 100.0)

    gx.close(ga, BTC, '周期再平衡')       # 残余 5 转 B(合成行 ts=now_ms >> 1000)
    assert abs(gx.live[gb].net_position) < 1e-9

    # 模拟重启:新执行器 + restore
    gx2 = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    Reconciler(gx2).restore(gb)
    assert abs(gx2.live[gb].net_position) < 1e-9       # 合成行重放 → claims 复原
    assert gx2._trade_cursor[gb] == 1000               # 游标只认真实成交 ts
    assert abs(gx2.ledger.claim(gb)) < 1e-9

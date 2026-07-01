"""回归锁：真中性网格涨破 stop_high 时为净空，high 保险丝(buy reduce-only)须平掉空头，
对账判定已触发后撑网全拆。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.state.models import CLOSED

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 90.0, 'high_price': 110.0, 'grid_count': 10,
      'stop_low_price': 85.0, 'stop_high_price': 115.0}


def test_neutral_top_breakout_high_fuse_covers_short_and_closes(store):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.001, 1e-6, 1e-6, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0,
                      stop_orders_enabled=True, stop_slippage=0.15)
    gid = gx.open('fake', SYM, GP)
    # 涨到网格顶：卖线成交 → 净空
    for p in [102, 105, 108, 110]:
        ex.set_price(SYM, p); gx.sync(gid, SYM)
    assert ex.fetch_positions(SYM).net_size < 0
    # 破 stop_high：high 保险丝(buy reduce-only)触发，把空头平向 0
    ex.set_price(SYM, GP['stop_high_price'] + 0.5)
    assert abs(ex.fetch_positions(SYM).net_size) < 1e-9
    # 对账判定保险丝已触发 → 撑网全拆
    out = Reconciler(gx).reconcile_fuses(gid, SYM)
    assert out['fired'] is True
    assert gx.grids.get(gid).status == CLOSED

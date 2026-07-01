"""test_sync_halt: verify skip_replenish gate in GridExecutor.sync.

When skip_replenish=True:
  - fills are ingested (filled order marked closed, fill recorded)
  - NO new opposite-side order is placed (open order count does not grow)

When skip_replenish=False (default):
  - fills ingested AND opposite order IS placed (open order count stays same,
    because the filled order closes and a new opposite opens).
"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.state.orders import OrderRepository

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store, price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    return ex, gx


def test_sync_skip_replenish_no_new_order(store):
    """With skip_replenish=True: fill ingested but NO new opposite order placed."""
    ex, gx = _setup(store, price=100.0)
    gid = gx.open('fake', SYM, GP)

    # Count open orders before fill
    open_before = len(ex.fetch_open_orders(SYM))

    # Trigger a sell fill (price moves up to cross a sell line)
    ex.set_price(SYM, 100.6)

    # Confirm a fill happened in FakeExchange by checking trades
    # At this point, the filled order is no longer open in the exchange
    open_after_fill_exchange = len(ex.fetch_open_orders(SYM))
    assert open_after_fill_exchange < open_before, "Expected one sell order to fill"

    # sync with skip_replenish=True
    res = gx.sync(gid, SYM, skip_replenish=True)

    # Fill must be ingested
    assert res['new_fills'] == 1, "Fill must be ingested even under halt"

    # Check DB: the filled order should be marked closed in our state
    orders = OrderRepository(store).list_by_grid(gid)
    closed_orders = [o for o in orders if o.status == 'closed']
    assert len(closed_orders) >= 1, "Filled order must be marked closed"

    # No new opposite order was placed: open order count in exchange should be one less
    # (the filled sell closed, no new buy replenish placed)
    open_after_sync = len(ex.fetch_open_orders(SYM))
    assert open_after_sync == open_after_fill_exchange, (
        "skip_replenish=True must NOT place new opposite order; "
        f"expected {open_after_fill_exchange} open orders, got {open_after_sync}"
    )

    # Accounting snapshot still saved
    acc = gx.accounting.get(gid)
    assert acc is not None

    # IMPORTANT: verify that accounting actually reflected the fill under halt
    # The sell fill should have increased realized_pnl or changed net_position.
    # Check that at least one accounting field changed from init (non-zero values indicate fill was processed)
    assert acc.net_position != 0 or acc.realized_pnl != 0 or acc.fee_paid != 0, (
        "Accounting must reflect the fill under skip_replenish=True halt; "
        f"expected net_position/realized_pnl/fee_paid to be non-zero after fill, "
        f"got net_position={acc.net_position}, realized_pnl={acc.realized_pnl}, fee_paid={acc.fee_paid}"
    )


def test_sync_normal_replenishes(store):
    """Without skip_replenish (default=False): when the opposite slot is free, opposite order IS placed."""
    from collections import Counter
    ex, gx = _setup(store, price=100.0)
    gid = gx.open('fake', SYM, GP)

    # 往返走一格制造「对侧空槽」：先成交并 sync 掉最近卖单(line5，清空该线，其配对 buy@4 在→本轮不补)，
    # 再成交最近买单(line4)：其对侧 sell@5 现为空 → 补 sell@5（价>现价→挂住）。
    sell5 = min((o for o in ex.fetch_open_orders(SYM) if o.side == 'sell'), key=lambda o: o.price)
    ex._fill(sell5, sell5.price); del ex._open[sell5.id]
    gx.sync(gid, SYM)
    assert not any(o.line_index == 5 and o.side == 'sell' and o.status == 'open'
                   for o in gx.orders.list_by_grid(gid))   # line5 已空

    buy4 = max((o for o in ex.fetch_open_orders(SYM) if o.side == 'buy'), key=lambda o: o.price)
    ex._fill(buy4, buy4.price); del ex._open[buy4.id]
    res = gx.sync(gid, SYM)  # default: skip_replenish=False

    assert res['new_fills'] == 1
    # 补对侧落地：sell@5 重新挂出；无 (line,side) 重复；store 与交易所挂单一致
    opens = [o for o in gx.orders.list_by_grid(gid) if o.status == 'open']
    assert any(o.line_index == 5 and o.side == 'sell' for o in opens), "must replenish sell@line5"
    assert not [k for k, v in Counter((o.line_index, o.side) for o in opens).items() if v > 1]
    assert len(ex.fetch_open_orders(SYM)) == len(opens)

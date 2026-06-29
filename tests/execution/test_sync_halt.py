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
    """Without skip_replenish (default=False): fill ingested AND opposite order placed."""
    ex, gx = _setup(store, price=100.0)
    gid = gx.open('fake', SYM, GP)

    open_before = len(ex.fetch_open_orders(SYM))
    ex.set_price(SYM, 100.6)
    open_after_fill_exchange = len(ex.fetch_open_orders(SYM))
    assert open_after_fill_exchange < open_before

    # sync with default skip_replenish=False
    res = gx.sync(gid, SYM)  # default: skip_replenish=False

    assert res['new_fills'] == 1

    # Opposite order WAS placed: open count should be back to open_before
    open_after_sync = len(ex.fetch_open_orders(SYM))
    assert open_after_sync == open_before, (
        "skip_replenish=False must place opposite order; "
        f"expected {open_before} open orders, got {open_after_sync}"
    )

from gridtrade.execution.events import EventBus, GridOpened, GridClosed


def test_publish_delivers_to_all_subscribers():
    seen_a, seen_b = [], []
    bus = EventBus()
    bus.subscribe(seen_a.append)
    bus.subscribe(seen_b.append)
    ev = GridOpened(grid_id='g1', exchange='okx', symbol='BTC/USDT:USDT', tag='t0')
    bus.publish(ev)
    assert seen_a == [ev] and seen_b == [ev]


def test_handlers_can_filter_by_event_type():
    closes = []
    bus = EventBus()
    bus.subscribe(lambda e: closes.append(e) if isinstance(e, GridClosed) else None)
    bus.publish(GridOpened(grid_id='g1', exchange='okx', symbol='X', tag='t'))
    bus.publish(GridClosed(grid_id='g1', exchange='okx', symbol='X',
                           reason='固定止损', pnl_ratio=-0.04))
    assert len(closes) == 1 and closes[0].reason == '固定止损'


def test_publish_with_no_subscribers_is_noop():
    EventBus().publish(GridOpened(grid_id='g', exchange='e', symbol='s', tag='t'))

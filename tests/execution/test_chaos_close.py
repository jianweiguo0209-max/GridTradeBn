import random

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.faulty import FaultyAdapter, Partial
from gridtrade.exchanges.resilience import RetryPolicy
from gridtrade.exchanges.resilient_adapter import ResilientAdapter
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.state.store import StateStore

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def build_stack(schedule=None, price=100.0):
    fake = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    fake.set_price(SYM, price)
    faulty = FaultyAdapter(fake, schedule or {})
    resilient = ResilientAdapter(faulty, policy=RetryPolicy(max_attempts=4),
                                 sleep=lambda _: None, rng=random.Random(0))
    store = StateStore.in_memory(); store.create_all()
    gx = GridExecutor(resilient, store, cap=1000.0, leverage=5.0)
    return fake, faulty, gx


def test_close_clean_flattens_position_baseline():
    fake, faulty, gx = build_stack()
    gid = gx.open('fake', SYM, GP)               # 中性底仓 -> 持有多头净仓
    assert fake.fetch_positions(SYM).net_size > 0
    gx.close(gid, SYM, '测试平仓')
    assert gx.grids.get(gid).status == 'CLOSED'
    assert abs(fake.fetch_positions(SYM).net_size) < 1e-9   # 无故障：平干净


def test_close_partial_fill_is_flattened_by_bounded_retry():
    # close() reduce 第一次只成交一半 -> close 必须校残仓并补一笔 reduce 直到平掉
    fake, faulty, gx = build_stack()
    gid = gx.open('fake', SYM, GP)
    net_before = fake.fetch_positions(SYM).net_size
    assert net_before > 0
    faulty._schedule['create_market_order'] = [Partial(0.5)]   # 仅首笔 reduce 吃一半
    gx.close(gid, SYM, '测试平仓')
    assert abs(fake.fetch_positions(SYM).net_size) < 1e-9       # 残仓被补平
    assert gx.grids.get(gid).status == 'CLOSED'

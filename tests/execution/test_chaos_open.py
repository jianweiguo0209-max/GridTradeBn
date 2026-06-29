import random

import ccxt

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.faulty import FaultyAdapter, RaiseAfter
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
    resilient = ResilientAdapter(FaultyAdapter(fake, schedule or {}),
                                 policy=RetryPolicy(max_attempts=4),
                                 sleep=lambda _: None, rng=random.Random(0))
    store = StateStore.in_memory(); store.create_all()
    gx = GridExecutor(resilient, store, cap=1000.0, leverage=5.0)
    return fake, gx


def test_open_baseline_no_faults():
    fake, gx = build_stack()
    gid = gx.open('fake', SYM, GP)
    assert gx.grids.get(gid).status == 'ACTIVE'
    assert len(fake.fetch_open_orders(SYM)) == 9


def test_open_transient_timeout_still_reaches_active():
    # 前两次挂单请求未达交易所即超时 → ResilientAdapter 重试 → 仍开齐
    fake, gx = build_stack({'create_limit_order':
                            [ccxt.RequestTimeout('t'), ccxt.RequestTimeout('t')]})
    gid = gx.open('fake', SYM, GP)
    assert gx.grids.get(gid).status == 'ACTIVE'
    assert len(fake.fetch_open_orders(SYM)) == 9                  # 无缺单
    ids = [o.id for o in fake.fetch_open_orders(SYM)]
    assert len(ids) == len(set(ids))                             # 无重复单


def test_open_lost_ack_no_duplicate_order():
    # 第一笔挂单：内层已建单但 ack 丢失 → 重试再发 → 不得产生第二个挂单
    fake, gx = build_stack({'create_limit_order': [RaiseAfter(ccxt.RequestTimeout('lost-ack'))]})
    gid = gx.open('fake', SYM, GP)
    assert gx.grids.get(gid).status == 'ACTIVE'
    assert len(fake.fetch_open_orders(SYM)) == 9                  # 幂等：仍是 9，不是 10
    ids = [o.id for o in fake.fetch_open_orders(SYM)]
    assert len(ids) == len(set(ids))

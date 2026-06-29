# tests/execution/test_chaos_reconcile.py
import random

import ccxt

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.faulty import FaultyAdapter
from gridtrade.exchanges.resilience import RetryPolicy
from gridtrade.exchanges.resilient_adapter import ResilientAdapter
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
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


def test_reconcile_converges_despite_transient_fault():
    fake, faulty, gx = build_stack()
    gid = gx.open('fake', SYM, GP)
    rec = Reconciler(gx)

    # 缺失：交易所撤掉一个挂单（DB 仍 open）
    victim = fake.fetch_open_orders(SYM)[0]
    fake.cancel_order(SYM, victim.id)
    # 孤儿：交易所多挂一个非本网格意图单
    fake.create_limit_order(SYM, 'buy', 95.0, 0.5, client_oid='zzz:orphan:0')

    # 在补缺失单时注入一次瞬时超时
    faulty._schedule['create_limit_order'] = [ccxt.RequestTimeout('t')]
    out = rec.reconcile_open_orders(gid, SYM)
    assert faulty._schedule.get('create_limit_order', []) == []   # the injected timeout was consumed (retry path exercised)
    assert out == {'canceled': 1, 'replaced': 1}                  # 重试后仍补回 + 撤孤儿
    final_orders = fake.fetch_open_orders(SYM)
    assert all(o.client_oid != 'zzz:orphan:0' for o in final_orders)
    assert len(final_orders) == 9   # 收敛到期望单集（9 grid orders）

    out2 = rec.reconcile_open_orders(gid, SYM)                    # 再对账：幂等
    assert out2 == {'canceled': 0, 'replaced': 0}

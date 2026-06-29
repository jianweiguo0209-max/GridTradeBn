import random

import ccxt
import pytest

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.faulty import FaultyAdapter
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


def _baseline_after_one_fill():
    fake, faulty, gx = build_stack()
    gid = gx.open('fake', SYM, GP)
    fake.set_price(SYM, 100.6)            # 穿越上方一格 -> 成交 -> sync 补对侧
    res = gx.sync(gid, SYM)
    snap = gx.live[gid].snapshot(fake.fetch_price(SYM))
    return res, snap, len(fake.fetch_open_orders(SYM))


def test_replenish_under_timeout_matches_baseline():
    base_res, base_snap, base_open = _baseline_after_one_fill()
    assert base_res['new_fills'] > 0   # baseline must actually fill+replenish, else test is vacuous

    # 干净开仓后，仅在补单阶段注入超时（open 不受影响）
    fake, faulty, gx = build_stack()
    gid = gx.open('fake', SYM, GP)
    faulty._schedule['create_limit_order'] = [ccxt.RequestTimeout('t'),
                                              ccxt.RequestTimeout('t')]
    fake.set_price(SYM, 100.6)
    res = gx.sync(gid, SYM)
    assert faulty._schedule.get('create_limit_order', []) == []   # both injected timeouts were consumed (retry path exercised)
    snap = gx.live[gid].snapshot(fake.fetch_price(SYM))

    assert res['new_fills'] == base_res['new_fills']
    assert len(fake.fetch_open_orders(SYM)) == base_open            # 补单数与基线一致（无多补）
    for k in ('realized_pnl', 'net_position', 'fee_paid', 'avg_price'):
        assert snap[k] == pytest.approx(base_snap[k])              # 记账不漂移


def test_replenish_idempotent_on_resync():
    fake, faulty, gx = build_stack()
    gid = gx.open('fake', SYM, GP)
    fake.set_price(SYM, 100.6)
    gx.sync(gid, SYM)
    open_after_first = len(fake.fetch_open_orders(SYM))
    res2 = gx.sync(gid, SYM)                                       # 二次 sync：无新成交
    assert res2['new_fills'] == 0
    assert len(fake.fetch_open_orders(SYM)) == open_after_first    # 不重复补单

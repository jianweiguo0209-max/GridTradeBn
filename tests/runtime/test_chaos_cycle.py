# tests/runtime/test_chaos_cycle.py
import random

import ccxt
import pytest

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.faulty import FaultyAdapter
from gridtrade.exchanges.resilience import RetryPolicy
from gridtrade.exchanges.resilient_adapter import ResilientAdapter
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.execution.manager import GridManager
from gridtrade.execution.gates import GateChain
from gridtrade.runtime.cycles import run_monitor_cycle
from gridtrade.state.store import StateStore

SYM_A = 'BTC/USDT:USDT'
SYM_B = 'ETH/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP_CFG = {'stop_loss': -0.5, 'take_profit': 1.0, 'trailing': 0.3}


def build():
    insts = [Instrument(SYM_A, 0.1, 0.001, 0.001, 'live', 0),
             Instrument(SYM_B, 0.1, 0.001, 0.001, 'live', 0)]
    fake = FakeExchange(instruments=insts, price=100.0)
    fake.set_price(SYM_A, 100.0); fake.set_price(SYM_B, 100.0)
    faulty = FaultyAdapter(fake, {})
    resilient = ResilientAdapter(faulty, policy=RetryPolicy(max_attempts=2),
                                 sleep=lambda _: None, rng=random.Random(0))
    store = StateStore.in_memory(); store.create_all()
    gx = GridExecutor(resilient, store, cap=1000.0, leverage=5.0)
    mgr = GridManager(gx, GateChain([]), stop_cfg=STOP_CFG)
    return fake, faulty, gx, mgr


def test_one_bad_grid_currently_aborts_whole_cycle():
    fake, faulty, gx, mgr = build()
    gx.open('fake', SYM_A, GP)
    gx.open('fake', SYM_B, GP)
    rec = Reconciler(gx)
    # 对 A 币种的对账注入持续故障：fetch_open_orders 始终维护中 -> 重试耗尽抛
    faulty._schedule['fetch_open_orders'] = [ccxt.OnMaintenance('m')] * 50
    with pytest.raises(ccxt.OnMaintenance):           # 特征化：整轮被掀翻
        run_monitor_cycle(rec, mgr)

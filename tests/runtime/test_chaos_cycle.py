import random

import ccxt

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

SYM_A = 'BTC/USDT:USDT'
SYM_B = 'ETH/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618}


def build(store):
    insts = [Instrument(SYM_A, 0.1, 0.001, 0.001, 'live', 0),
             Instrument(SYM_B, 0.1, 0.001, 0.001, 'live', 0)]
    fake = FakeExchange(instruments=insts, price=100.0)
    fake.set_price(SYM_A, 100.0); fake.set_price(SYM_B, 100.0)
    faulty = FaultyAdapter(fake, {})
    resilient = ResilientAdapter(faulty, policy=RetryPolicy(max_attempts=2),
                                 sleep=lambda _: None, rng=random.Random(0))
    gx = GridExecutor(resilient, store, cap=1000.0, leverage=5.0)
    mgr = GridManager(gx, GateChain([]), stop_cfg=STOP_CFG)
    return fake, faulty, gx, mgr


def test_bad_grid_reconcile_does_not_block_healthy_grid(store):
    # 一个网格 reconcile 持续故障 -> 降级记录，不阻塞另一网格的对账
    fake, faulty, gx, mgr = build(store)
    gid_a = gx.open('fake', SYM_A, GP)
    gid_b = gx.open('fake', SYM_B, GP)
    rec = Reconciler(gx)
    # 恰好耗尽 max_attempts=2：先被处理的网格 reconcile 抛错降级，另一网格 schedule 已空 -> 成功
    faulty._schedule['fetch_open_orders'] = [ccxt.OnMaintenance('m'), ccxt.OnMaintenance('m')]
    out = run_monitor_cycle(rec, mgr)
    assert len(out['degraded']) == 1                       # 仅坏网格降级
    assert len(out['reconciled']) == 1                     # 健康网格仍完成对账
    assert set(out['degraded']) | set(out['reconciled']) == {gid_a, gid_b}


def test_bad_grid_monitor_does_not_block_healthy_grid(store):
    # monitor_all 段同样隔离：一个网格 sync 故障 -> 记错降级，另一网格仍被 monitor
    fake, faulty, gx, mgr = build(store)
    gx.open('fake', SYM_A, GP)
    gx.open('fake', SYM_B, GP)
    rec = Reconciler(gx)
    # fetch_my_trades 仅 sync(monitor_all) 用、reconcile 不用 -> 隔离 monitor_all 段
    faulty._schedule['fetch_my_trades'] = [ccxt.OnMaintenance('m'), ccxt.OnMaintenance('m')]
    out = run_monitor_cycle(rec, mgr)
    assert out['degraded'] == {}                           # reconcile 段不受影响
    errored = [r for r in out['monitored'] if 'error' in r]
    ok = [r for r in out['monitored'] if 'error' not in r]
    assert len(errored) == 1 and len(ok) == 1              # 一坏一好

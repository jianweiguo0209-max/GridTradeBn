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


def test_read_fault_snapshot_skips_round_then_recovers(store):
    # 语义校准（账户级快照后）：读路径=每轮 1 次账户级调用，故障 blast radius=一轮
    # 整体跳过（不带残缺数据跑单元；HL 原生实现单端点本就原子），故障消退后下一轮
    # 全格恢复。故障注入按 _all 方法名（FaultyAdapter 按名拦截，账户级读不再逐 symbol）。
    fake, faulty, gx, mgr = build(store)
    gid_a = gx.open('fake', SYM_A, GP)
    gid_b = gx.open('fake', SYM_B, GP)
    rec = Reconciler(gx)
    faulty._schedule['fetch_my_trades_all'] = [ccxt.OnMaintenance('m'), ccxt.OnMaintenance('m')]
    logs = []
    out1 = run_monitor_cycle(rec, mgr, log=logs.append)
    assert out1['monitored'] == []                         # 整轮跳过
    assert any('snapshot failed' in s for s in logs)
    out2 = run_monitor_cycle(rec, mgr)                     # 故障耗尽 → 下一轮全格恢复
    assert set(out2['reconciled']) == {gid_a, gid_b}


def test_bad_grid_write_does_not_block_healthy_grid(store):
    # 写路径仍逐格隔离：SYM_A 的重挂写被持续拒 → 仅该格 degraded，SYM_B 照常对账。
    # 触发方式：丢单 + E2 宽限（2 轮）到期重挂 → create_limit_order 故障恰好耗尽重试。
    fake, faulty, gx, mgr = build(store)
    gid_a = gx.open('fake', SYM_A, GP)
    gid_b = gx.open('fake', SYM_B, GP)
    rec = Reconciler(gx)
    sell_a = [o for o in fake.fetch_open_orders(SYM_A) if o.side == 'sell'][0]
    fake._open.pop(sell_a.id, None)                        # A 丢一张卖单（成交不可见）
    run_monitor_cycle(rec, mgr)                            # 宽限第 1 轮：不重挂
    faulty._schedule['create_limit_order'] = [ccxt.OnMaintenance('m'), ccxt.OnMaintenance('m')]
    out = run_monitor_cycle(rec, mgr)                      # 第 2 轮：重挂 → 写故障耗尽
    assert gid_a in out['degraded']                        # 坏格 reconcile 阶段降级
    assert gid_b in out['reconciled'] and gid_a not in out['reconciled']

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


def build_stack(store, schedule=None, price=100.0):
    fake = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    fake.set_price(SYM, price)
    faulty = FaultyAdapter(fake, schedule or {})
    resilient = ResilientAdapter(faulty, policy=RetryPolicy(max_attempts=4),
                                 sleep=lambda _: None, rng=random.Random(0))
    gx = GridExecutor(resilient, store, cap=1000.0, leverage=5.0)
    return fake, faulty, gx


def _arm_pending_replenish(fake, gx, gid):
    # 走到「下一次 sync 会补一张挂得住的对侧单」的状态：往返走一格。
    # 先成交并 sync 掉最近卖单(line5，配对 buy@4 在→不补)，再成交最近买单(line4)但不 sync
    # → 下次 sync 会补 sell@5（价>现价→挂住，触发一次 create_limit_order）。
    sell5 = min((o for o in fake.fetch_open_orders(SYM) if o.side == 'sell'), key=lambda o: o.price)
    fake._fill(sell5, sell5.price); del fake._open[sell5.id]
    gx.sync(gid, SYM)
    buy4 = max((o for o in fake.fetch_open_orders(SYM) if o.side == 'buy'), key=lambda o: o.price)
    fake._fill(buy4, buy4.price); del fake._open[buy4.id]


def _baseline_after_one_fill():
    # baseline uses its own isolated in-memory store (reference values only; not persisted)
    _store = StateStore.in_memory(); _store.create_all()
    fake, faulty, gx = build_stack(_store)
    gid = gx.open('fake', SYM, GP)
    _arm_pending_replenish(fake, gx, gid)
    res = gx.sync(gid, SYM)              # 这次 sync 补 sell@5
    snap = gx.live[gid].snapshot(fake.fetch_price(SYM))
    return res, snap, len(fake.fetch_open_orders(SYM))


def test_replenish_under_timeout_matches_baseline(store):
    base_res, base_snap, base_open = _baseline_after_one_fill()
    assert base_res['new_fills'] > 0   # baseline must actually fill+replenish, else test is vacuous

    # 干净开仓后，仅在补单阶段注入超时（open 不受影响）
    fake, faulty, gx = build_stack(store)
    gid = gx.open('fake', SYM, GP)
    _arm_pending_replenish(fake, gx, gid)     # 走到「下次 sync 补 sell@5」的状态
    faulty._schedule['create_limit_order'] = [ccxt.RequestTimeout('t'),
                                              ccxt.RequestTimeout('t')]
    res = gx.sync(gid, SYM)                    # 补 sell@5 时穿过两次超时
    assert faulty._schedule.get('create_limit_order', []) == []   # both injected timeouts were consumed (retry path exercised)
    snap = gx.live[gid].snapshot(fake.fetch_price(SYM))

    assert res['new_fills'] == base_res['new_fills']
    assert len(fake.fetch_open_orders(SYM)) == base_open            # 补单数与基线一致（无多补）
    for k in ('realized_pnl', 'net_position', 'fee_paid', 'avg_price'):
        assert snap[k] == pytest.approx(base_snap[k])              # 记账不漂移


def test_replenish_idempotent_on_resync(store):
    fake, faulty, gx = build_stack(store)
    gid = gx.open('fake', SYM, GP)
    fake.set_price(SYM, 100.6)
    gx.sync(gid, SYM)
    open_after_first = len(fake.fetch_open_orders(SYM))
    res2 = gx.sync(gid, SYM)                                       # 二次 sync：无新成交
    assert res2['new_fills'] == 0
    assert len(fake.fetch_open_orders(SYM)) == open_after_first    # 不重复补单


def test_replenish_invalid_order_error_carries_order_params(store):
    # 可观测性：补单被交易所拒（如 HL min $10）时，异常须携带实际下单参数
    # （side/价/量/名义额）——线上只有异常字符串可见，缺参数则根因不可查
    # （2026-07-05 VVV $26 合法补单被拒 $10 之谜的排查缺口）。
    fake, faulty, gx = build_stack(store)
    gid = gx.open('fake', SYM, GP)
    _arm_pending_replenish(fake, gx, gid)          # 下次 sync 将补 sell@line5
    faulty._schedule['create_limit_order'] = [
        ccxt.InvalidOrder('Order must have minimum value of $10.')]
    with pytest.raises(ccxt.InvalidOrder) as ei:
        gx.sync(gid, SYM)
    msg = str(ei.value)
    assert 'replenish' in msg and SYM in msg       # 哪个网格动作、哪个币
    assert 'sell' in msg                           # 方向
    assert 'notional=' in msg and 'px=' in msg and 'sz=' in msg   # 价/量/名义额全带
    assert 'minimum value' in msg                  # 原始交易所错误保留

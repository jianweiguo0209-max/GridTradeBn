from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _new_executor(ex, store):
    from gridtrade.execution.grid_executor import GridExecutor
    return GridExecutor(ex, store, cap=1000.0, leverage=5.0)


def test_restore_rebuilds_state_matching_pre_restart(store):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 100.6); gx.sync(gid, SYM)
    snap_before = gx.live[gid].snapshot(ex.fetch_price(SYM))

    # 模拟重启：全新 executor（空内存），共享同一 store/exchange
    gx2 = _new_executor(ex, store)
    assert gid not in gx2.live
    from gridtrade.execution.reconciler import Reconciler
    Reconciler(gx2).restore(gid)
    # 重建后内存态可用且与重启前一致
    assert gid in gx2.live and gid in gx2._geom
    snap_after = gx2.live[gid].snapshot(ex.fetch_price(SYM))
    assert abs(snap_before['net_position'] - snap_after['net_position']) < 1e-9
    assert abs(snap_before['realized_pnl'] - snap_after['realized_pnl']) < 1e-9


def test_restore_then_sync_no_double_replenish(store):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 100.6); gx.sync(gid, SYM)
    open_before = len(ex.fetch_open_orders(SYM))

    gx2 = _new_executor(ex, store)
    from gridtrade.execution.reconciler import Reconciler
    Reconciler(gx2).restore(gid)
    res = gx2.sync(gid, SYM)             # 重启后 sync：历史成交已在 grid_fills → 不重复摄入
    assert res['new_fills'] == 0
    assert len(ex.fetch_open_orders(SYM)) == open_before


def test_reconcile_cancels_orphan_and_replaces_missing(store):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)
    from gridtrade.execution.reconciler import Reconciler
    rec = Reconciler(gx)

    # 干净状态：无孤儿无缺失
    out0 = rec.reconcile_open_orders(gid, SYM)
    assert out0 == {'canceled': 0, 'replaced': 0}

    # 制造缺失：直接在交易所撤掉一个挂单（DB 仍记 open）
    victim = ex.fetch_open_orders(SYM)[0]
    ex.cancel_order(SYM, victim.id)
    assert len(ex.fetch_open_orders(SYM)) == 8
    out1 = rec.reconcile_open_orders(gid, SYM)
    assert out1['replaced'] == 1 and out1['canceled'] == 0
    assert len(ex.fetch_open_orders(SYM)) == 9       # 已补回

    # 制造孤儿：交易所多挂一个不属于本网格意图的单
    ex.create_limit_order(SYM, 'buy', 95.0, 0.5, client_oid='zzz:orphan:0')
    out2 = rec.reconcile_open_orders(gid, SYM)
    assert out2['canceled'] == 1
    assert all(o.client_oid != 'zzz:orphan:0' for o in ex.fetch_open_orders(SYM))


def test_restore_preserves_funding_without_refetching_full_history(store):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 100.6)
    ex.seed_funding_payments(SYM, [(10_000, 2.0)])   # paid 2.0 before restart
    gx.sync(gid, SYM)
    funding_before = gx.accounting.get(gid).funding_paid
    assert abs(funding_before - 2.0) < 1e-9

    # restart: fresh executor, restore from durable state
    gx2 = _new_executor(ex, store)
    from gridtrade.execution.reconciler import Reconciler
    Reconciler(gx2).restore(gid)
    assert abs(gx2.live[gid].funding_paid - 2.0) < 1e-9          # recovered, not lost
    # simulate the exchange having dropped the old payment from its window (page-limited):
    ex.seed_funding_payments(SYM, [])                            # no payments returned now
    gx2.sync(gid, SYM)
    # funding must NOT reset to 0 just because the exchange no longer returns the old payment
    assert abs(gx2.accounting.get(gid).funding_paid - 2.0) < 1e-9

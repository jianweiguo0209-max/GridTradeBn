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
    from gridtrade.state.grids import GridRepository
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 100.6)
    opened = GridRepository(store).get(gid).created_at
    ex.seed_funding_payments(SYM, [(opened + 1, 2.0)])   # paid 2.0 after open, before restart
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


def test_restore_before_first_sync_excludes_pre_open_funding(store):
    # 开仓后、首次 sync 前重启：restore 不得把资金费游标退回 0（acc.funding_cursor 仍是 0），
    # 否则随后的 sync 又会把开仓前的历史 funding 计入本网格。
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)          # 开仓即 init（funding_cursor=0），尚未 sync

    gx2 = _new_executor(ex, store)          # 重启：全新 executor
    from gridtrade.execution.reconciler import Reconciler
    Reconciler(gx2).restore(gid)

    ex.seed_funding_payments(SYM, [(1, 5.0)])   # ts=1：远早于开仓的历史 funding
    gx2.sync(gid, SYM)
    assert gx2.accounting.get(gid).funding_paid == 0.0


def test_check_position_drift_flags_divergence_but_does_not_change_position(store):
    # 净仓对账（防御纵深）：模型净仓与真实持仓偏离超容差 → ok=False 告警；只读、不改仓。
    from gridtrade.execution.reconciler import Reconciler
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)
    gx.sync(gid, SYM)
    rec = Reconciler(gx)
    # 开仓后模型与交易所一致 → 无背离
    assert rec.check_position_drift(gid, SYM)['ok'] is True
    # 人为制造背离：模型净仓 +5×每格量（远超容差 1.5×每格量）
    order_num = gx._geom[gid]['order_num']
    bump = 5 * order_num
    acc = gx.accounting.get(gid)
    real_before = ex.fetch_positions(SYM).net_size
    acc.net_position = acc.net_position + bump
    gx.accounting.save(acc)
    d = rec.check_position_drift(gid, SYM)
    assert d['ok'] is False
    assert abs(d['drift'] - bump) < 1e-9
    assert abs(d['exchange'] - real_before) < 1e-9      # 只读：交易所持仓未被改动
    assert ex.fetch_positions(SYM).net_size == real_before

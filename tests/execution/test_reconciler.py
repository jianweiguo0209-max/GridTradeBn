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
    rec = Reconciler(gx, replace_grace=1)   # 本测试聚焦孤儿撤+缺失补，用即时重挂（grace=1）

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


class _FlakyFetch:
    """包装 FakeExchange：模拟 HL 抖动——fetch_open_orders 少返回一张【仍在挂】的单；
    记录 cancel_order 调用。其余调用透传。"""
    def __init__(self, inner, hide_oid):
        self._inner = inner
        self._hide = hide_oid
        self.cancels = []

    def fetch_open_orders(self, symbol):
        return [o for o in self._inner.fetch_open_orders(symbol) if o.id != self._hide]

    def cancel_order(self, symbol, order_id):
        self.cancels.append(order_id)
        return self._inner.cancel_order(symbol, order_id)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_reconcile_blindspot_open_order_left_alone_no_duplicate(store):
    # HL 抖动：fetch_open_orders 偶尔少返回一张【仍在挂】的单。三态升级(spec 2026-07-09)后
    # 语义校准：重挂前问 order_status 权威——'open'=信息面盲区 → 一手不动(替代旧的
    # 撤旧重挂)。不变量相同且更强：无重复单、旧单原封、其后续成交仍可按 oid 摄入。
    from gridtrade.execution.reconciler import Reconciler
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)
    victim = ex.fetch_open_orders(SYM)[0]          # 一张仍在挂的真实单
    before = {o.id for o in ex.fetch_open_orders(SYM)}

    gx.adapter = _FlakyFetch(ex, hide_oid=victim.id)   # 让对账时这张单“消失”（实际还挂着）
    out = Reconciler(gx, replace_grace=1).reconcile_open_orders(gid, SYM)

    after = {o.id for o in ex.fetch_open_orders(SYM)}
    assert out['replaced'] == 0                         # 权威在挂 → 不重挂
    assert victim.id in after                           # 旧单原封不动
    assert after == before                              # 无重复单、零折腾


def test_reconcile_grace_delays_replace_until_consecutive_missing(store):
    # E2：一张 open 单从挂单簿消失（成交但成交尚不可见时也是这样）→ 宽限期内本轮先不重挂、
    # 不覆盖 oid（给 sync 时间摄入成交）；连续 missing 达到 grace 才重挂。
    from gridtrade.execution.reconciler import Reconciler
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)
    rec = Reconciler(gx, replace_grace=2)
    victim = ex.fetch_open_orders(SYM)[0]
    old_oid = victim.id
    ex.cancel_order(SYM, victim.id)                    # 从交易所撤掉（DB 仍 open）= "从 book 消失"
    out1 = rec.reconcile_open_orders(gid, SYM)         # 第 1 轮：宽限，不重挂
    assert out1['replaced'] == 0
    assert gx.orders.get(victim.client_oid).exchange_order_id == old_oid   # oid 未被覆盖
    out2 = rec.reconcile_open_orders(gid, SYM)         # 第 2 轮：仍 missing → 达 grace → 重挂
    assert out2['replaced'] == 1
    assert gx.orders.get(victim.client_oid).exchange_order_id != old_oid   # 此时才换新 oid


def test_reconcile_grace_resets_when_order_reingested(store):
    # 宽限计数应在该单不再 missing 时清零：成交被 sync 标 closed → 移出 expected → 不再重挂。
    from gridtrade.execution.reconciler import Reconciler
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)
    rec = Reconciler(gx, replace_grace=2)
    victim = ex.fetch_open_orders(SYM)[0]
    ex.cancel_order(SYM, victim.id)
    assert rec.reconcile_open_orders(gid, SYM)['replaced'] == 0   # 第1轮宽限
    gx.orders.upsert(__import__('gridtrade.state.models', fromlist=['GridOrder']).GridOrder(
        client_oid=victim.client_oid, grid_id=gid, line_index=0, side=victim.side,
        price=victim.price, size=victim.size, status='closed'))   # 模拟 sync 标 closed
    # 该单已不在 expected(open) → 第2轮不应重挂它
    assert rec.reconcile_open_orders(gid, SYM)['replaced'] == 0


def test_restore_rebuilds_real_fee_from_persisted_fills(store):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 100.6); gx.sync(gid, SYM)        # 一笔成交，真实费落库（Task 3）
    fee_before = gx.live[gid].snapshot(ex.fetch_price(SYM))['fee_paid']
    assert fee_before > 0.0

    # 模拟重启：全新 executor，从持久化 grid_fills 重放
    gx2 = _new_executor(ex, store)
    from gridtrade.execution.reconciler import Reconciler
    Reconciler(gx2).restore(gid)
    fee_after = gx2.live[gid].snapshot(ex.fetch_price(SYM))['fee_paid']
    assert abs(fee_after - fee_before) < 1e-9          # 重放自持久化 fee，不丢、与运行态一致


def test_restore_uses_persisted_grid_cap_not_executor_default(store):
    # mainnet 2026-07-06 实证 bug：动态 cap 开的网格（cap≠executor 默认），重启 restore 用
    # ex.cap 重算 → order_num 缩小(302/100≈3×) → 补单 1/3 量($8.63<$10 被拒/静默小单)，
    # LiveEquity cap 错 → pnl_ratio 虚大 3× → 止损/止盈 3 倍提前触发。
    # 修复口径：geom.order_num 用网格行持久化的 order_num；LiveEquity 用网格行 cap。
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = _new_executor(ex, store)                      # executor 默认 cap=1000
    gid = gx.open('fake', SYM, GP, cap=300.0)          # 实际开仓 cap=300（模拟动态 cap）
    opened_order_num = gx._geom[gid]['order_num']
    g = gx.grids.get(gid)
    assert abs(g.order_num - opened_order_num) < 1e-9  # 前提：开仓已持久化真值

    gx2 = _new_executor(ex, store)                     # 重启：默认 cap=1000 的新 executor
    from gridtrade.execution.reconciler import Reconciler
    Reconciler(gx2).restore(gid)
    # 差分 load-bearing：旧逻辑用 ex.cap=1000 重算 → order_num 放大 1000/300≈3.3×，必红
    assert abs(gx2._geom[gid]['order_num'] - opened_order_num) < 1e-9
    assert abs(gx2.live[gid].cap - 300.0) < 1e-9       # pnl_ratio 分母 = 真实开仓 cap

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.state.models import ACTIVE


SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store, price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    from gridtrade.execution.grid_executor import GridExecutor
    ex_ = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    return ex, store, ex_


def test_open_starts_flat(store):
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP, offset=0, tag='t0')
    from gridtrade.state.grids import GridRepository
    g = GridRepository(store).get(gid)
    assert g.status == ACTIVE and g.entry_price == 100.0
    # 真中性：开网即 flat，无初始市价单
    assert abs(ex.fetch_positions(SYM).net_size) < 1e-9
    # 9 条线，entry 不在线上 → 9 个挂单（4 sell / 5 buy）
    opens = ex.fetch_open_orders(SYM)
    assert len(opens) == 9
    sells = [o for o in opens if o.side == 'sell']
    buys = [o for o in opens if o.side == 'buy']
    assert len(sells) == 4 and len(buys) == 5
    # 无 :init: 市价成交
    assert all(':init:' not in t.client_oid for t in ex.fetch_my_trades(SYM))


def test_neutral_net_follows_price_short_above_long_below(store):
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    ex.set_price(SYM, 102.5); gx.sync(gid, SYM)   # 穿所有卖线 → 净空
    assert ex.fetch_positions(SYM).net_size < 0
    ex.set_price(SYM, 97.5); gx.sync(gid, SYM)    # 穿所有买线 → 净多
    assert ex.fetch_positions(SYM).net_size > 0


def test_open_persists_orders_with_client_oid(store):
    ex, store, gx = _setup(store)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    from gridtrade.state.orders import OrderRepository
    rows = OrderRepository(store).list_by_grid(gid)
    assert len(rows) == 9
    assert all(r.client_oid.startswith(f'{gid}:') for r in rows)
    assert all(r.status == 'open' for r in rows)


def test_open_undercapitalized_raises(store):
    import pytest
    ex, store, _ = _setup(store)
    from gridtrade.execution.grid_executor import GridExecutor
    # min_amount 极大 → 每格量被向下取整到 0 → grid_order_info 返回 None → 建网失败
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0, min_amount=1e9)
    with pytest.raises(RuntimeError):
        gx.open(ex_exchange_name(), SYM, GP)


def test_open_uses_dynamic_cap_from_equity(store):
    from gridtrade.exchanges.base import Balance
    from gridtrade.execution.grid_executor import GridExecutor
    from gridtrade.state.grids import GridRepository
    ex, store, _ = _setup(store, price=100.0)
    ex.fetch_balance = lambda: Balance(equity=500.0, cash=500.0)
    # cap_equity_frac 启用 → cap 按当前权益动态定：500 × 0.10 = 50（非固定 100）
    gx = GridExecutor(ex, store, cap=100.0, leverage=5.0,
                      cap_equity_frac=0.10, cap_min=20.0, cap_max=100000.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    assert abs(GridRepository(store).get(gid).cap - 50.0) < 1e-9


def test_open_dynamic_cap_off_by_default_uses_fixed_cap(store):
    from gridtrade.exchanges.base import Balance
    from gridtrade.execution.grid_executor import GridExecutor
    from gridtrade.state.grids import GridRepository
    ex, store, _ = _setup(store, price=100.0)
    ex.fetch_balance = lambda: Balance(equity=500.0, cash=500.0)
    gx = GridExecutor(ex, store, cap=100.0, leverage=5.0)   # 未启用 → 固定 cap
    gid = gx.open(ex_exchange_name(), SYM, GP)
    assert abs(GridRepository(store).get(gid).cap - 100.0) < 1e-9


def test_sync_records_fill_partner_present_no_over_replenish(store):
    from collections import Counter
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    before_open = len(ex.fetch_open_orders(SYM))
    ex.set_price(SYM, 100.6)   # 触发 line 5 卖单成交（100.4812）
    res = gx.sync(gid, SYM)
    assert res['new_fills'] == 1
    # 配对层级：sell@5 的配对 buy@4 本就 resting → 不重复补对侧；挂单数减一、无 (line,side) 重复。
    # （补对侧「挂得住」的正向场景见 test_sync_normal_replenishes 的往返走格）
    assert len(ex.fetch_open_orders(SYM)) == before_open - 1
    opens = [o for o in gx.orders.list_by_grid(gid) if o.status == 'open']
    assert not [k for k, v in Counter((o.line_index, o.side) for o in opens).items() if v > 1]
    # 真中性：开网 flat，一笔卖单成交 → 净空一格量（-on）
    from gridtrade.state.grids import GridRepository
    on = GridRepository(store).get(gid).order_num
    assert abs(ex.fetch_positions(SYM).net_size - (-on)) < 1e-6
    # accounting 落了快照
    acc = gx.accounting.get(gid)
    assert acc is not None and abs(acc.net_position - (-on)) < 1e-6
    # LiveEquity snapshot net_position must match the real exchange position
    assert abs(gx.live[gid].snapshot(ex.fetch_price(SYM))['net_position'] - ex.fetch_positions(SYM).net_size) < 1e-6


def test_sync_funding_payments_accumulate(store):
    from gridtrade.state.grids import GridRepository
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    opened = GridRepository(store).get(gid).created_at
    ex.seed_funding_payments(SYM, [(opened + 1, 1.0)])   # 开仓后结算：支付 1 USDT
    gx.sync(gid, SYM)
    acc = gx.accounting.get(gid)
    assert abs(acc.funding_paid - 1.0) < 1e-9


def test_open_excludes_pre_open_funding(store):
    # 开仓前结算的 funding 不得计入本网格（游标从开仓时刻起算，而非 0）。
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    ex.seed_funding_payments(SYM, [(1, 1.0)])   # ts=1：远早于开仓
    gx.sync(gid, SYM)
    acc = gx.accounting.get(gid)
    assert acc.funding_paid == 0.0


def test_sync_idempotent_no_new_fills(store):
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    ex.set_price(SYM, 100.6)
    gx.sync(gid, SYM)
    res2 = gx.sync(gid, SYM)   # 第二次无新成交
    assert res2['new_fills'] == 0


def test_sync_funding_payments_idempotent_across_calls(store):
    from gridtrade.state.grids import GridRepository
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    opened = GridRepository(store).get(gid).created_at
    ex.seed_funding_payments(SYM, [(opened + 1, 1.0)])   # 开仓后结算：支付 1 USDT
    gx.sync(gid, SYM)
    first = gx.accounting.get(gid).funding_paid
    gx.sync(gid, SYM)                                 # 第二次：无新资金费流水
    second = gx.accounting.get(gid).funding_paid
    assert abs(first - 1.0) < 1e-9
    assert abs(second - 1.0) < 1e-9, f"funding double-counted: {second}"


def test_close_cancels_orders_flattens_and_records(store):
    from gridtrade.state.models import CLOSED
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    out = gx.close(gid, SYM, '固定止损')
    assert out['reason'] == '固定止损'
    # 所有挂单已撤
    assert ex.fetch_open_orders(SYM) == []
    # 净仓已平
    assert abs(ex.fetch_positions(SYM).net_size) < 1e-9
    # 网格 CLOSED，槽位释放
    from gridtrade.state.grids import GridRepository
    assert GridRepository(store).get(gid).status == CLOSED
    assert GridRepository(store).get_active_by_symbol('fake', SYM) is None
    # 留下一条 record
    recs = gx.records.list_by_grid(gid)
    assert len(recs) == 1 and recs[0].exit_reason == '固定止损'


def test_close_record_money_uses_grid_cap_not_executor_default(store):
    # 差分：动态 cap 网格（grid.cap=50）在默认 cap=100 的 executor 上开→关，
    # record 的 sz/total_pnl 必须按 50 计——pnl_ratio 分母是 LiveEquity.cap==grid.cap，
    # 乘 executor 静态 cap 会整体错标（mainnet 2026-07-06 实证：低报 cap真/cap默认 倍）。
    from gridtrade.exchanges.base import Balance
    from gridtrade.execution.grid_executor import GridExecutor
    from gridtrade.state.grids import GridRepository
    ex, store, _ = _setup(store, price=100.0)
    ex.fetch_balance = lambda: Balance(equity=500.0, cash=500.0)
    gx = GridExecutor(ex, store, cap=100.0, leverage=5.0,
                      cap_equity_frac=0.10, cap_min=20.0, cap_max=100000.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    assert abs(GridRepository(store).get(gid).cap - 50.0) < 1e-9   # 前提：动态 cap 生效
    ex.set_price(SYM, 100.6)    # 触发一笔卖单成交 → pnl_ratio 非零
    gx.sync(gid, SYM)
    gx.close(gid, SYM, '手动停止')
    rec = gx.records.list_by_grid(gid)[0]
    assert rec.pnl_ratio != 0.0
    assert abs(rec.sz - 50.0) < 1e-9
    assert abs(rec.total_pnl - rec.pnl_ratio * 50.0) < 1e-12


def test_resume_restores_original_close_reason(store):
    # close() 已落真因但中途死掉（停在 CLOSING）→ 续平落 record 须还原真因：
    # '周期再平衡(续平)' 而非裸 '平仓恢复'（恢复动作≠触发原因，裸写盖真因）。
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    g = gx.grids.get(gid)
    gx.grids.set_close_reason(gid, '周期再平衡')
    gx.grids.transition_status(gid, 'CLOSING', expected_version=g.version)
    gx.finalize_close(gid, SYM, '平仓恢复')
    rec = gx.records.list_by_grid(gid)[0]
    assert rec.exit_reason == '周期再平衡(续平)'


def test_resume_without_stored_reason_falls_back(store):
    # 遗留场景（CLOSING 但无 close_reason，如外部直接转态）→ 保持 '平仓恢复' 兜底。
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    g = gx.grids.get(gid)
    gx.grids.transition_status(gid, 'CLOSING', expected_version=g.version)
    gx.finalize_close(gid, SYM, '平仓恢复')
    assert gx.records.list_by_grid(gid)[0].exit_reason == '平仓恢复'


def test_close_persists_reason_and_record_unchanged(store):
    # 正常关格：close() 把真因落 grids.close_reason；record 原因不变。
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    gx.close(gid, SYM, '固定止损')
    assert gx.grids.get(gid).close_reason == '固定止损'
    assert gx.records.list_by_grid(gid)[0].exit_reason == '固定止损'


def test_close_emits_structured_log_line(store, capsys):
    # 平仓可观测性：关格必须打一行 [close] 结构化日志（止损/PV/轮换全路径共用此点）。
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    gx.close(gid, SYM, '固定止损')
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if l.startswith('[close] grid %s' % gid)]
    assert line and 'reason=固定止损' in line[0] and 'pnl_ratio=' in line[0]


def test_close_then_reopen_same_symbol_ok(store):
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    gx.close(gid, SYM, '手动停止')
    gid2 = gx.open(ex_exchange_name(), SYM, GP)   # 槽位已释放，可再开
    assert gid2 != gid


def test_sync_refetches_late_visible_trade_below_cursor(store):
    # E4：一笔成交"晚可见"、其 ts 低于当前游标（被别的已摄入成交推高的 max_ts）→
    # 游标=max_ts 会把它跳过永久漏；游标留重叠应重新拉到并入账。
    from gridtrade.exchanges.base import Trade
    ex, store, gx = _setup(store, 100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    sells = [o for o in ex.fetch_open_orders(SYM) if o.side == 'sell']
    a, b = sells[0], sells[1]
    ex._trades.append(Trade(id='hi', client_oid=a.client_oid, symbol=SYM, side='sell',
                            price=a.price, size=a.size, fee=0.0, ts=100, order_id=a.id))
    gx.sync(gid, SYM)                              # 摄入高 ts → max_ts=100
    n1 = len(gx.fills.list_by_grid(gid))
    assert any(f.trade_id == 'hi' for f in gx.fills.list_by_grid(gid))
    ex._trades.append(Trade(id='lo', client_oid=b.client_oid, symbol=SYM, side='sell',
                            price=b.price, size=b.size, fee=0.0, ts=50, order_id=b.id))
    gx.sync(gid, SYM)                              # ts=50 < max_ts=100：晚到成交
    n2 = len(gx.fills.list_by_grid(gid))
    assert n2 == n1 + 1, "ts<游标的晚到成交应被游标重叠重新拉到并入账"
    assert any(f.trade_id == 'lo' for f in gx.fills.list_by_grid(gid))


def test_sync_wires_real_fee_into_persistence_and_accounting(store):
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    fee_after_open = gx.live[gid].real_fee_paid       # 真中性：开网 flat，无底仓费 → 0
    ex.set_price(SYM, 100.6)                           # 触发 line 5 卖单成交
    res = gx.sync(gid, SYM)
    assert res['new_fills'] == 1

    f = gx.fills.list_by_grid(gid)[0]
    real_fill_fee = f.size * f.price * 0.0005          # FakeExchange 费率 0.0005
    # (a) 落库真实费
    assert abs(f.fee - real_fill_fee) < 1e-9
    # (b) 运行态 live 累加的是真实费（增量==真实费，而非 0.0002 估算回退）
    delta = gx.live[gid].real_fee_paid - fee_after_open
    assert abs(delta - real_fill_fee) < 1e-9
    # (c) accounting.fee_paid 已用真实快照
    acc = gx.accounting.get(gid)
    assert abs(acc.fee_paid - gx.live[gid].snapshot(ex.fetch_price(SYM))['fee_paid']) < 1e-9


def ex_exchange_name():
    return 'fake'

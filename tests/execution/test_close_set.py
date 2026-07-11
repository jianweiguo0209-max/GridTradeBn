# tests/execution/test_close_set.py
"""组件二核心(spec 2026-07-11-symbol-desk):close_set 关格集合净额化。
①对冲对同关=零交易所单(PUMP gt01/gt07 案型);②混合集合恰一张净额 reduce 单;
③单格退化 ≡ 旧 ex.close 逐位;④幂等重入;⑤残余分摊 exclude 集合成员;
⑥滑点归因=执行格承担(用户已决)。"""
from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store, n, stop_orders=False):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0, stop_orders_enabled=stop_orders)
    gids = [gx.open('fake', BTC, dict(GP), tag='t%d' % i) for i in range(n)]
    return ex, gx, gids


def _force(ex, gx, want):
    for gid, c in want.items():
        cur = gx.live[gid].net_position
        d = c - cur
        if abs(d) > 1e-12:
            gx.live[gid].record_fill(100.0, 'buy' if d > 0 else 'sell', abs(d), 1000)
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, float(sum(want.values())), 100.0)


def _market_orders(ex):
    return [t for t in ex._trades if getattr(t, 'client_oid', '') and ':close:' in str(t.client_oid)]


def test_hedged_pair_zero_exchange_orders(store):
    # PUMP 案型:±X 对冲对同关 → 交易所零市价单,双格 CLOSED,记录按 mark 实现
    ex, gx, (a, b) = _setup(store, 2)
    _force(ex, gx, {a: 3.2e5, b: -3.2e5})
    n_trades_before = len(ex._trades)
    out = gx.ledger.close_set([a, b], BTC, '周期再平衡')
    assert len(ex._trades) == n_trades_before              # 零交易所成交
    assert abs(ex.fetch_positions(BTC).net_size) < 1e-9
    assert gx.grids.get(a).status == 'CLOSED' and gx.grids.get(b).status == 'CLOSED'
    assert len(out) == 2 and len(gx.records.list_by_grid(a)) == 1
    assert abs(gx.live[a].net_position) < 1e-4 and abs(gx.live[b].net_position) < 1e-4


def test_mixed_set_single_net_reduce(store):
    # 4 格 (+5,-3,+2,-1) 交易所净 +3 → 恰一张 reduce 单、量=3、执行格=+5(同号最大)
    ex, gx, (a, b, c, d) = _setup(store, 4)
    _force(ex, gx, {a: 5.0, b: -3.0, c: 2.0, d: -1.0})
    before = len(ex._trades)
    gx.ledger.close_set([a, b, c, d], BTC, '周期再平衡')
    new = ex._trades[before:]
    assert len(new) == 1 and new[0].side == 'sell' and abs(new[0].size - 3.0) < 1e-9
    assert str(new[0].client_oid).startswith('%s:close:' % a)   # 执行格=+5 的 a
    assert abs(ex.fetch_positions(BTC).net_size) < 1e-9
    for g in (a, b, c, d):
        assert gx.grids.get(g).status == 'CLOSED'
    for g in (b, c, d):                                 # 非执行格账本经转仓归零
        assert abs(gx.live[g].net_position) < 1e-9
    # 执行格账本保留净额 +3(legacy 无兄弟扫平语义:record 按 mark 实现,不写合成行)
    assert abs(gx.live[a].net_position - 3.0) < 1e-9


def test_single_grid_degenerates_to_legacy_close(store):
    # 单格集合 ≡ 旧 ex.close:两条路径在镜像场景下产生等同的交易所/记录/状态结果
    for path in ('legacy', 'set'):
        from gridtrade.state.store import StateStore
        st = StateStore.in_memory(); st.create_all()
        ex, gx, (a, b) = _setup(st, 2)
        _force(ex, gx, {a: 5.0, b: 3.0})
        if path == 'legacy':
            legacy = gx.close(a, BTC, '周期再平衡')
        else:
            setout = gx.ledger.close_set([a], BTC, '周期再平衡')[0]
        left = ex.fetch_positions(BTC).net_size
        rec = gx.records.list_by_grid(a)[0]
        if path == 'legacy':
            base = (round(left, 9), rec.exit_reason, round(rec.pnl_ratio, 12),
                    gx.grids.get(a).status, round(gx.live[b].net_position, 9))
        else:
            assert (round(left, 9), rec.exit_reason, round(rec.pnl_ratio, 12),
                    gx.grids.get(a).status, round(gx.live[b].net_position, 9)) == base


def test_close_set_idempotent(store):
    ex, gx, (a, b) = _setup(store, 2)
    _force(ex, gx, {a: 4.0, b: -4.0})
    gx.ledger.close_set([a, b], BTC, '周期再平衡')
    rows_before = sum(len([f for f in gx.fills.list_by_grid(g)
                           if f.trade_id.startswith('ledger:')]) for g in (a, b))
    trades_before = len(ex._trades)
    gx.ledger.close_set([a, b], BTC, '周期再平衡')     # 重入
    rows_after = sum(len([f for f in gx.fills.list_by_grid(g)
                          if f.trade_id.startswith('ledger:')]) for g in (a, b))
    assert rows_after == rows_before and len(ex._trades) == trades_before
    assert len(gx.records.list_by_grid(a)) == 1        # 记录不重复


def test_residual_excludes_set_members(store):
    # 3 格集合(+6,-2,+1) + 集合外幸存格 E(-5):净 0 → 预净额后执行格残余 5 全给 E
    ex, gx, (a, b, c, e) = _setup(store, 4)
    _force(ex, gx, {a: 6.0, b: -2.0, c: 1.0, e: -5.0})
    before = len(ex._trades)
    gx.ledger.close_set([a, b, c], BTC, '周期再平衡')
    assert len(ex._trades) == before                    # 净 0:零交易所单
    assert abs(gx.live[e].net_position) < 1e-9          # E 收 5 → 归零 == 交易所
    assert gx.grids.get(e).status == 'ACTIVE'


def test_fuses_cancelled_for_all_set_members(store):
    ex, gx, (a, b) = _setup(store, 2, stop_orders=True)
    _force(ex, gx, {a: 2.0, b: -2.0})
    gx.ledger.close_set([a, b], BTC, '周期再平衡')
    assert not ex._stops.get(BTC)                       # 两格丝全撤

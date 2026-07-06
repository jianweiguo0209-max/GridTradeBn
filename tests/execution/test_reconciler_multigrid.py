# tests/execution/test_reconciler_multigrid.py
"""cap=2 同币双格下的对账多格感知（mainnet KIOXIA 2026-07-06 同门相残事故根治）：
reconcile 的 unexpected 撤单曾写死每币一格——同币双格时互撤对方全部挂单（线单+fuse），
挂单存活仅 33s、两格经济死亡。修复口径：受保护集合 = 本币全部活跃格的挂单∪fuse。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup_two_grids(store, stop_orders=False):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0, stop_orders_enabled=stop_orders)
    ga = gx.open('fake', BTC, dict(GP), tag='tA')      # cap=2 默认：同币两格合法
    gb = gx.open('fake', BTC, dict(GP), tag='tB')
    return ex, gx, ga, gb


def test_reconcile_does_not_cancel_sibling_orders(store):
    ex, gx, ga, gb = _setup_two_grids(store)
    rec = Reconciler(gx)
    before = {o.id for o in ex.fetch_open_orders(BTC)}
    out_a = rec.reconcile_open_orders(ga, BTC)
    out_b = rec.reconcile_open_orders(gb, BTC)
    after = {o.id for o in ex.fetch_open_orders(BTC)}
    assert out_a == {'canceled': 0, 'replaced': 0}     # 同门单不是孤儿
    assert out_b == {'canceled': 0, 'replaced': 0}
    assert after == before                             # 双方挂单簿原封不动


def test_reconcile_still_cancels_true_orphan(store):
    ex, gx, ga, gb = _setup_two_grids(store)
    rec = Reconciler(gx)
    orphan = ex.create_limit_order(BTC, 'buy', 90.0, 1.0, client_oid='orphan:x')
    out = rec.reconcile_open_orders(ga, BTC)
    assert out['canceled'] == 1                        # 真孤儿（无主单）仍被清
    assert orphan.id not in {o.id for o in ex.fetch_open_orders(BTC)}


def test_reconcile_protects_sibling_fuses(store):
    ex, gx, ga, gb = _setup_two_grids(store, stop_orders=True)
    rec = Reconciler(gx)
    gb_row = gx.grids.get(gb)
    rec.reconcile_open_orders(ga, BTC)                 # A 对账不得撤 B 的保险丝
    on_book = {o.id for o in ex.fetch_open_orders(BTC)}
    assert gb_row.fuse_low_oid in on_book and gb_row.fuse_high_oid in on_book


def test_position_drift_aggregates_same_symbol_grids(store):
    # 双格同币：单格 model vs 全账户仓位必然背离——按币聚合后判定
    ex, gx, ga, gb = _setup_two_grids(store)
    ex.set_price(BTC, 100.6)                           # 双格各成交一笔卖单 → 各 -order_num
    gx.sync(ga, BTC)
    gx.sync(gb, BTC)
    rec = Reconciler(gx)
    da = rec.check_position_drift(ga, BTC)
    assert da is not None and da['ok'] is True         # Σmodel == exchange → 不误报
    assert da['model'] == gx.accounting.get(ga).net_position + \
           gx.accounting.get(gb).net_position          # 聚合口径

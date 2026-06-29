from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store, price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    return ex, GridExecutor(ex, store, cap=1000.0, leverage=5.0)


def test_reconcile_no_op_when_consistent(store):
    ex, gx = _setup(store, 100.0)
    gid = gx.open('fake', SYM, GP)
    r = Reconciler(gx).reconcile_open_orders(gid, SYM)
    assert r == {'canceled': 0, 'replaced': 0}      # 一致 -> 不动


def test_reconcile_replaces_missing_by_order_id(store):
    ex, gx = _setup(store, 100.0)
    gid = gx.open('fake', SYM, GP)
    victim = ex.fetch_open_orders(SYM)[0]
    ex._open.pop(victim.id, None)                    # 交易所侧撤掉一个挂单（库里仍 open）
    r = Reconciler(gx).reconcile_open_orders(gid, SYM)
    assert r['replaced'] == 1


def test_reconcile_cancels_orphan_by_order_id(store):
    ex, gx = _setup(store, 100.0)
    gid = gx.open('fake', SYM, GP)
    ex.create_limit_order(SYM, 'buy', 90.0, 1.0, client_oid='orphan')  # 我方不认的挂单
    r = Reconciler(gx).reconcile_open_orders(gid, SYM)
    assert r['canceled'] == 1


def test_reconcile_matches_by_order_id_when_exchange_client_oid_differs(store):
    # 模拟 HL：交易所返回的挂单不带我方 cloid（client_oid 与库里不同），但 order id 一致。
    # 旧的 client_oid 匹配会把全部挂单当孤儿撤掉+全部当缺失补回；order id 匹配应零动作。
    ex, gx = _setup(store, 100.0)
    gid = gx.open('fake', SYM, GP)
    for o in ex._open.values():
        o.client_oid = '0xopaque-' + o.id
    r = Reconciler(gx).reconcile_open_orders(gid, SYM)
    assert r == {'canceled': 0, 'replaced': 0}

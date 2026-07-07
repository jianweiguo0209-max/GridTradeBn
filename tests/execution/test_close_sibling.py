# tests/execution/test_close_sibling.py
"""cap=2 同币双格下的关格多格感知（mainnet NBIS 2026-07-07 关格相残根治）：
finalize_close 曾用 symbol 级 cancel_all + 全净仓 reduce——关 A 撤光 B 的挂单/保险丝
并把 B 的仓位一起市价平掉（平仓成交挂 A 的 oid，B 永不摄入 → 幻影账簿）。
修复口径：有兄弟 → 只撤自己的单、只平自己 accounting 份额；无兄弟 → 旧行为不变。"""
from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup_two_grids(store, stop_orders=False):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0,
                      stop_orders_enabled=stop_orders)
    ga = gx.open('fake', BTC, dict(GP), tag='tA')
    gb = gx.open('fake', BTC, dict(GP), tag='tB')
    return ex, gx, ga, gb


def test_close_with_sibling_preserves_sibling_orders(store):
    ex, gx, ga, gb = _setup_two_grids(store)
    gb_oids = {o.exchange_order_id for o in gx.orders.list_by_grid(gb)
               if o.status == 'open'}
    gx.close(ga, BTC, '周期再平衡')
    on_book = {o.id for o in ex.fetch_open_orders(BTC)}
    assert gb_oids and gb_oids.issubset(on_book)       # 兄弟挂单原封不动
    # 自己的单全下书
    ga_oids = {o.exchange_order_id for o in gx.orders.list_by_grid(ga)
               if o.exchange_order_id}
    assert not (ga_oids & on_book)
    assert gx.grids.get(ga).status == 'CLOSED'


def test_close_with_sibling_reduces_only_own_share(store):
    ex, gx, ga, gb = _setup_two_grids(store)
    ex.set_price(BTC, 98.5)          # 双格各自买线成交 → 各自净多
    gx.sync(ga, BTC)
    gx.sync(gb, BTC)
    own_a = gx.accounting.get(ga).net_position
    own_b = gx.accounting.get(gb).net_position
    assert own_a > 0 and own_b > 0
    total = ex.fetch_positions(BTC).net_size
    assert abs(total - (own_a + own_b)) < 1e-9         # 前提：账户级聚合
    gx.close(ga, BTC, '周期再平衡')
    left = ex.fetch_positions(BTC).net_size
    assert abs(left - own_b) < 1e-9                    # 只平走 A 的份额，B 的仓保留


def test_close_with_sibling_preserves_sibling_fuses(store):
    ex, gx, ga, gb = _setup_two_grids(store, stop_orders=True)
    gb_row = gx.grids.get(gb)
    gx.close(ga, BTC, '周期再平衡')
    on_book = {o.id for o in ex.fetch_open_orders(BTC)}
    assert gb_row.fuse_low_oid in on_book and gb_row.fuse_high_oid in on_book


def test_close_with_sibling_sign_mismatch_skips_reduce(store):
    # 自身份额为多、但交易所净仓被兄弟对冲成空——reduce-only 无可平，不得越界砍兄弟。
    ex, gx, ga, gb = _setup_two_grids(store)
    ex.set_price(BTC, 98.5)          # A 买线成交 → A 净多
    gx.sync(ga, BTC)
    own_a = gx.accounting.get(ga).net_position
    assert own_a > 0
    # 人工把交易所净仓压成净空（模拟兄弟深度做空的账户级净额）
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, -3.0, 100.0)
    before = ex.fetch_positions(BTC).net_size
    gx.close(ga, BTC, '周期再平衡')
    assert abs(ex.fetch_positions(BTC).net_size - before) < 1e-9   # 一手未动
    assert gx.grids.get(ga).status == 'CLOSED'


def test_close_last_grid_still_sweeps_symbol(store):
    # 无兄弟（或兄弟已全关）→ 保留 symbol 级扫除旧行为：孤儿单/孤儿仓一并清。
    ex, gx, ga, gb = _setup_two_grids(store)
    gx.close(ga, BTC, '周期再平衡')
    orphan = ex.create_limit_order(BTC, 'buy', 90.0, 1.0, client_oid='orphan:x')
    gx.close(gb, BTC, '周期再平衡')                     # 最后一格：cancel_all 扫孤儿
    assert orphan.id not in {o.id for o in ex.fetch_open_orders(BTC)}
    assert abs(ex.fetch_positions(BTC).net_size) <= gx.min_amount

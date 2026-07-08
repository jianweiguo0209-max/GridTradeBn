# tests/execution/test_position_ledger.py
"""PositionLedger 核心(spec 2026-07-08-position-ledger):claims/签名权重/合成转仓/丝摄入。
核心不变量:Σ claims(本币活跃格) = 交易所净仓;转仓是纯账本操作(不动交易所)。"""
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


def _force_claims(gx, ga, gb, a, b, px=100.0):
    """直接向 live 账本写成交,精确制造 claims(单测捷径,绕过撮合)。"""
    for gid, want in ((ga, a), (gb, b)):
        cur = gx.live[gid].net_position
        d = want - cur
        if abs(d) > 1e-12:
            gx.live[gid].record_fill(px, 'buy' if d > 0 else 'sell', abs(d), 1000)


# ── claims ──

def test_claims_reads_live_books(store):
    ex, gx, ga, gb = _setup_two_grids(store)
    _force_claims(gx, ga, gb, 5.0, -3.0)
    cl = gx.ledger.claims(BTC, 'fake')
    assert abs(cl[ga] - 5.0) < 1e-9 and abs(cl[gb] + 3.0) < 1e-9


def test_claim_falls_back_to_accounting_when_not_loaded(store):
    ex, gx, ga, gb = _setup_two_grids(store)
    ex.set_price(BTC, 98.5)
    gx.sync(ga, BTC)
    want = gx.accounting.get(ga).net_position
    del gx.live[ga]                      # 模拟未 restore
    assert abs(gx.ledger.claim(ga) - want) < 1e-9


# ── funding 签名权重 ──

def test_funding_weight_single_grid_is_one(store):
    ex, gx, ga, gb = _setup_two_grids(store)
    gx.grids.set_close_reason(gb, 'x')
    g = gx.grids.get(gb)
    gx.grids.transition_status(gb, 'CLOSING', expected_version=g.version)
    g = gx.grids.get(gb)
    gx.grids.transition_status(gb, 'CLOSED', expected_version=g.version)
    _force_claims(gx, ga, gb, 5.0, 0.0)
    assert gx.ledger.funding_weight(ga, BTC) == 1.0


def test_funding_weight_same_sign_sums_to_one(store):
    ex, gx, ga, gb = _setup_two_grids(store)
    _force_claims(gx, ga, gb, 6.0, 2.0)
    wa = gx.ledger.funding_weight(ga, BTC)
    wb = gx.ledger.funding_weight(gb, BTC)
    assert abs(wa - 0.75) < 1e-9 and abs(wb - 0.25) < 1e-9
    assert abs(wa + wb - 1.0) < 1e-12


def test_funding_weight_hedged_signed(store):
    # 对冲 (+5, −3):净 +2,per-unit 均匀 → 权重 (2.5, −1.5),对冲侧赚对侧 funding
    ex, gx, ga, gb = _setup_two_grids(store)
    _force_claims(gx, ga, gb, 5.0, -3.0)
    assert abs(gx.ledger.funding_weight(ga, BTC) - 2.5) < 1e-9
    assert abs(gx.ledger.funding_weight(gb, BTC) + 1.5) < 1e-9


def test_funding_weight_net_zero_splits_evenly(store):
    ex, gx, ga, gb = _setup_two_grids(store)
    _force_claims(gx, ga, gb, 5.0, -5.0)
    assert abs(gx.ledger.funding_weight(ga, BTC) - 0.5) < 1e-12
    assert abs(gx.ledger.funding_weight(gb, BTC) - 0.5) < 1e-12


# ── 合成转仓 ──

def test_settle_transfer_moves_claim_between_books(store):
    ex, gx, ga, gb = _setup_two_grids(store)
    _force_claims(gx, ga, gb, 5.0, -5.0)
    net_before = ex.fetch_positions(BTC).net_size
    gx.ledger.settle_transfer(ga, gb, BTC, 5.0, 100.0, 'closeshare')
    assert abs(gx.live[ga].net_position) < 1e-9            # 转出格归零
    assert abs(gx.live[gb].net_position) < 1e-9            # −5 + 5 = 0
    assert ex.fetch_positions(BTC).net_size == net_before  # 纯账本,交易所一手未动
    rows_a = [f for f in gx.fills.list_by_grid(ga) if f.trade_id.startswith('ledger:')]
    rows_b = [f for f in gx.fills.list_by_grid(gb) if f.trade_id.startswith('ledger:')]
    assert len(rows_a) == 1 and len(rows_b) == 1
    assert rows_a[0].side == 'sell' and rows_b[0].side == 'buy'   # 转出多头=卖,接收=买
    assert rows_a[0].fee == 0.0 and rows_a[0].line_index == -1


def test_settle_transfer_negative_qty_sides_flip(store):
    ex, gx, ga, gb = _setup_two_grids(store)
    _force_claims(gx, ga, gb, -4.0, 4.0)
    gx.ledger.settle_transfer(ga, gb, BTC, -4.0, 100.0, 'closeshare')
    assert abs(gx.live[ga].net_position) < 1e-9
    assert abs(gx.live[gb].net_position) < 1e-9
    row_a = [f for f in gx.fills.list_by_grid(ga) if f.trade_id.startswith('ledger:')][0]
    assert row_a.side == 'buy'                             # 转出空头=买回


def test_settle_transfer_zero_qty_noop(store):
    ex, gx, ga, gb = _setup_two_grids(store)
    gx.ledger.settle_transfer(ga, gb, BTC, 0.0, 100.0, 'closeshare')
    assert not [f for f in gx.fills.list_by_grid(ga) if f.trade_id.startswith('ledger:')]


# ── 丝成交摄入 ──

def test_ingest_fuse_fills_by_oid_with_real_fee(store):
    ex, gx, ga, gb = _setup_two_grids(store, stop_orders=True)
    g = gx.grids.get(ga)
    ex.set_price(BTC, 96.5)                # 穿破 stop_low → A 的 sell 丝触发成交
    fuse_trades = [t for t in ex.fetch_my_trades(BTC) if t.order_id == g.fuse_low_oid]
    assert fuse_trades
    before = gx.live[ga].net_position
    n = gx.ledger.ingest_fuse_fills(ga, BTC, g.fuse_low_oid)
    assert n == len(fuse_trades)
    got = [f for f in gx.fills.list_by_grid(ga) if f.line_index == -1]
    assert len(got) == n
    assert got[0].trade_id == str(fuse_trades[0].id)       # 真实 trade_id(非 ledger:)
    assert abs(got[0].fee - fuse_trades[0].fee) < 1e-12    # 真实 fee
    moved = sum((-t.size if t.side == 'sell' else t.size) for t in fuse_trades)
    assert abs(gx.live[ga].net_position - (before + moved)) < 1e-9
    assert gx.ledger.ingest_fuse_fills(ga, BTC, g.fuse_low_oid) == 0   # 幂等去重

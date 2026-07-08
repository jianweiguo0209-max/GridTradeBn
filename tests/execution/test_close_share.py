# tests/execution/test_close_share.py
"""close_share 关格净额化(spec 2026-07-08-position-ledger 冲突①残留根治):
v23 只做了"对冲时不动手"(幸存格留模型-账户差);现在残余份额按 mark 价内部转给
幸存格 → 关格后 Σclaims == 交易所净仓,双方模型对齐。reduce 每步写 ledger:reduce
合成行 → 账本始终反映"还剩多少没平",崩溃续平幂等。"""
from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup_two_grids(store):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    ga = gx.open('fake', BTC, dict(GP), tag='tA')
    gb = gx.open('fake', BTC, dict(GP), tag='tB')
    return ex, gx, ga, gb


def _force_claims(ex, gx, ga, gb, a, b, px=100.0):
    """账本直写制造 claims,并把交易所净仓对齐到 a+b(不变量成立的前提态)。"""
    for gid, want in ((ga, a), (gb, b)):
        cur = gx.live[gid].net_position
        d = want - cur
        if abs(d) > 1e-12:
            gx.live[gid].record_fill(px, 'buy' if d > 0 else 'sell', abs(d), 1000)
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, float(a + b), px)


def _ledger_rows(gx, gid):
    return [f for f in gx.fills.list_by_grid(gid) if f.trade_id.startswith('ledger:')]


def test_hedged_close_transfers_remainder_to_survivor(store):
    # 对冲 (+5, −5):交易所净 0 → 零市价单;A 残余 5 转 B → B 账本 0 == 交易所 0(根治)
    ex, gx, ga, gb = _setup_two_grids(store)
    _force_claims(ex, gx, ga, gb, 5.0, -5.0)
    gx.close(ga, BTC, '周期再平衡')
    assert abs(ex.fetch_positions(BTC).net_size) < 1e-9        # 交易所一手未动
    assert abs(gx.live[gb].net_position) < 1e-9                # 幸存格模型对齐
    assert gx.grids.get(ga).status == 'CLOSED'
    rows_b = _ledger_rows(gx, gb)
    assert len(rows_b) == 1 and rows_b[0].side == 'buy' and rows_b[0].size == 5.0


def test_same_sign_close_reduces_like_v23_no_transfer(store):
    # 同号 (+5, +3):reduce 自己的 5,兄弟的 3 原封;残余 0 → 无转仓行
    ex, gx, ga, gb = _setup_two_grids(store)
    _force_claims(ex, gx, ga, gb, 5.0, 3.0)
    gx.close(ga, BTC, '周期再平衡')
    assert abs(ex.fetch_positions(BTC).net_size - 3.0) < 1e-9  # B 的份额保留
    assert not [f for f in _ledger_rows(gx, gb)]               # B 无转仓行
    assert abs(gx.live[gb].net_position - 3.0) < 1e-9


def test_partial_reduce_then_transfer(store):
    # 混合 (+5, −2):净 +3 → reduce 3,残余 2 转 B → B 账本 0 == 交易所 0
    ex, gx, ga, gb = _setup_two_grids(store)
    _force_claims(ex, gx, ga, gb, 5.0, -2.0)
    gx.close(ga, BTC, '周期再平衡')
    assert abs(ex.fetch_positions(BTC).net_size) < 1e-9
    assert abs(gx.live[gb].net_position) < 1e-9
    rows_b = _ledger_rows(gx, gb)
    assert len(rows_b) == 1 and abs(rows_b[0].size - 2.0) < 1e-9
    # A 账本:reduce 合成行 + 转出行 → 归零
    assert abs(gx.live[ga].net_position) < 1e-9


def test_close_share_idempotent_on_rerun(store):
    # 续平幂等:close 完成后再跑一遍 close_share → 无新账本行、无新市价单
    ex, gx, ga, gb = _setup_two_grids(store)
    _force_claims(ex, gx, ga, gb, 5.0, -5.0)
    gx.close(ga, BTC, '周期再平衡')
    rows_before = len(_ledger_rows(gx, ga)) + len(_ledger_rows(gx, gb))
    net_before = ex.fetch_positions(BTC).net_size
    gx.ledger.close_share(ga, BTC)
    assert len(_ledger_rows(gx, ga)) + len(_ledger_rows(gx, gb)) == rows_before
    assert ex.fetch_positions(BTC).net_size == net_before


def test_drift_check_clean_after_hedged_close(store):
    # 根治验收:对冲关格后幸存格 drift ok(v23 残留=此处必然超容差)
    from gridtrade.execution.reconciler import Reconciler
    ex, gx, ga, gb = _setup_two_grids(store)
    _force_claims(ex, gx, ga, gb, 5.0, -5.0)
    gx.close(ga, BTC, '周期再平衡')
    gx.sync(gb, BTC)                     # 把 live 账本落到 accounting
    rec = Reconciler(gx)
    out = rec.check_position_drift(gb, BTC)
    assert out is not None and out['ok'], out

"""关格 reduce 合成行携真实成交均价+真实费——兑现 live_equity snapshot 注释的
"退出时由 executor 落真实费"（testnet 实证 2026-07-14：SKYAI 关格真实 taker 费
$0.198 曾丢失，DB 只有 mark 价 fee=0 合成行）。fake 交易所 taker 费率 0.0005。"""
from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    gid = gx.open('fake', BTC, dict(GP), tag='tF')
    return ex, gx, gid


def _reduce_rows(gx, gid):
    return [f for f in gx.fills.list_by_grid(gid)
            if f.trade_id.startswith('ledger:reduce')]


def test_flatten_reduce_row_carries_real_px_and_fee(store):
    # 净仓 +5，价格走到 110 后关格 → reduce 卖 5 的合成行须携真实成交价与真实费
    ex, gx, gid = _setup(store)
    ex.cancel_all(BTC)   # 清开格梯子：隔离场景只测 reduce（价格上移不触发限价撮合）
    gx.live[gid].record_fill(100.0, 'buy', 5.0, 1000, 0.0)   # 播种建仓显式 0 费,隔离断言
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, 5.0, 100.0)
    ex.set_price(BTC, 110.0)
    gx.close(gid, BTC, '测试关格')
    rows = _reduce_rows(gx, gid)
    assert len(rows) == 1
    r = rows[0]
    assert abs(r.price - 110.0) < 1e-9                     # 真实成交均价（fake 按现价成交）
    assert abs(r.fee - 110.0 * 5.0 * 0.0005) < 1e-9        # 真实 taker 费，不再是 0
    # 记账口径同步：fee_paid 吃到这笔真实费
    snap = gx.live[gid].snapshot(110.0)
    assert abs(snap['fee_paid'] - 110.0 * 5.0 * 0.0005) < 1e-9


def test_transfer_rows_keep_zero_fee(store):
    # 转仓双边行（内部净额化、无真实成交）保持 0 费——语义不变
    from tests.execution.test_close_share import (_setup_two_grids, _force_claims,
                                                  _ledger_rows)
    ex, gx, ga, gb = _setup_two_grids(store)
    _force_claims(ex, gx, ga, gb, 5.0, -5.0)
    gx.close(ga, BTC, '周期再平衡')
    rows_b = _ledger_rows(gx, gb)
    assert len(rows_b) == 1 and rows_b[0].fee == 0.0

# tests/execution/test_ledger_eventid.py
"""组件三前置(spec 2026-07-11-symbol-desk):合成转仓对共享 event_id。
新 trade_id 格式 ledger:<event>:<gid>:<eid>,eid='ts-seq' 由 settle_transfer 生成一次、
两行共享 → 审计可精确配对(恰 2 行/带符号和 0/同价)。reduce 单边行 eid 独享。
前缀不变 ⇒ max_ts 游标排除语义不变。"""
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
    ga = gx.open('fake', BTC, dict(GP), tag='tA')
    gb = gx.open('fake', BTC, dict(GP), tag='tB')
    return ex, gx, ga, gb


def _eid(trade_id):
    return trade_id.split(':')[-1]


def test_transfer_pair_shares_eid(store):
    ex, gx, ga, gb = _setup(store)
    gx.live[ga].record_fill(100.0, 'buy', 5.0, 1000)
    gx.live[gb].record_fill(100.0, 'sell', 5.0, 1000)
    gx.ledger.settle_transfer(ga, gb, BTC, 5.0, 100.0, 'closeshare')
    ra = [f for f in gx.fills.list_by_grid(ga) if f.trade_id.startswith('ledger:')][0]
    rb = [f for f in gx.fills.list_by_grid(gb) if f.trade_id.startswith('ledger:')][0]
    assert _eid(ra.trade_id) == _eid(rb.trade_id)          # 共享 eid
    assert '-' in _eid(ra.trade_id)                        # 新格式 'ts-seq'
    assert ra.side != rb.side and ra.size == rb.size and ra.price == rb.price
    assert ra.trade_id != rb.trade_id                      # gid 段不同,主键不撞


def test_two_transfers_distinct_eids(store):
    ex, gx, ga, gb = _setup(store)
    gx.live[ga].record_fill(100.0, 'buy', 6.0, 1000)
    gx.live[gb].record_fill(100.0, 'sell', 6.0, 1000)
    gx.ledger.settle_transfer(ga, gb, BTC, 2.0, 100.0, 'closeshare')
    gx.ledger.settle_transfer(ga, gb, BTC, 3.0, 100.0, 'closeshare')
    eids = {_eid(f.trade_id) for f in gx.fills.list_by_grid(ga)
            if f.trade_id.startswith('ledger:')}
    assert len(eids) == 2                                  # 两次转仓 eid 不同


def test_reduce_row_single_sided_own_eid(store):
    ex, gx, ga, gb = _setup(store)
    gx.live[ga].record_fill(100.0, 'buy', 5.0, 1000)
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, 5.0, 100.0)
    gx.close(ga, BTC, '周期再平衡')
    reduce_rows = [f for f in gx.fills.list_by_grid(ga)
                   if f.trade_id.startswith('ledger:reduce:')]
    assert len(reduce_rows) == 1
    assert gx.fills.max_ts(ga) == 1000                     # 游标排除语义不变

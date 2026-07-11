# tests/runtime/test_verify_ledger.py
"""组件三(spec 2026-07-11-symbol-desk):verify-ledger 守恒审计。
正常库静默;单边合成行/量不守恒对 → pairs_bad;accounting 被篡改 → replay_bad;
旧格式行计 legacy 不误报。masking 从"查不到"变"必留痕"。"""
from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.runtime.dbadmin import verify_ledger
from gridtrade.state.models import Fill

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _healthy(store):
    """对冲关格 → 库里有转仓对(共享 eid)+ 活跃幸存格。"""
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    ga = gx.open('fake', BTC, dict(GP), tag='tA')
    gb = gx.open('fake', BTC, dict(GP), tag='tB')
    for gid, side in ((ga, 'buy'), (gb, 'sell')):
        gx.fills.add_if_new(Fill(trade_id='t-%s' % gid, grid_id=gid, line_index=3,
                                 side=side, price=100.0, size=5.0, fee=0.1, ts=1000))
        gx.live[gid].record_fill(100.0, side, 5.0, 1000, 0.1)
        gx._book_ids.setdefault(gid, set()).add('t-%s' % gid)
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, 0.0, 100.0)
    gx.close(ga, BTC, '周期再平衡')
    gx.sync(gb, BTC)                      # 幸存格 acc 落库(重放查的对照)
    return ex, gx, ga, gb


def test_healthy_store_all_clear(store):
    _healthy(store)
    rep = verify_ledger(store, log=lambda *a: None)
    assert rep['pairs_bad'] == 0 and rep['replay_bad'] == 0
    assert rep['pairs_ok'] >= 1                          # 至少一对转仓行被配对校验


def test_single_sided_synthetic_flagged(store):
    ex, gx, ga, gb = _healthy(store)
    gx.fills.add_if_new(Fill(trade_id='ledger:closeshare:%s:999-1' % gb, grid_id=gb,
                             line_index=-1, side='buy', price=100.0, size=3.0,
                             fee=0.0, ts=999))           # 单边行(无配对)
    rep = verify_ledger(store, log=lambda *a: None)
    assert rep['pairs_bad'] == 1


def test_unbalanced_pair_flagged(store):
    ex, gx, ga, gb = _healthy(store)
    for gid, side, size in ((ga, 'sell', 3.0), (gb, 'buy', 2.0)):   # 量不守恒
        gx.fills.add_if_new(Fill(trade_id='ledger:closeshare:%s:888-7' % gid,
                                 grid_id=gid, line_index=-1, side=side, price=100.0,
                                 size=size, fee=0.0, ts=888))
    rep = verify_ledger(store, log=lambda *a: None)
    assert rep['pairs_bad'] == 1


def test_tampered_accounting_flagged(store):
    ex, gx, ga, gb = _healthy(store)
    acc = gx.accounting.get(gb)
    acc.net_position = acc.net_position + 999.0          # 篡改快照
    gx.accounting.save(acc)
    rep = verify_ledger(store, log=lambda *a: None)
    assert rep['replay_bad'] == 1


def test_legacy_format_rows_not_flagged(store):
    ex, gx, ga, gb = _healthy(store)
    gx.fills.add_if_new(Fill(trade_id='ledger:reduce:%s:1751900000000:3' % gb,
                             grid_id=gb, line_index=-1, side='sell', price=100.0,
                             size=1.0, fee=0.0, ts=777))  # 旧 5 段格式
    rep = verify_ledger(store, log=lambda *a: None)
    assert rep['pairs_bad'] == 0 and rep['legacy'] >= 1


# ── --records:record 直算重验(spec 2026-07-12-honest-record-pnl 组件二) ──


def test_records_audit_clean_store_silent(store):
    """_healthy 场景的关格 record 由直算 snapshot 落库 → --records 全绿。"""
    _healthy(store)
    rep = verify_ledger(store, log=lambda *a: None, records=True)
    assert rep['records_scanned'] >= 1
    assert rep['records_bad'] == 0


def test_records_audit_flags_tampered_record(store):
    """把 record.total_pnl 篡改(模拟引擎时代失真)→ RECORD deviation 必报。"""
    import sqlalchemy as sa
    from gridtrade.state.models import order_records
    _healthy(store)
    with store.engine.begin() as c:
        c.execute(sa.update(order_records).values(total_pnl=15.0, pnl_ratio=0.01))
    msgs = []
    rep = verify_ledger(store, log=lambda *a: msgs.append(a), records=True)
    assert rep['records_bad'] >= 1
    assert any('RECORD deviation' in str(m) for m in msgs)


def test_records_audit_no_fills_counted_not_flagged(store):
    """无 fills 的旧 record(迁移前)只计数不误报。"""
    from gridtrade.state.models import Record, now_ms
    from gridtrade.state.records import RecordRepository
    RecordRepository(store).add(Record(id='', exchange='fake', symbol=BTC, tag='old',
                                       grid_id='ghost', sz=100.0, total_pnl=1.0,
                                       pnl_ratio=0.01, exit_reason='x',
                                       opened_at=1, closed_at=now_ms()))
    rep = verify_ledger(store, log=lambda *a: None, records=True)
    assert rep['records_nofills'] >= 1 and rep['records_bad'] == 0

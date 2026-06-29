from gridtrade.dashboard.queries import build_records
from gridtrade.state.records import RecordRepository
from gridtrade.state.fills import FillRepository
from gridtrade.state.models import Record, Fill


def test_records_aggregates_by_tag_and_orders_recent_first(store):
    recs = RecordRepository(store)
    recs.add(Record(id='r1', exchange='hyperliquid', symbol='BTC/USDT:USDT',
                    tag='gt0', total_pnl=10.0, pnl_ratio=0.1,
                    exit_reason='take_profit', closed_at=2000))
    recs.add(Record(id='r2', exchange='hyperliquid', symbol='ETH/USDT:USDT',
                    tag='gt0', total_pnl=-4.0, pnl_ratio=-0.04,
                    exit_reason='stop_loss', closed_at=3000))
    recs.add(Record(id='r3', exchange='hyperliquid', symbol='SOL/USDT:USDT',
                    tag='gt1', total_pnl=2.0, pnl_ratio=0.02,
                    exit_reason='take_profit', closed_at=1000))
    FillRepository(store).add_if_new(Fill(trade_id='t1', grid_id='g1', line_index=0,
                                          side='buy', price=90.0, size=1.0, ts=5000))

    dto = build_records(store)
    assert [r.id for r in dto.records] == ['r2', 'r1', 'r3']   # closed_at 降序
    by = {s.tag: s for s in dto.tag_summaries}
    assert by['gt0'].count == 2
    assert by['gt0'].total_pnl == 6.0       # 10 - 4
    assert by['gt0'].win_count == 1
    assert round(by['gt0'].win_rate, 4) == 0.5
    assert by['gt1'].count == 1
    assert len(dto.recent_fills) == 1
    assert dto.recent_fills[0].grid_id == 'g1'


def test_records_ignores_unclosed_records(store):
    RecordRepository(store).add(Record(id='open1', exchange='hyperliquid',
                                       symbol='BTC/USDT:USDT', tag='gt0',
                                       closed_at=None))
    dto = build_records(store)
    assert dto.records == []
    assert dto.tag_summaries == []

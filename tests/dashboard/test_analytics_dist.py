from gridtrade.dashboard.analytics import fill_distribution, exit_reason_stats
from gridtrade.state.fills import FillRepository
from gridtrade.state.records import RecordRepository
from gridtrade.state.models import Fill, Record


def test_fill_distribution(store):
    f = FillRepository(store)
    f.add_if_new(Fill(trade_id='t1', grid_id='g', line_index=0, side='buy', price=1, size=1, fee=0.1, ts=1000))
    f.add_if_new(Fill(trade_id='t2', grid_id='g', line_index=1, side='sell', price=1, size=1, fee=0.2, ts=3_600_000 + 1000))
    f.add_if_new(Fill(trade_id='t3', grid_id='g', line_index=0, side='buy', price=1, size=1, fee=0.3, ts=3_600_000 + 2000))
    d = fill_distribution(store)
    assert dict(d.by_side) == {'buy': 2, 'sell': 1}
    assert dict(d.by_line) == {0: 2, 1: 1}
    assert round(d.fee_cum[-1][1], 4) == 0.6       # 累计费 0.1+0.2+0.3
    assert len(d.by_hour) == 2                      # 两个小时桶


def test_exit_reason_stats(store):
    recs = RecordRepository(store)
    recs.add(Record(id='r1', exchange='x', symbol='B', tag='t', total_pnl=10.0,
                    exit_reason='take_profit', closed_at=1000))
    recs.add(Record(id='r2', exchange='x', symbol='E', tag='t', total_pnl=-4.0,
                    exit_reason='stop_loss', closed_at=2000))
    recs.add(Record(id='r3', exchange='x', symbol='S', tag='t', total_pnl=6.0,
                    exit_reason='take_profit', closed_at=3000))
    by = {s.reason: s for s in exit_reason_stats(store)}
    assert by['take_profit'].count == 2
    assert round(by['take_profit'].share, 4) == round(2/3, 4)
    assert by['take_profit'].avg_pnl == 8.0         # (10+6)/2

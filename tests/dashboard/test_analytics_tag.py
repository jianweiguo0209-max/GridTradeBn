from gridtrade.dashboard.analytics import tag_attribution
from gridtrade.state.records import RecordRepository
from gridtrade.state.fills import FillRepository
from gridtrade.state.models import Record, Fill


def test_tag_attribution(store):
    recs = RecordRepository(store)
    recs.add(Record(id='r1', exchange='x', symbol='BTC', tag='gt0', grid_id='g1',
                    total_pnl=10.0, opened_at=1000, closed_at=4000))
    recs.add(Record(id='r2', exchange='x', symbol='ETH', tag='gt0', grid_id='g2',
                    total_pnl=-4.0, opened_at=2000, closed_at=5000))
    fills = FillRepository(store)
    fills.add_if_new(Fill(trade_id='t1', grid_id='g1', line_index=0, side='buy',
                          price=1.0, size=1.0, fee=0.3, ts=1500))
    fills.add_if_new(Fill(trade_id='t2', grid_id='g2', line_index=0, side='sell',
                          price=1.0, size=1.0, fee=0.2, ts=2500))
    s = {t.tag: t for t in tag_attribution(store)}['gt0']
    assert s.count == 2
    assert s.total_pnl == 6.0
    assert s.total_fee == 0.5
    assert round(s.net_pnl, 4) == 5.5            # 6.0 - 0.5
    assert s.win_count == 1 and round(s.win_rate, 4) == 0.5
    assert s.avg_hold_ms == 3000                 # (3000+3000)/2
    assert round(s.max_drawdown, 4) == 4.0       # 峰值 10 → 谷 6，回撤 4

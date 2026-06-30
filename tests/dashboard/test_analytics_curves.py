# tests/dashboard/test_analytics_curves.py
from gridtrade.dashboard.analytics import realized_curve, equity_curve
from gridtrade.state.records import RecordRepository
from gridtrade.state.equity import EquitySnapshotRepository
from gridtrade.state.models import Record


def test_realized_curve_cumulative(store):
    recs = RecordRepository(store)
    recs.add(Record(id='r1', exchange='x', symbol='BTC', tag='gt0', total_pnl=10.0, closed_at=1000))
    recs.add(Record(id='r2', exchange='x', symbol='ETH', tag='gt0', total_pnl=-4.0, closed_at=2000))
    recs.add(Record(id='open', exchange='x', symbol='SOL', tag='gt0', closed_at=None))  # 未平不计
    assert realized_curve(store) == [(1000, 10.0), (2000, 6.0)]
    assert realized_curve(store, start_ms=1500) == [(2000, -4.0)]   # 范围过滤后从该窗起累加


def test_equity_curve(store):
    repo = EquitySnapshotRepository(store)
    repo.add_if_due(499.0, None, interval_sec=0, now_ms_fn=lambda: 1000)
    repo.add_if_due(505.0, None, interval_sec=0, now_ms_fn=lambda: 2000)
    assert equity_curve(store) == [(1000, 499.0), (2000, 505.0)]

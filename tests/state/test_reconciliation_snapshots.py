"""对账快照仓储(2026-07-17):selection/signal 两表往返/幂等覆盖/区间读。"""
from gridtrade.state.reconciliation_snapshots import (SelectionSnapshotRepository,
                                                      SignalSnapshotRepository)


# ---- selection_snapshots ----

def test_selection_roundtrip_and_overwrite(store):
    repo = SelectionSnapshotRepository(store)
    ranked = [{'symbol': 'A/USDT:USDT', 'factors': {'Er_2': 1.0}, 'rank_sum': 3, 'rank': 1},
              {'symbol': 'B/USDT:USDT', 'factors': {'Er_2': 0.5}, 'rank_sum': 6, 'rank': 2}]
    repo.add('binance', 1000, offset=5, ranked=ranked, picks=['A/USDT:USDT'])
    snap = repo.get('binance', 1000)
    assert snap['offset'] == 5
    assert snap['ranked'][0]['symbol'] == 'A/USDT:USDT' and snap['ranked'][0]['rank'] == 1
    assert snap['picks'] == ['A/USDT:USDT']
    # 同 tick 重跑 → 覆盖为最新
    repo.add('binance', 1000, offset=5, ranked=ranked[:1], picks=['A/USDT:USDT'])
    assert len(repo.get('binance', 1000)['ranked']) == 1
    assert repo.get('binance', 9999) is None


def test_selection_list_range_ordered(store):
    repo = SelectionSnapshotRepository(store)
    for ts in (3000, 1000, 2000):
        repo.add('binance', ts, offset=0, ranked=[], picks=[])
    out = repo.list_range('binance', 1000, 2500)
    assert [r['run_time'] for r in out] == [1000, 2000]


# ---- signal_snapshots ----

def test_signal_roundtrip_and_overwrite(store):
    repo = SignalSnapshotRepository(store)
    repo.add('g1', 5000, 'X/USDT:USDT', pv_spike=1, funding_rate=0.002, pnl_ratio=-0.01)
    evs = repo.list_for_grid('g1')
    assert len(evs) == 1 and evs[0]['pv_spike'] == 1
    assert abs(evs[0]['funding_rate'] - 0.002) < 1e-12 and abs(evs[0]['pnl_ratio'] + 0.01) < 1e-12
    # 同 (grid_id, ts) 重跑 → 覆盖
    repo.add('g1', 5000, 'X/USDT:USDT', pv_spike=0, funding_rate=0.0)
    evs = repo.list_for_grid('g1')
    assert len(evs) == 1 and evs[0]['pv_spike'] == 0 and evs[0]['pnl_ratio'] is None


def test_signal_list_for_grid_and_range(store):
    repo = SignalSnapshotRepository(store)
    repo.add('g1', 1000, 'X/USDT:USDT', pv_spike=1)
    repo.add('g1', 3000, 'X/USDT:USDT', funding_rate=0.003)
    repo.add('g2', 2000, 'Y/USDT:USDT', pv_spike=1)
    assert [e['ts'] for e in repo.list_for_grid('g1')] == [1000, 3000]
    assert [e['grid_id'] for e in repo.list_range(1500, 3500)] == ['g2', 'g1']

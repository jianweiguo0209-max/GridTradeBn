from gridtrade.state.equity import EquitySnapshotRepository


def test_add_if_due_throttles(store):
    repo = EquitySnapshotRepository(store)
    t = [1_000_000]
    assert repo.add_if_due(499.0, 400.0, interval_sec=300, now_ms_fn=lambda: t[0]) is True
    # 间隔内（+100s）不写
    t[0] = 1_100_000
    assert repo.add_if_due(500.0, None, interval_sec=300, now_ms_fn=lambda: t[0]) is False
    # 超间隔（+300s）才写
    t[0] = 1_300_000
    assert repo.add_if_due(501.0, None, interval_sec=300, now_ms_fn=lambda: t[0]) is True
    rows = repo.list_range(0)
    assert [r.equity for r in rows] == [499.0, 501.0]      # 升序，只 2 行
    assert repo.latest_ts() == 1_300_000


def test_list_range_filters(store):
    repo = EquitySnapshotRepository(store)
    for ts in (1000, 2000, 3000):
        repo.add_if_due(float(ts), None, interval_sec=0, now_ms_fn=lambda ts=ts: ts)
    assert [r.ts for r in repo.list_range(2000)] == [2000, 3000]
    assert [r.ts for r in repo.list_range(1000, 2000)] == [1000, 2000]

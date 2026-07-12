"""票池快照(2026-07-12,选币可复现性):仓储往返/幂等覆盖/区间读 + scheduler 落库。"""
import pandas as pd

from gridtrade.state.universe_snapshots import UniverseSnapshotRepository


def test_roundtrip_and_idempotent_overwrite(store):
    repo = UniverseSnapshotRepository(store)
    repo.add('hl', 1000, ['B/USDC:USDC', 'A/USDC:USDC'],
             excluded={'held_banned': ['C/USDC:USDC'], 'braked': []})
    snap = repo.get('hl', 1000)
    assert snap['symbols'] == ['A/USDC:USDC', 'B/USDC:USDC']   # 排序稳定
    assert snap['excluded']['held_banned'] == ['C/USDC:USDC']
    repo.add('hl', 1000, ['D/USDC:USDC'])                       # 同 tick 重跑 → 覆盖
    assert repo.get('hl', 1000)['symbols'] == ['D/USDC:USDC']
    assert repo.get('hl', 9999) is None


def test_list_range_ordered(store):
    repo = UniverseSnapshotRepository(store)
    for ts in (3000, 1000, 2000):
        repo.add('hl', ts, ['X/USDC:USDC'])
    out = repo.list_range('hl', 1000, 2500)
    assert [r[0] for r in out] == [1000, 2000]


def test_scheduler_tick_persists_snapshot(store, monkeypatch):
    """scheduler 每 tick 落一行快照:symbols=实际进入排名的集合(post 取数)。"""
    from gridtrade.config import load_deploy_config
    from gridtrade.runtime.factory import build_runtime
    from gridtrade.runtime.scheduler import run_scheduler_once
    rt = build_runtime(load_deploy_config(env={'EXCHANGE': 'fake'}))

    def _fake_fetch(adapter, symbols, run_time, **kw):
        return {'BTC/USDC:USDC': pd.DataFrame()}     # 实际进入排名的集合
    run_scheduler_once(rt, now_fn=lambda: 1_750_000_000.0, fetch_candles=_fake_fetch)
    run_ms = int(pd.Timestamp(1_750_000_000.0, unit='s').floor('H').value // 1_000_000)
    snap = UniverseSnapshotRepository(rt.store).get('fake', run_ms)
    assert snap is not None
    assert snap['symbols'] == ['BTC/USDC:USDC']
    assert 'held_banned' in snap['excluded'] and 'braked' in snap['excluded']

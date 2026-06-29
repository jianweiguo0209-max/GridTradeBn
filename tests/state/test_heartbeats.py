from gridtrade.state.models import Heartbeat


def _repo(store):
    from gridtrade.state.heartbeats import HeartbeatRepository
    return HeartbeatRepository(store)


def test_beat_inserts_then_updates_same_machine(store):
    repo = _repo(store)
    hb1 = repo.beat('monitor', ts=1000)
    assert isinstance(hb1, Heartbeat)
    assert hb1.machine == 'monitor' and hb1.last_beat_ts == 1000
    hb2 = repo.beat('monitor', ts=2000)        # 同机器 -> upsert 更新
    assert hb2.last_beat_ts == 2000
    assert repo.get('monitor').last_beat_ts == 2000


def test_get_missing_returns_none(store):
    assert _repo(store).get('nope') is None


def test_list_all_returns_all_machines(store):
    repo = _repo(store)
    repo.beat('monitor', ts=10)
    repo.beat('scheduler', ts=20)
    got = {h.machine: h.last_beat_ts for h in repo.list_all()}
    assert got == {'monitor': 10, 'scheduler': 20}


def test_beat_default_ts_is_positive(store):
    repo = _repo(store)
    hb = repo.beat('monitor')
    assert hb.last_beat_ts > 0

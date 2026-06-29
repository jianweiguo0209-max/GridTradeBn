from gridtrade.dashboard.queries import build_health
from gridtrade.state.heartbeats import HeartbeatRepository
from gridtrade.exchanges.base import Balance


class _FakeAdapter:
    def __init__(self, equity=499.0, cash=400.0, raise_balance=False):
        self._b = Balance(equity=equity, cash=cash)
        self._raise = raise_balance

    def fetch_balance(self):
        if self._raise:
            raise RuntimeError("network down")
        return self._b


def test_health_marks_stale_machine_and_reads_balance(store):
    hb = HeartbeatRepository(store)
    hb.beat('monitor', ts=1_000_000)
    hb.beat('scheduler', ts=1_000_000)

    # now = 1_000_000 + 40s -> monitor(40s) stale vs 30s threshold
    dto = build_health(store, _FakeAdapter(), now_ms_fn=lambda: 1_040_000,
                       stale_threshold_sec=30.0)

    by = {m.machine: m for m in dto.machines}
    assert by['monitor'].age_sec == 40.0
    assert by['monitor'].stale is True
    assert dto.equity == 499.0
    assert dto.cash == 400.0
    assert dto.balance_error is None
    assert dto.db_ok is True


def test_health_degrades_on_balance_error(store):
    HeartbeatRepository(store).beat('monitor', ts=1_000_000)
    dto = build_health(store, _FakeAdapter(raise_balance=True),
                       now_ms_fn=lambda: 1_005_000, stale_threshold_sec=30.0)
    assert dto.equity is None
    assert dto.balance_error is not None
    assert dto.db_ok is True

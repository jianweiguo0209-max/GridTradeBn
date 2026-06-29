"""只读查询层：复用现有仓储 + 直读表做 dashboard 聚合，绝不写库/写交易所。"""
from dataclasses import dataclass
from typing import List, Optional

from gridtrade.runtime.introspect import adapter_endpoint
from gridtrade.state.heartbeats import HeartbeatRepository
from gridtrade.state.models import now_ms


@dataclass
class MachineHealth:
    machine: str
    last_beat_ts: int
    age_sec: float
    stale: bool


@dataclass
class HealthDTO:
    machines: List[MachineHealth]
    endpoint: str
    equity: Optional[float]
    cash: Optional[float]
    balance_error: Optional[str]
    db_ok: bool


def build_health(store, adapter, *, now_ms_fn=now_ms,
                 stale_threshold_sec: float = 30.0) -> HealthDTO:
    db_ok = True
    machines: List[MachineHealth] = []
    try:
        beats = HeartbeatRepository(store).list_all()
        now = now_ms_fn()
        for hb in sorted(beats, key=lambda b: b.machine):
            age = (now - hb.last_beat_ts) / 1000.0
            machines.append(MachineHealth(
                machine=hb.machine, last_beat_ts=hb.last_beat_ts,
                age_sec=age, stale=age > stale_threshold_sec))
    except Exception:
        db_ok = False

    equity = cash = None
    balance_error = None
    try:
        bal = adapter.fetch_balance()
        equity, cash = bal.equity, bal.cash
    except Exception as exc:
        balance_error = repr(exc)

    try:
        endpoint = adapter_endpoint(adapter)
    except Exception:
        endpoint = 'n/a'

    return HealthDTO(machines=machines, endpoint=endpoint, equity=equity,
                     cash=cash, balance_error=balance_error, db_ok=db_ok)

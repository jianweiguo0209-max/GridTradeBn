"""HeartbeatRepository：机器心跳行（machine -> last_beat_ts）。fly 判活/告警靠它。"""
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy import insert, select, update

from gridtrade.state.models import Heartbeat, heartbeats, now_ms

_FIELDS = ('machine', 'last_beat_ts')


def _to_hb(row) -> Heartbeat:
    m = row._mapping
    return Heartbeat(**{f: m[f] for f in _FIELDS})


class HeartbeatRepository:
    def __init__(self, store):
        self.engine = store.engine

    def beat(self, machine: str, ts: Optional[int] = None) -> Heartbeat:
        ts = int(ts) if ts is not None else now_ms()
        try:
            with self.engine.begin() as c:
                c.execute(insert(heartbeats),
                          {'machine': machine, 'last_beat_ts': ts})
        except sa.exc.IntegrityError:
            with self.engine.begin() as c:
                c.execute(update(heartbeats)
                          .where(heartbeats.c.machine == machine)
                          .values(last_beat_ts=ts))
        return self.get(machine)

    def get(self, machine: str) -> Optional[Heartbeat]:
        with self.engine.connect() as c:
            row = c.execute(
                select(heartbeats).where(heartbeats.c.machine == machine)).first()
        return _to_hb(row) if row is not None else None

    def list_all(self) -> List[Heartbeat]:
        with self.engine.connect() as c:
            rows = c.execute(select(heartbeats)).all()
        return [_to_hb(r) for r in rows]

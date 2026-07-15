"""HeartbeatRepository：机器心跳行（machine -> last_beat_ts）。fly 判活/告警靠它。"""
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from gridtrade.state.models import Heartbeat, heartbeats, now_ms

_FIELDS = ('machine', 'last_beat_ts')


def _to_hb(row) -> Heartbeat:
    m = row._mapping
    return Heartbeat(**{f: m[f] for f in _FIELDS})


class HeartbeatRepository:
    def __init__(self, store):
        self.engine = store.engine

    def beat(self, machine: str, ts: Optional[int] = None) -> Heartbeat:
        # 原生 upsert（ON CONFLICT DO UPDATE）取代 INSERT→catch IntegrityError→UPDATE：旧模式
        # 每次心跳（machine 已存在）必产生一条 PG duplicate-key ERROR（monitor/scheduler ~5-10s
        # 一次 → 洪水），且失败事务 ROLLBACK + 二次 UPDATE 往返加重 PG 负载（testnet X:10 选币/
        # 开格尖峰断连的贡献因子，2026-07-15 实证）。ON CONFLICT 原子一次写、无 ERROR、无竞态。
        ts = int(ts) if ts is not None else now_ms()
        ins = (pg_insert if self.engine.dialect.name == 'postgresql' else sqlite_insert)(heartbeats)
        stmt = ins.values(machine=machine, last_beat_ts=ts).on_conflict_do_update(
            index_elements=['machine'], set_={'last_beat_ts': ts})
        with self.engine.begin() as c:
            c.execute(stmt)
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

"""EquitySnapshotRepository：monitor 节流写真权益快照（节流逻辑落 DB，重启安全）。"""
import uuid
from typing import List, Optional

from sqlalchemy import insert, select

from gridtrade.state.models import equity_snapshots, EquitySnapshot, now_ms

_FIELDS = ('id', 'ts', 'equity', 'cash')


def _to_snap(row) -> EquitySnapshot:
    m = row._mapping
    return EquitySnapshot(**{f: m[f] for f in _FIELDS})


class EquitySnapshotRepository:
    def __init__(self, store):
        self.engine = store.engine

    def latest_ts(self) -> Optional[int]:
        with self.engine.connect() as c:
            row = c.execute(
                select(equity_snapshots.c.ts)
                .order_by(equity_snapshots.c.ts.desc()).limit(1)
            ).first()
        return int(row[0]) if row is not None else None

    def add_if_due(self, equity: float, cash: Optional[float] = None, *,
                   interval_sec: int, now_ms_fn=now_ms) -> bool:
        now = now_ms_fn()
        latest = self.latest_ts()
        if latest is not None and now - latest < interval_sec * 1000:
            return False
        with self.engine.begin() as c:
            c.execute(insert(equity_snapshots), {
                'id': uuid.uuid4().hex, 'ts': now, 'equity': float(equity),
                'cash': None if cash is None else float(cash),
            })
        return True

    def list_range(self, start_ms: int,
                   end_ms: Optional[int] = None) -> List[EquitySnapshot]:
        q = select(equity_snapshots).where(equity_snapshots.c.ts >= start_ms)
        if end_ms is not None:
            q = q.where(equity_snapshots.c.ts <= end_ms)
        with self.engine.connect() as c:
            rows = c.execute(q.order_by(equity_snapshots.c.ts)).all()
        return [_to_snap(r) for r in rows]

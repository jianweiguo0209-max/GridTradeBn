"""票池快照仓储(2026-07-12,选币可复现性)。

因子名次是组内相对名次:票池集合一变,全体名次重排(实证:TRUMP 在 168 币集合无影、
57 币线上集合进 #4)。事后精确复现历史选币必须留存"当时实际进入排名的集合"
(post 地板/黑名单/held 预过滤/取数跳过)。scheduler 每 tick 写一行,幂等。
"""
import json

import sqlalchemy as sa
from sqlalchemy import insert, select

from gridtrade.state.models import now_ms, universe_snapshots


class UniverseSnapshotRepository:
    def __init__(self, store):
        self.engine = store.engine

    def add(self, exchange: str, run_time_ms: int, symbols, excluded=None) -> None:
        """幂等写入(同 tick 重跑覆盖为最新)。symbols=实际进入排名的币列表。"""
        values = {'exchange': exchange, 'run_time': int(run_time_ms),
                  'symbols': json.dumps(sorted(symbols)),
                  'excluded': json.dumps(excluded or {}, ensure_ascii=False),
                  'created_at': now_ms()}
        try:
            with self.engine.begin() as c:
                c.execute(insert(universe_snapshots), values)
        except sa.exc.IntegrityError:
            with self.engine.begin() as c:
                c.execute(sa.update(universe_snapshots)
                          .where(universe_snapshots.c.exchange == exchange)
                          .where(universe_snapshots.c.run_time == int(run_time_ms))
                          .values(symbols=values['symbols'],
                                  excluded=values['excluded'],
                                  created_at=values['created_at']))

    def get(self, exchange: str, run_time_ms: int):
        """{'symbols': [...], 'excluded': {...}} 或 None。"""
        with self.engine.connect() as c:
            row = c.execute(
                select(universe_snapshots)
                .where(universe_snapshots.c.exchange == exchange)
                .where(universe_snapshots.c.run_time == int(run_time_ms))
            ).first()
        if row is None:
            return None
        m = row._mapping
        return {'symbols': json.loads(m['symbols']),
                'excluded': json.loads(m['excluded'] or '{}'),
                'created_at': m['created_at']}

    def list_range(self, exchange: str, start_ms: int, end_ms: int):
        """[(run_time, symbols, excluded)] 升序——离线重放的驱动数据。"""
        with self.engine.connect() as c:
            rows = c.execute(
                select(universe_snapshots)
                .where(universe_snapshots.c.exchange == exchange)
                .where(universe_snapshots.c.run_time >= int(start_ms))
                .where(universe_snapshots.c.run_time <= int(end_ms))
                .order_by(universe_snapshots.c.run_time)
            ).all()
        return [(r._mapping['run_time'], json.loads(r._mapping['symbols']),
                 json.loads(r._mapping['excluded'] or '{}')) for r in rows]

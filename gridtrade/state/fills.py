"""FillRepository：已摄入成交的持久去重 + 重放真相源（trade_id 主键）。"""
from typing import List

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from gridtrade.state.models import Fill, grid_fills, now_ms

_FIELDS = ('trade_id', 'grid_id', 'line_index', 'side', 'price', 'size', 'fee', 'ts', 'created_at')


def _to_fill(row) -> Fill:
    m = row._mapping
    return Fill(**{f: m[f] for f in _FIELDS})


class FillRepository:
    def __init__(self, store):
        self.engine = store.engine

    def add_if_new(self, fill: Fill) -> bool:
        # 原生 upsert（ON CONFLICT DO NOTHING）取代 INSERT→catch IntegrityError：重叠窗口
        # （_TRADE_REFETCH_OVERLAP_MS）每轮重复摄入已入账成交，旧模式每次都产生一条 PG
        # duplicate-key ERROR（grid_fills_pkey 洪水）+ 失败事务 ROLLBACK 开销。rowcount>0=真新
        # 插入（幂等语义不变：新=True、重复=False）。
        values = {f: getattr(fill, f) for f in _FIELDS}
        values['created_at'] = fill.created_at or now_ms()
        ins = (pg_insert if self.engine.dialect.name == 'postgresql' else sqlite_insert)(grid_fills)
        stmt = ins.values(**values).on_conflict_do_nothing(index_elements=['trade_id'])
        with self.engine.begin() as c:
            r = c.execute(stmt)
        return r.rowcount > 0

    def list_by_grid(self, grid_id: str) -> List[Fill]:
        with self.engine.connect() as c:
            rows = c.execute(select(grid_fills)
                             .where(grid_fills.c.grid_id == grid_id)
                             .order_by(grid_fills.c.ts)).all()
        return [_to_fill(r) for r in rows]

    def max_ts(self, grid_id: str) -> int:
        # 排除合成行(ledger: 前缀,内部转仓/关格 reduce 记账):max_ts 是 fetch_my_trades
        # 的 since 游标源(sync+restore),合成行 ts=now 会把游标推过未摄入的真实成交。
        # list_by_grid(restore 重放)不排除——重放正是 claims 恢复机制。
        with self.engine.connect() as c:
            v = c.execute(select(sa.func.max(grid_fills.c.ts))
                          .where(grid_fills.c.grid_id == grid_id)
                          .where(~grid_fills.c.trade_id.like('ledger:%'))).scalar()
        return int(v) if v is not None else 0

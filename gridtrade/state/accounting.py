"""AccountingRepository：网格实时记账（乐观锁 + 峰值收益跟踪）。"""
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from gridtrade.state.models import (Accounting, ConcurrencyError, grid_accounting,
                                    now_ms)

_FIELDS = ('grid_id', 'realized_pnl', 'fee_paid', 'funding_paid', 'net_position',
           'avg_price', 'pnl_ratio_max', 'funding_cursor', 'updated_at', 'version')


def _to_acc(row) -> Accounting:
    m = row._mapping
    return Accounting(**{f: m[f] for f in _FIELDS})


class AccountingRepository:
    def __init__(self, store):
        self.engine = store.engine

    def init(self, grid_id: str) -> Accounting:
        # init-if-absent：原生 ON CONFLICT DO NOTHING 取代 INSERT→catch IntegrityError（重启
        # restore 重复 init 同 grid_id 会撞主键刷 PG ERROR）。低频，与三张高频表统一 upsert 口径。
        ins = (pg_insert if self.engine.dialect.name == 'postgresql' else sqlite_insert)(grid_accounting)
        stmt = ins.values(
            grid_id=grid_id, realized_pnl=0.0, fee_paid=0.0, funding_paid=0.0,
            net_position=0.0, avg_price=0.0, pnl_ratio_max=0.0, funding_cursor=0,
            updated_at=now_ms(), version=1).on_conflict_do_nothing(index_elements=['grid_id'])
        with self.engine.begin() as c:
            c.execute(stmt)
        return self.get(grid_id)

    def get(self, grid_id: str) -> Optional[Accounting]:
        with self.engine.connect() as c:
            row = c.execute(
                select(grid_accounting).where(grid_accounting.c.grid_id == grid_id)
            ).first()
        return _to_acc(row) if row is not None else None

    def save(self, acc: Accounting) -> Accounting:
        with self.engine.begin() as c:
            res = c.execute(
                update(grid_accounting)
                .where(grid_accounting.c.grid_id == acc.grid_id,
                       grid_accounting.c.version == acc.version)
                .values(realized_pnl=acc.realized_pnl, fee_paid=acc.fee_paid,
                        funding_paid=acc.funding_paid, net_position=acc.net_position,
                        avg_price=acc.avg_price, pnl_ratio_max=acc.pnl_ratio_max,
                        funding_cursor=acc.funding_cursor,
                        version=acc.version + 1, updated_at=now_ms())
            )
            if res.rowcount == 0:
                raise ConcurrencyError(
                    f'stale version for accounting {acc.grid_id}: {acc.version}')
        return self.get(acc.grid_id)

    def bump_peak(self, grid_id: str, pnl_ratio: float) -> Accounting:
        for _ in range(2):  # 读-改-写；并发陈旧时重试一次
            acc = self.get(grid_id)
            if acc is None:
                acc = self.init(grid_id)
            if pnl_ratio <= acc.pnl_ratio_max:
                return acc
            acc.pnl_ratio_max = pnl_ratio
            try:
                return self.save(acc)
            except ConcurrencyError:
                continue
        return self.get(grid_id)

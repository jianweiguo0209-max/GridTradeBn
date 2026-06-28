"""RecordRepository：历史成交/关仓记录（替代 orderInfo.pkl / gridResult.csv）。"""
import uuid
from typing import List, Optional

from sqlalchemy import insert, select

from gridtrade.state.models import Record, now_ms, order_records

_FIELDS = ('id', 'grid_id', 'exchange', 'symbol', 'tag', 'offset', 'opened_at',
           'closed_at', 'sz', 'total_pnl', 'pnl_ratio', 'exit_reason', 'created_at')


def _to_record(row) -> Record:
    m = row._mapping
    return Record(**{f: m[f] for f in _FIELDS})


class RecordRepository:
    def __init__(self, store):
        self.engine = store.engine

    def add(self, record: Record) -> Record:
        rid = record.id or uuid.uuid4().hex
        values = {f: getattr(record, f) for f in _FIELDS}
        values['id'] = rid
        values['created_at'] = record.created_at or now_ms()
        with self.engine.begin() as c:
            c.execute(insert(order_records), values)
        return self.get(rid)

    def get(self, record_id: str) -> Optional[Record]:
        with self.engine.begin() as c:
            row = c.execute(
                select(order_records).where(order_records.c.id == record_id)
            ).first()
        return _to_record(row) if row is not None else None

    def list_by_tag(self, tag: str) -> List[Record]:
        with self.engine.begin() as c:
            rows = c.execute(
                select(order_records).where(order_records.c.tag == tag)
                .order_by(order_records.c.created_at)
            ).all()
        return [_to_record(r) for r in rows]

    def list_by_grid(self, grid_id: str) -> List[Record]:
        with self.engine.begin() as c:
            rows = c.execute(
                select(order_records).where(order_records.c.grid_id == grid_id)
                .order_by(order_records.c.created_at)
            ).all()
        return [_to_record(r) for r in rows]

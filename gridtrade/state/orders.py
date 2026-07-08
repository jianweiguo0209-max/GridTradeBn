"""OrderRepository：网格挂单的持久化（按 client_oid 主键 upsert）。"""
from typing import List, Optional

from sqlalchemy import insert, select, update

from gridtrade.state.models import GridOrder, grid_orders, now_ms

_FIELDS = ('client_oid', 'grid_id', 'line_index', 'exchange_order_id', 'side',
           'price', 'size', 'status', 'filled', 'created_at', 'updated_at')


def _to_order(row) -> GridOrder:
    m = row._mapping
    return GridOrder(**{f: m[f] for f in _FIELDS})


class OrderRepository:
    def __init__(self, store):
        self.engine = store.engine

    def upsert(self, order: GridOrder) -> GridOrder:
        import sqlalchemy as sa
        ts = now_ms()
        values = {f: getattr(order, f) for f in _FIELDS}
        values['created_at'] = order.created_at or ts
        values['updated_at'] = ts
        try:
            with self.engine.begin() as c:
                c.execute(insert(grid_orders), values)
        except sa.exc.IntegrityError:
            # client_oid already exists -> update mutable fields, preserve created_at
            with self.engine.begin() as c:
                c.execute(
                    update(grid_orders)
                    .where(grid_orders.c.client_oid == order.client_oid)
                    .values(grid_id=order.grid_id, line_index=order.line_index,
                            exchange_order_id=order.exchange_order_id,
                            side=order.side, price=order.price, size=order.size,
                            status=order.status, filled=order.filled, updated_at=ts)
                )
        return self.get(order.client_oid)

    def get(self, client_oid: str) -> Optional[GridOrder]:
        with self.engine.connect() as c:
            row = c.execute(
                select(grid_orders).where(grid_orders.c.client_oid == client_oid)
            ).first()
        return _to_order(row) if row is not None else None

    def list_by_grid(self, grid_id: str) -> List[GridOrder]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(grid_orders).where(grid_orders.c.grid_id == grid_id)
                .order_by(grid_orders.c.created_at)
            ).all()
        return [_to_order(r) for r in rows]

    def list_open_by_grid(self, grid_id: str) -> List[GridOrder]:
        return [o for o in self.list_by_grid(grid_id) if o.status == 'open']

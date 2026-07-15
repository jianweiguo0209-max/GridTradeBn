"""OrderRepository：网格挂单的持久化（按 client_oid 主键 upsert）。"""
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

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
        # 原生 upsert（ON CONFLICT DO UPDATE）取代 INSERT→catch IntegrityError→UPDATE：每轮
        # reconcile/sync 更新挂单（补单/重挂/吃单）都撞 client_oid 主键，旧模式每次一条 PG
        # duplicate-key ERROR（grid_orders_pkey 洪水，量最大——每轮所有挂单）+ 废事务 ROLLBACK
        # 开销（testnet PG 尖峰断连贡献因子，2026-07-15 实证）。ON CONFLICT 原子一次写、保留 created_at。
        ts = now_ms()
        values = {f: getattr(order, f) for f in _FIELDS}
        values['created_at'] = order.created_at or ts
        values['updated_at'] = ts
        ins = (pg_insert if self.engine.dialect.name == 'postgresql' else sqlite_insert)(grid_orders)
        set_ = {k: values[k] for k in _FIELDS if k not in ('client_oid', 'created_at')}
        stmt = ins.values(**values).on_conflict_do_update(index_elements=['client_oid'], set_=set_)
        with self.engine.begin() as c:
            c.execute(stmt)
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

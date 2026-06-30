"""只读复盘聚合：曲线/归因/分布/退出原因。纯计算，不写库、不调行情。"""
from typing import List, Tuple

from sqlalchemy import select

from gridtrade.state.equity import EquitySnapshotRepository
from gridtrade.state.models import order_records


def realized_curve(store, *, start_ms: int = 0) -> List[Tuple]:
    with store.engine.connect() as c:
        rows = c.execute(
            select(order_records.c.closed_at, order_records.c.total_pnl)
            .where(order_records.c.closed_at.isnot(None),
                   order_records.c.closed_at >= start_ms)
            .order_by(order_records.c.closed_at)
        ).all()
    out = []
    cum = 0.0
    for closed_at, pnl in rows:
        cum += (pnl or 0.0)
        out.append((int(closed_at), cum))
    return out


def equity_curve(store, *, start_ms: int = 0) -> List[Tuple]:
    snaps = EquitySnapshotRepository(store).list_range(start_ms)
    return [(s.ts, s.equity) for s in snaps]

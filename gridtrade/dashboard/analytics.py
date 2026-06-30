"""只读复盘聚合：曲线/归因/分布/退出原因。纯计算，不写库、不调行情。"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

from sqlalchemy import select

from gridtrade.state.equity import EquitySnapshotRepository
from gridtrade.state.models import grid_fills, order_records


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


@dataclass
class TagStat:
    tag: str
    count: int
    total_pnl: float
    total_fee: float
    net_pnl: float
    win_count: int
    win_rate: float
    avg_hold_ms: Optional[float]
    max_drawdown: float


def _max_drawdown(cum_series) -> float:
    peak = float('-inf')
    mdd = 0.0
    for v in cum_series:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd if mdd != float('-inf') else 0.0


def tag_attribution(store, *, start_ms: int = 0) -> List[TagStat]:
    with store.engine.connect() as c:
        recs = c.execute(
            select(order_records.c.tag, order_records.c.grid_id,
                   order_records.c.total_pnl, order_records.c.opened_at,
                   order_records.c.closed_at)
            .where(order_records.c.closed_at.isnot(None),
                   order_records.c.closed_at >= start_ms)
            .order_by(order_records.c.closed_at)
        ).all()
        fee_rows = c.execute(
            select(grid_fills.c.grid_id, grid_fills.c.fee)
        ).all()
    fee_by_grid = {}
    for gid, fee in fee_rows:
        fee_by_grid[gid] = fee_by_grid.get(gid, 0.0) + (fee or 0.0)

    agg = {}
    for tag, gid, pnl, opened, closed in recs:
        a = agg.setdefault(tag, {'count': 0, 'pnl': 0.0, 'win': 0, 'fee': 0.0,
                                 'holds': [], 'cum': [], 'run': 0.0})
        a['count'] += 1
        a['pnl'] += (pnl or 0.0)
        if (pnl or 0.0) > 0:
            a['win'] += 1
        a['fee'] += fee_by_grid.get(gid, 0.0)
        if opened is not None and closed is not None:
            a['holds'].append(closed - opened)
        a['run'] += (pnl or 0.0)
        a['cum'].append(a['run'])

    out = []
    for tag in sorted(agg):
        a = agg[tag]
        avg_hold = (sum(a['holds']) / len(a['holds'])) if a['holds'] else None
        out.append(TagStat(
            tag=tag, count=a['count'], total_pnl=a['pnl'], total_fee=a['fee'],
            net_pnl=a['pnl'] - a['fee'], win_count=a['win'],
            win_rate=(a['win'] / a['count'] if a['count'] else 0.0),
            avg_hold_ms=avg_hold, max_drawdown=_max_drawdown(a['cum'])))
    return out
